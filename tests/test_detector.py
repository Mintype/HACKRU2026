"""Detector tests using a stub landmarker (no MediaPipe, no camera)."""

import importlib.util
import unittest

from vision.detector import PostureDetector

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_CV2 = importlib.util.find_spec("cv2") is not None


class StubLandmarker:
    """Returns whatever landmarks it was told to return."""

    def __init__(self, landmarks=None):
        self.landmarks = landmarks

    def detect(self, frame):
        return self.landmarks


def upright():
    return {
        "side": "right",
        "ear": (100.0, 100.0, 1.0),
        "eye": (150.0, 105.0, 1.0),
        "shoulder": (100.0, 200.0, 1.0),
        "hip": (100.0, 300.0, 1.0),
        "knee": (200.0, 310.0, 1.0),
    }


def slouching():
    # Head juts 50 px forward over 90 px: ~29 degrees.
    return {
        "side": "right",
        "ear": (150.0, 110.0, 1.0),
        "eye": (200.0, 115.0, 1.0),
        "shoulder": (100.0, 200.0, 1.0),
        "hip": (100.0, 300.0, 1.0),
        "knee": (200.0, 310.0, 1.0),
    }


class TestPostureDetector(unittest.TestCase):
    def make(self, landmarks):
        return PostureDetector(StubLandmarker(landmarks), tolerance=20.0)

    def test_no_person(self):
        detector = self.make(None)
        reading = detector.process(frame=None, ts=0.0)
        self.assertFalse(reading["person"])
        self.assertFalse(reading["seated"])
        self.assertIsNone(reading["score"])

    def test_upright_geometry_and_keys(self):
        detector = self.make(upright())
        detector.set_baseline(12.0, 4.0)
        reading = detector.process(frame=None, ts=0.0)
        for key in ("ts", "person", "seated", "neck_angle", "torso_angle", "score"):
            self.assertIn(key, reading)
        self.assertAlmostEqual(reading["neck_angle"], 0.0, places=3)
        self.assertAlmostEqual(reading["torso_angle"], 0.0, places=3)
        self.assertAlmostEqual(reading["score"], 1.0, places=3)

    def test_seated_after_delay(self):
        detector = self.make(upright())
        detector.set_baseline(12.0, 4.0)
        self.assertFalse(detector.process(frame=None, ts=0.0)["seated"])
        self.assertFalse(detector.process(frame=None, ts=1.0)["seated"])
        self.assertTrue(detector.process(frame=None, ts=2.0)["seated"])

    def test_force_seated_ignores_a_steep_thigh(self):
        standing = dict(upright(), knee=(110.0, 420.0, 1.0))  # thigh near vertical
        self.assertFalse(self.make(standing).process(frame=None, ts=5.0)["seated"])
        detector = PostureDetector(StubLandmarker(standing), tolerance=20.0, force_seated=True)
        self.assertFalse(detector.process(frame=None, ts=0.0)["seated"])
        self.assertTrue(detector.process(frame=None, ts=2.0)["seated"])
        # Nobody in frame is still away, forced or not.
        detector.landmarker.landmarks = None
        self.assertFalse(detector.process(frame=None, ts=3.0)["seated"])

    def test_slouch_lowers_score(self):
        detector = self.make(slouching())
        detector.set_baseline(12.0, 4.0)
        # Feed a few frames so the EMA converges.
        reading = None
        for i in range(15):
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertLess(reading["score"], 0.7)
        self.assertGreater(reading["neck_angle"], 25.0)

    def test_smoothing_resists_a_single_bad_frame(self):
        detector = self.make(upright())
        detector.set_baseline(12.0, 4.0)
        for i in range(20):
            detector.process(frame=None, ts=i * 0.1)
        detector.landmarker.landmarks = slouching()
        reading = detector.process(frame=None, ts=2.0)
        # One noisy frame barely moves the EMA and must not cross the slouch
        # threshold (0.7); alpha is 0.2.
        self.assertGreater(reading["score"], 0.7)

    def test_knee_hidden_falls_back_to_hip_stability(self):
        landmarks = upright()
        landmarks["knee"] = (200.0, 310.0, 0.05)  # desk hides the knee
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        for i in range(30):
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertTrue(reading["seated"])
        self.assertIsNone(reading["thigh_angle"])

    def test_reports_which_part_is_giving_way(self):
        # head juts 50px forward, torso stays upright
        detector = self.make(slouching())
        detector.set_baseline(12.0, 4.0)
        reading = detector.process(frame=None, ts=0.0)
        self.assertEqual(reading["slouch_part"], "neck")
        self.assertGreater(reading["neck_drift"], reading["torso_drift"])

    def test_torso_dominant_slouch(self):
        landmarks = upright()
        landmarks["shoulder"] = (150.0, 200.0, 1.0)  # shoulder forward of the hip
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        reading = detector.process(frame=None, ts=0.0)
        self.assertEqual(reading["slouch_part"], "torso")
        self.assertGreater(reading["torso_drift"], reading["neck_drift"])

