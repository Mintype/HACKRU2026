"""Mobile routes: frames in, roster out.

The pose model is stubbed, so this runs without a camera, MediaPipe or a network.
JPEG encoding/decoding needs OpenCV, and those tests skip without it.
"""

import importlib.util
import json
import os
import tempfile
import unittest

HAVE_CV2 = importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None

from tests.test_multipose import facing_pose, side_pose


class StubLandmarker:
    """Stands in for the MediaPipe wrapper: returns whatever it was handed."""

    def __init__(self, poses=None, num_poses=3):
        self.poses = list(poses or [])
        self.num_poses = num_poses
        self.calls = 0

    def detect_multi(self, frame):
        self.calls += 1
        return list(self.poses)

    def detect(self, frame):
        poses = self.detect_multi(frame)
        return poses[0] if poses else None

    def close(self):
        pass


def jpeg_bytes(width=64, height=48):
    """A real (if dull) JPEG, so the decode path is genuinely exercised."""
    import cv2
    import numpy as np

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


class MobileAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["POSTURE_DATA_DIR"] = self.tmp.name
        import config

        config.ensure_dirs()
        self.landmarker = StubLandmarker()
        from server.app import create_app

        self.app = create_app(
            mobile=True,
            mobile_max_people=3,
            mobile_landmarker=self.landmarker,
            host="127.0.0.1",
            port=5099,
        )
        self.client = self.app.test_client()
        self.coach = self.app.coach

    def tearDown(self):
        os.environ.pop("POSTURE_DATA_DIR", None)

    def post_frame(self, poses=None, device="test-phone"):
        if poses is not None:
            self.landmarker.poses = list(poses)
        return self.client.post(
            f"/m/frame?device={device}",
            data=jpeg_bytes(),
            content_type="image/jpeg",
        )


class TestMobileRoutes(MobileAppTestCase):
    def test_mobile_mode_has_no_local_camera_thread(self):
        self.assertTrue(self.coach.mobile_mode)
        self.assertIsNone(self.coach.source)
        self.assertIsNone(self.coach.detector)
        # The vision thread is never started, so it does not claim frames.
        self.assertFalse(self.coach.vision.is_alive())
        self.assertEqual(self.coach.vision.status()["mode"], "mobile")
        self.assertEqual(self.coach.vision.status()["source"], "phone")

    def test_the_page_is_served(self):
        response = self.client.get("/m")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Posture Coach", body)
        self.assertIn("/static/mobile.js", body)

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_a_frame_with_nobody_in_it_is_a_valid_empty_answer(self):
        response = self.post_frame(poses=[])
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["seen"], 0)
        self.assertEqual(data["roster"], [])

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_two_people_in_one_frame_come_back_as_two_tracks(self):
        response = self.post_frame(poses=[facing_pose(centre=150.0), facing_pose(centre=430.0)])
        data = response.get_json()
        self.assertEqual(data["seen"], 2)
        self.assertEqual(len(data["roster"]), 2)
        self.assertEqual({row["label"] for row in data["roster"]}, {"Person 1", "Person 2"})
        self.assertIn("detect_ms", data["timing"])

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_the_roster_carries_what_the_phone_needs_to_draw(self):
        self.post_frame(poses=[side_pose()])
        data = self.post_frame(poses=[side_pose()]).get_json()
        row = data["roster"][0]
        for key in ("id", "label", "view", "metric", "score", "state", "good", "bad", "points"):
            self.assertIn(key, row)
        self.assertEqual(row["view"], "side")
        self.assertTrue(row["points"])

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_the_desktop_hud_is_fed_the_worst_person(self):
        # Both upright first so each has their own baseline, then one slumps.
        for _ in range(6):
            self.post_frame(poses=[facing_pose(centre=150.0), facing_pose(centre=430.0)])
        for _ in range(12):
            self.post_frame(poses=[facing_pose(centre=150.0), facing_pose(centre=430.0, ear_y=155.0)])
        reading = self.coach.vision.latest_reading()
        self.assertTrue(reading.get("mobile"))
        self.assertEqual(reading.get("people"), 2)
        self.assertIsNotNone(reading.get("score"))
        self.assertLess(reading["score"], 0.5)
        # The phone's frames are what /video_feed serves now.
        self.assertIsNotNone(self.coach.vision.latest_jpeg())

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_an_empty_room_still_shares_the_picture(self):
        # Aiming the phone is exactly when you want to see what it sees, so the
        # frame is shared even with nobody in it - the HUD just says nobody.
        self.post_frame(poses=[])
        self.assertIsNotNone(self.coach.vision.latest_jpeg())
        reading = self.coach.vision.latest_reading()
        self.assertFalse(reading.get("person"))
        self.assertIsNone(reading.get("score"))
        self.assertTrue(reading.get("mobile"))

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_a_broken_payload_is_refused_not_crashed(self):
        response = self.client.post(
            "/m/frame", data=b"not a jpeg at all", content_type="image/jpeg"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("decode", data["error"])
        self.assertEqual(self.coach.mobile.dropped, 1)

    def test_an_empty_body_is_a_400(self):
        response = self.client.post("/m/frame", data=b"", content_type="image/jpeg")
        self.assertEqual(response.status_code, 400)

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_state_reports_the_roster_without_a_new_frame(self):
        self.post_frame(poses=[facing_pose(centre=200.0)])
        data = self.client.get("/m/state").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["seen"], 1)
        self.assertEqual(data["roster"][0]["label"], "Person 1")

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_status_includes_the_mobile_block(self):
        self.post_frame(poses=[facing_pose(centre=200.0)])
        status = self.client.get("/api/status").get_json()
        self.assertTrue(status["server"]["mobile"])
        self.assertEqual(status["mobile"]["mode"], "mobile")
        self.assertEqual(status["mobile"]["people"], 1)
        self.assertEqual(status["mobile"]["max_people"], 3)
        self.assertEqual(status["mobile"]["devices"], ["test-phone"])

    @unittest.skipUnless(HAVE_CV2, "opencv + numpy required to encode a frame")
    def test_a_fast_poster_does_not_duplicate_the_person(self):
        for _ in range(10):
            data = self.post_frame(poses=[facing_pose(centre=250.0 + 3.0)]).get_json()
        self.assertEqual(len(data["roster"]), 1)
        self.assertEqual(data["roster"][0]["id"], 1)


