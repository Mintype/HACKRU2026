"""Pure geometry for side-view posture measurement.

Deliberately dependency-free (no OpenCV / MediaPipe / NumPy) so the math and
the state machine can be unit-tested on any machine.

Image coordinates have ``y`` growing *downward*, which is why every helper
takes offsets already normalised to "up is positive".
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

Point = Tuple[float, float]


def _clamp_angle(angle: float) -> float:
    """Clamp to 0..90 degrees (a limb can never be more than horizontal)."""
    return max(0.0, min(90.0, angle))


def angle_from_vertical(dx: float, dy_up: float) -> float:
    """Angle of a vector from vertical, in degrees.

    ``dx`` is the horizontal offset and ``dy_up`` the vertical offset measured
    *upward* (positive when the second point is lower on screen than the first,
    because image ``y`` grows downward).

    ``atan2`` is used rather than ``atan(dx / dy)`` so a near-zero denominator
    cannot blow up.
    """
    return _clamp_angle(math.degrees(math.atan2(abs(dx), dy_up)))


def neck_angle(ear: Sequence[float], shoulder: Sequence[float]) -> float:
    """Forward-head angle: the ear-shoulder line from vertical.

    Grows as the head juts forward.
    """
    dx = float(ear[0]) - float(shoulder[0])
    dy_up = float(shoulder[1]) - float(ear[1])
    return angle_from_vertical(dx, dy_up)


def torso_angle(shoulder: Sequence[float], hip: Sequence[float]) -> float:
    """Hunch/lean angle: the shoulder-hip line from vertical."""
    dx = float(shoulder[0]) - float(hip[0])
    dy_up = float(hip[1]) - float(shoulder[1])
    return angle_from_vertical(dx, dy_up)


def thigh_angle(hip: Sequence[float], knee: Sequence[float]) -> float:
    """Thigh angle from *horizontal* -- small means the person is seated."""
    dx = abs(float(knee[0]) - float(hip[0]))
    dy = abs(float(knee[1]) - float(hip[1]))
    return _clamp_angle(math.degrees(math.atan2(dy, dx)))


def head_pitch(eye: Sequence[float], ear: Sequence[float]) -> float:
    """How far the head is tilted *down*, in degrees, signed.

    Measured from the ear→eye line against horizontal: the eye sits forward of
    the ear, so pitching the head down rotates the eye below it and the angle
    grows positive. Level is ~0; looking into a lap is tens of degrees.

    This is a heuristic, not object detection: it says "looking down for a long
    time", which is what a phone, a lap or a keyboard has in common. The raw
    value is shown on the Debug panel so the threshold can be tuned against a
    real camera instead of guessed.
    """
    dx = abs(float(eye[0]) - float(ear[0]))
    dy = float(eye[1]) - float(ear[1])  # positive when the eye is below the ear
    return max(-90.0, min(90.0, math.degrees(math.atan2(dy, dx))))


def posture_score(angle: float, baseline: float, tolerance: float = 20.0) -> float:
    """Map an angle to a 0..1 score relative to the calibrated baseline.

    ``1.0`` means "at or above the calibrated upright posture"; ``0.0`` means
    the angle drifted by ``tolerance`` degrees or more past the baseline.
    """
    if tolerance <= 0:
        return 1.0
    drift = max(0.0, float(angle) - float(baseline))
    return 1.0 - min(1.0, drift / float(tolerance))


def combine_scores(neck_score: float, torso_score: float) -> float:
    """Either kind of slouch should count, so take the worse one."""
    return min(neck_score, torso_score)


class Ema:
    """Exponential moving average used to keep one noisy frame from roasting."""

    def __init__(self, alpha: float = 0.2):
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = float(alpha)
        self.value: Optional[float] = None

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = float(sample)
        else:
            self.value = self.alpha * float(sample) + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None
