import math
import unittest

from vision import angles


class TestAngles(unittest.TestCase):
    def test_vertical_angle_is_zero(self):
        self.assertAlmostEqual(angles.neck_angle((100, 100), (100, 200)), 0.0, places=3)
        self.assertAlmostEqual(angles.torso_angle((100, 200), (100, 300)), 0.0, places=3)

    def test_known_neck_angle(self):
        # 10 px forward over 100 px down -> atan(0.1) ~= 5.71 degrees
        got = angles.neck_angle((110, 100), (100, 200))
        self.assertAlmostEqual(got, math.degrees(math.atan2(10, 100)), places=3)

    def test_ear_below_shoulder_is_clamped(self):
        # Defensive: a wild landmark should not produce > 90 degrees.
        self.assertLessEqual(angles.neck_angle((100, 300), (100, 200)), 90.0)

    def test_thigh_from_horizontal(self):
        self.assertAlmostEqual(angles.thigh_angle((100, 300), (200, 300)), 0.0, places=3)
        self.assertAlmostEqual(angles.thigh_angle((100, 300), (100, 400)), 90.0, places=3)

    def test_posture_score(self):
        self.assertAlmostEqual(angles.posture_score(10, baseline=10, tolerance=20), 1.0)
        self.assertAlmostEqual(angles.posture_score(20, baseline=10, tolerance=20), 0.5)
        self.assertAlmostEqual(angles.posture_score(40, baseline=10, tolerance=20), 0.0)
        # Straighter than baseline is still a perfect score.
        self.assertAlmostEqual(angles.posture_score(5, baseline=10, tolerance=20), 1.0)

    def test_combine_scores_takes_worse(self):
        self.assertAlmostEqual(angles.combine_scores(0.9, 0.4), 0.4)

    def test_head_pitch_is_positive_when_looking_down(self):
        # eye level with the ear
        self.assertAlmostEqual(angles.head_pitch((150, 100), (100, 100)), 0.0, places=3)
        # eye below the ear -> looking down
        self.assertGreater(angles.head_pitch((150, 140), (100, 100)), 30.0)
        # eye above the ear -> never reads as "down"
        self.assertLessEqual(angles.head_pitch((150, 60), (100, 100)), 0.0)

    def test_ema_smoothing(self):
        ema = angles.Ema(alpha=0.2)
        self.assertAlmostEqual(ema.update(0.0), 0.0)
        self.assertAlmostEqual(ema.update(1.0), 0.2)
        self.assertAlmostEqual(ema.update(1.0), 0.36)
        ema.reset()
        self.assertIsNone(ema.value)
        self.assertAlmostEqual(ema.update(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