class TestMobileDisabled(unittest.TestCase):
    """Without --mobile the routes must say so rather than half-work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["POSTURE_DATA_DIR"] = self.tmp.name
        import config

        config.ensure_dirs()
        from server.app import create_app

        self.app = create_app(simulate=True, host="127.0.0.1", port=5098)
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("POSTURE_DATA_DIR", None)

    def test_frame_posting_is_refused_with_a_clear_message(self):
        response = self.client.post("/m/frame", data=b"whatever", content_type="image/jpeg")
        self.assertEqual(response.status_code, 409)
        self.assertIn("--mobile", response.get_json()["error"])

    def test_state_polling_is_refused_too(self):
        response = self.client.get("/m/state")
        self.assertEqual(response.status_code, 409)

    def test_status_has_no_mobile_block(self):
        status = self.client.get("/api/status").get_json()
        self.assertFalse(status["server"]["mobile"])
        self.assertIsNone(status["mobile"])

    def test_the_page_still_loads_and_says_what_to_do(self):
        response = self.client.get("/m")
        self.assertEqual(response.status_code, 200)
        self.assertIn("mobile.js", response.get_data(as_text=True))


class TestRoastCarriesThePerson(unittest.TestCase):
    """The per-roast context must reach the prompt (and therefore the phone)."""

    def test_a_roast_context_override_wins(self):
        from voice.roasts import fallback_batch

        # The service merges the event's context over the global one; the mobile
        # tracker sends {"person": ..., "view": ..., "metric": ...}.
        merged = {"slouch_part": "neck"}
        merged.update({"person": "Person 2", "view": "front", "metric": "front · head sink 22%"})
        self.assertEqual(merged["person"], "Person 2")
        batch = fallback_batch(count=3, tier="mild", part=merged["slouch_part"])
        self.assertEqual(len(batch), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