class TestSleepDetection(unittest.TestCase):
    """Sleep is collapsed + motionless + head down, held for a while.

    The pieces are tested separately because each one alone is a false positive:
    a dropped head is just looking down, and a collapse is just slouching.
    """

    def collapsed(self):
        """A bad slump with the head dropped far below the calibrated level."""
        landmarks = slouching()
        landmarks["ear"] = (150.0, 110.0, 1.0)
        landmarks["eye"] = (170.0, 180.0, 1.0)  # eye 70px below a 20px ear gap
        return landmarks

    def test_a_dropped_head_alone_is_not_sleep(self):
        # Sitting up straight and looking down is a posture problem, not a nap.
        landmarks = upright()
        landmarks["eye"] = (170.0, 180.0, 1.0)
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        for i in range(20):
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertFalse(reading["sleepy"])
        self.assertGreater(reading["score"], 0.35)

    def test_a_still_collapse_alone_is_not_sleep_without_a_dropped_head(self):
        landmarks = slouching()  # collapsed, but the head is level
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        for i in range(20):
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertLess(reading["score"], 0.35)
        self.assertFalse(reading["sleepy"])

    def test_a_still_collapse_with_a_dropped_head_is_sleep(self):
        landmarks = upright()
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        detector.start_head_calibration(0.0, 0.5)
        for i in range(10):  # calibrate while sitting up
            detector.process(frame=None, ts=i * 0.1)
        reference = detector.head_down_baseline
        self.assertIsNotNone(reference)

        # Now they fold over and stop moving. The first frames after the change
        # still count as movement, which is right: they have only just settled.
        landmarks.clear()
        landmarks.update(self.collapsed())
        for i in range(40):
            reading = detector.process(frame=None, ts=1.0 + i * 0.1)
        self.assertTrue(reading["sleepy"])
        self.assertAlmostEqual(reading["head_ref_deg"], round(reference, 1), places=1)
        self.assertGreater(reading["head_drop_deg"], 20.0)
        self.assertLess(reading["motion"], 0.006)

    def test_moving_keeps_them_awake(self):
        # Same slumped pose, but fidgeting: awake, so not asleep.
        landmarks = self.collapsed()
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        for i in range(30):
            landmarks["knee"] = (200.0 + (20.0 if i % 2 else -20.0), 310.0, 1.0)
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertLess(reading["score"], 0.35)
        self.assertGreater(reading["motion"], 0.006)
        self.assertFalse(reading["sleepy"])

    def test_a_hidden_eye_still_counts_as_head_down(self):
        # A head dropped far enough usually takes the eye landmarks with it.
        landmarks = self.collapsed()
        landmarks["eye"] = (170.0, 180.0, 0.01)
        detector = self.make(landmarks)
        detector.set_baseline(12.0, 4.0)
        for i in range(20):
            reading = detector.process(frame=None, ts=i * 0.1)
        self.assertIsNone(reading["head_down_deg"])
        self.assertTrue(reading["sleepy"])

    def make(self, landmarks):
        return PostureDetector(StubLandmarker(landmarks), tolerance=20.0)

    @unittest.skipUnless(HAVE_NUMPY and HAVE_CV2, "numpy + opencv required")
    def test_overlay_draws_without_error(self):
        import numpy as np

        detector = self.make(slouching())
        detector.set_baseline(12.0, 4.0)
        reading = detector.process(frame=None, ts=0.0)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        out = detector.draw_overlay(frame, reading)
        self.assertEqual(out.shape, frame.shape)
        self.assertGreater(int(out.sum()), 0)


class TestSyntheticFeed(unittest.TestCase):
    @unittest.skipUnless(HAVE_NUMPY and HAVE_CV2, "numpy + opencv required")
    def test_render_produces_an_image(self):
        from vision import synthetic

        frame = synthetic.render(
            {
                "person": True,
                "seated": True,
                "neck_angle": 30.0,
                "torso_angle": 12.0,
                "score": 0.4,
                "state": "slouching",
            }
        )
        self.assertEqual(frame.shape, (480, 640, 3))

    @unittest.skipUnless(HAVE_NUMPY and HAVE_CV2, "numpy + opencv required")
    def test_render_handles_away(self):
        from vision import synthetic

        frame = synthetic.render({"person": False, "seated": False, "score": None})
        self.assertEqual(frame.shape, (480, 640, 3))


if __name__ == "__main__":
    unittest.main()
