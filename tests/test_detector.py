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
        "shoulder": (100.0, 200.0, 1.0),
        "hip": (100.0, 300.0, 1.0),
        "knee": (200.0, 310.0, 1.0),
    }


def slouching():
    # Head juts 50 px forward over 90 px: ~29 degrees.
    return {
        "side": "right",
        "ear": (150.0, 110.0, 1.0),
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
