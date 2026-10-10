"""The vision module proper: frame -> :class:`Reading`.

Also owns sit-down detection (thigh close to horizontal for ~2 s) and the
temporary "away" state used when a desk hides the knee.
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
    ):
        self.landmarker = landmarker
        self.tolerance = float(tolerance)
        self.seated_threshold = float(seated_threshold)
        self.seated_delay = float(seated_delay)
        self.standup_delay = float(standup_delay)
        self.min_visibility = float(min_visibility)

        self.baseline = dict(DEFAULT_BASELINE)
        self.calibrated = False
        self._ema = angles.Ema(alpha)

        self.seated = False
        self._seated_since: Optional[float] = None
        self._away_since: Optional[float] = None
        self._hip_ref = None

    # -- baseline ---------------------------------------------------------
    def set_baseline(self, neck: float, torso: float) -> None:
        self.baseline = {"neck": float(neck), "torso": float(torso)}
        self.calibrated = True
        # Calibration is a step change: don't let the old average linger.
        self._ema.reset()

    # -- per-frame --------------------------------------------------------
    def process(self, frame, ts: Optional[float] = None) -> Dict[str, object]:
        ts = float(ts if ts is not None else time.time())

        landmarks = self.landmarker.detect(frame) if self.landmarker else None
        if landmarks is None:
            self._seated_since = None
            self.seated = False
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

        seated = self._update_seated(ts, thigh, torso, hip)
        reading = self._reading(ts, person=True, seated=seated, neck=neck, torso=torso, thigh=thigh)
        reading["points"] = {
            "ear": (ear[0], ear[1]),
            "shoulder": (shoulder[0], shoulder[1]),
            "hip": (hip[0], hip[1]),
            "knee": (knee[0], knee[1]),
        }
        reading["side"] = landmarks.get("side", "right")
        return reading

    # -- sit-down detection ----------------------------------------------
    def _update_seated(self, ts: float, thigh: Optional[float], torso: float, hip) -> bool:
        if thigh is not None:
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
            }

        neck_score = angles.posture_score(neck, self.baseline["neck"], self.tolerance)
        torso_score = angles.posture_score(torso, self.baseline["torso"], self.tolerance)
        raw = angles.combine_scores(neck_score, torso_score)
        smoothed = self._ema.update(raw)
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

        neck = reading.get("neck_angle")
        torso = reading.get("torso_angle")
        label = []
        if neck is not None:
            label.append(f"neck {neck:.0f}deg")
        if torso is not None:
            label.append(f"torso {torso:.0f}deg")
        if score is not None:
            label.append(f"score {score:.2f}")
        if label:
            mid = ((ear[0] + shoulder[0]) // 2, (ear[1] + shoulder[1]) // 2 - 12)
            cv2.putText(frame, "  ".join(label), mid, cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

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
