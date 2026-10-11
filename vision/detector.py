"""The vision module proper: frame -> :class:`Reading`.

Also owns sit-down detection (thigh close to horizontal for ~2 s), the
temporary "away" state used when a desk hides the knee, and sleep detection
(slumped, motionless, head down for a while).
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from . import angles

#: Default baseline used until the user has calibrated.
DEFAULT_BASELINE = {"neck": 12.0, "torso": 4.0}


class PostureDetector:
    """Turns one frame into one reading (see :mod:`vision`)."""

    def __init__(
        self,
        landmarker=None,
        tolerance: float = 20.0,
        alpha: float = 0.2,
        seated_threshold: float = 30.0,
        seated_delay: float = 2.0,
        standup_delay: float = 1.0,
        min_visibility: float = 0.3,
        sleep_score: float = 0.35,
        sleep_stillness: float = 0.006,
        sleep_head_delta: float = 20.0,
        sleep_head_fallback: float = 45.0,
        force_seated: bool = False,
    ):
        self.landmarker = landmarker
        self.tolerance = float(tolerance)
        self.alpha = float(alpha)
        self.seated_threshold = float(seated_threshold)
        self.seated_delay = float(seated_delay)
        #: Treat any detected person as seated (``run.py --force-seated``), for
        #: setups where the camera cannot judge the thigh angle.
        self.force_seated = bool(force_seated)
        self.standup_delay = float(standup_delay)
        self.min_visibility = float(min_visibility)
        #: Collapsed posture: the score at or below which a slump is a slump.
        self.sleep_score = float(sleep_score)
        #: Movement per frame (normalized landmark units) below which they count
        #: as motionless. Sleep is the still kind of collapse; awake slouching
        #: fidgets.
        self.sleep_stillness = float(sleep_stillness)
        #: Head tilt past the calibrated head level that counts as a dropped head.
        self.sleep_head_delta = float(sleep_head_delta)
        #: Absolute tilt used only until a head reference has been measured.
        self.sleep_head_fallback = float(sleep_head_fallback)

        self.baseline = dict(DEFAULT_BASELINE)
        self.calibrated = False
        self._ema = angles.Ema(alpha)

        self.seated = False
        self._seated_since: Optional[float] = None
        self._away_since: Optional[float] = None
        self._hip_ref = None
        #: Smoothed joint movement per frame, the stillness half of sleep.
        self._motion = 0.0
        self._prev_points: Optional[Dict[str, tuple]] = None
        #: Head tilt recorded during calibration, so a dropped head is measured
        #: against this person and this camera rather than a guessed angle.
        self.head_down_baseline: Optional[float] = None
        self._head_calib_until: Optional[float] = None
        self._head_samples: list = []

    # -- baseline ---------------------------------------------------------
    def set_baseline(self, neck: float, torso: float) -> None:
        self.baseline = {"neck": float(neck), "torso": float(torso)}
        self.calibrated = True
        # Calibration is a step change: don't let the old average linger.
        self._ema.reset()

    def start_head_calibration(self, ts: float, duration: float) -> None:
        """Record the head tilt of "sitting up straight" for a few seconds.

        Without this a dropped head would need an absolute angle, which depends
        on the camera height, the chair and the person. Relative to a measured
        level it needs no guessing.
        """
        self._head_calib_until = float(ts) + max(0.5, float(duration))
        self._head_samples = []
        self.head_down_baseline = None

    def _finish_head_calibration(self, ts: float) -> None:
        if self._head_samples:
            self.head_down_baseline = sum(self._head_samples) / len(self._head_samples)
        self._head_calib_until = None
        self._head_samples = []

    # -- per-frame --------------------------------------------------------
    def process(self, frame, ts: Optional[float] = None) -> Dict[str, object]:
        ts = float(ts if ts is not None else time.time())

        landmarks = self.landmarker.detect(frame) if self.landmarker else None
        if landmarks is None:
            self._seated_since = None
            self.seated = False
            self._prev_points = None
            self._motion = 0.0
            return self._reading(ts, person=False, seated=False, neck=None, torso=None, thigh=None)

        ear, shoulder, hip, knee = (
            landmarks["ear"],
            landmarks["shoulder"],
            landmarks["hip"],
            landmarks["knee"],
        )

        neck = angles.neck_angle(ear, shoulder)
        torso = angles.torso_angle(shoulder, hip)
        thigh = angles.thigh_angle(hip, knee) if knee[2] >= self.min_visibility else None

        eye = landmarks.get("eye")
        head_down = angles.head_pitch(eye, ear) if eye is not None and eye[2] >= self.min_visibility else None
        if head_down is not None and self._head_calib_until is not None:
            if ts <= self._head_calib_until:
                self._head_samples.append(head_down)
            else:
                self._finish_head_calibration(ts)

        points = {
            "ear": (ear[0], ear[1]),
            "shoulder": (shoulder[0], shoulder[1]),
            "hip": (hip[0], hip[1]),
            "knee": (knee[0], knee[1]),
        }
        motion = self._update_motion(points)

        seated = self._update_seated(ts, thigh, torso, hip)
        reading = self._reading(ts, person=True, seated=seated, neck=neck, torso=torso, thigh=thigh)
        reading["points"] = points
        reading["side"] = landmarks.get("side", "right")
        reading["head_down_deg"] = round(head_down, 1) if head_down is not None else None
        reading["head_ref_deg"] = (
            round(self.head_down_baseline, 1) if self.head_down_baseline is not None else None
        )
        reading["head_drop_deg"] = (
            round(head_down - self.head_down_baseline, 1)
            if head_down is not None and self.head_down_baseline is not None
            else None
        )
        reading["motion"] = round(motion, 4)
        reading["sleepy"] = self._is_sleepy(reading.get("score"), motion, head_down)
        return reading

    def _update_motion(self, points: Dict[str, tuple]) -> float:
        """How much the joints moved since the last frame, smoothed.

        Landmarks are normalized to 0..1, so this number means the same thing on
        any camera. Stillness is what separates a sleeping slump from an awake
        one, so it is measured rather than assumed.
        """
        previous = self._prev_points
        self._prev_points = dict(points)
        if not previous:
            self._motion = 0.0
            return 0.0
        total = 0.0
        counted = 0
        for key, (x, y) in points.items():
            old = previous.get(key)
            if not old:
                continue
            total += abs(x - old[0]) + abs(y - old[1])
            counted += 1
        if not counted:
            self._motion = 0.0
            return 0.0
        moved = total / counted
        self._motion += self.alpha * (moved - self._motion)
        return self._motion

    def _is_sleepy(self, score: Optional[float], motion: float, head_down: Optional[float]) -> bool:
        """Slumped, motionless and head down: the shape of a nap in a chair.

        Honest about its limits: pose alone cannot see closed eyes, so this is
        really "collapsed and not moving for a while". Someone wide awake and
        perfectly still while reading deeply will also look like this, which is
        why the state machine waits ``sleep_threshold_seconds`` before shouting.
        """
        if score is None or score > self.sleep_score:
            return False
        if motion is None or motion > self.sleep_stillness:
            return False
        if head_down is None:
            # No eye landmarks at all: a head dropped out of frame is itself a
            # strong signal, so do not hold the collapse back for a missing eye.
            return True
        if self.head_down_baseline is not None:
            return (head_down - self.head_down_baseline) >= self.sleep_head_delta
        return head_down >= self.sleep_head_fallback

    # -- sit-down detection ----------------------------------------------
    def _update_seated(self, ts: float, thigh: Optional[float], torso: float, hip) -> bool:
        if self.force_seated:
            candidate = True
        elif thigh is not None:
            candidate = thigh < self.seated_threshold
        else:
            # Desk hides the knee: person present + hip parked in one spot +
            # torso upright enough.
            candidate = self._fallback_seated(hip, torso)

        if candidate:
            self._away_since = None
            if self._seated_since is None:
                self._seated_since = ts
            if not self.seated and (ts - self._seated_since) >= self.seated_delay:
                self.seated = True
        else:
            self._seated_since = None
            if self.seated:
                if self._away_since is None:
                    self._away_since = ts
                if (ts - self._away_since) >= self.standup_delay:
                    self.seated = False
        return self.seated

    def _fallback_seated(self, hip, torso: float) -> bool:
        if hip[2] < self.min_visibility or torso > 45.0:
            self._hip_ref = None
            return False
        if self._hip_ref is None:
            self._hip_ref = (hip[0], hip[1])
            return False
        moved = abs(hip[0] - self._hip_ref[0]) + abs(hip[1] - self._hip_ref[1])
        return moved < 25.0

    # -- reading ----------------------------------------------------------
    def _reading(self, ts, *, person, seated, neck, torso, thigh) -> Dict[str, object]:
        if neck is None or torso is None:
            return {
                "ts": ts,
                "person": person,
                "seated": seated,
                "neck_angle": None,
                "torso_angle": None,
                "score": None,
                "raw_score": None,
                "calibrated": self.calibrated,
                "thigh_angle": None,
                "neck_drift": None,
                "torso_drift": None,
                "slouch_part": None,
                "head_down_deg": None,
                "head_ref_deg": None,
                "head_drop_deg": None,
                "motion": None,
                "sleepy": False,
            }

        neck_score = angles.posture_score(neck, self.baseline["neck"], self.tolerance)
        torso_score = angles.posture_score(torso, self.baseline["torso"], self.tolerance)
        raw = angles.combine_scores(neck_score, torso_score)
        smoothed = self._ema.update(raw)

        # How far each part has drifted past *this person's own* upright
        # baseline, and which part is actually giving way. The roasts use this
        # so the insult names the real failure (head forward vs back hunching).
        neck_drift = max(0.0, neck - self.baseline["neck"])
        torso_drift = max(0.0, torso - self.baseline["torso"])
        if neck_drift > torso_drift + 3.0:
            part = "neck"
        elif torso_drift > neck_drift + 3.0:
            part = "torso"
        else:
            part = "even"

        return {
            "ts": ts,
            "person": person,
            "seated": seated,
            "neck_angle": round(neck, 2),
            "torso_angle": round(torso, 2),
            "score": round(smoothed, 3),
            "raw_score": round(raw, 3),
            "calibrated": self.calibrated,
            "thigh_angle": round(thigh, 2) if thigh is not None else None,
            "neck_drift": round(neck_drift, 2),
            "torso_drift": round(torso_drift, 2),
            "slouch_part": part,
            "head_down_deg": None,
            "head_ref_deg": None,
            "head_drop_deg": None,
            "motion": None,
            "sleepy": False,
        }

    # -- overlay ----------------------------------------------------------
    def draw_overlay(self, frame, reading: Dict[str, object], lower: float = 0.7, upper: float = 0.8):
        """Draw the ear-shoulder and shoulder-hip lines plus live angles.

        This overlay is both the debugging tool and the centerpiece of the
        website's live feed.
        """
        import cv2

        points = reading.get("points")
        if not points:
            cv2.putText(frame, "no pose", (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 180), 2)
            return frame

        score = reading.get("score")
        if score is None:
            color = (180, 180, 180)
        elif score >= upper:
            color = (80, 220, 120)
        elif score >= lower:
            color = (60, 200, 240)
        else:
            color = (80, 80, 240)

        def ip(name):
            x, y = points[name]
            return int(x), int(y)

        ear, shoulder, hip, knee = ip("ear"), ip("shoulder"), ip("hip"), ip("knee")
        cv2.line(frame, ear, shoulder, color, 3, cv2.LINE_AA)
        cv2.line(frame, shoulder, hip, color, 3, cv2.LINE_AA)
        cv2.line(frame, hip, knee, (150, 150, 150), 2, cv2.LINE_AA)
        for pt in (ear, shoulder, hip, knee):
            cv2.circle(frame, pt, 5, color, -1, cv2.LINE_AA)

        state = reading.get("state")
        if state:
            cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (25, 25, 30), -1)
            cv2.putText(
                frame,
                f"{str(state).upper()}",
                (12, 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
                cv2.LINE_AA,
            )
        return frame
