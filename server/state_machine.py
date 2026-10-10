"""The posture state machine.

Vision only reports readings; voice only does what it is told.  Every decision
about *when to roast* lives here.

States and thresholds follow the build guide::

    away -> (seated ~2 s) -> calibrating -> good
    good -> slouching (score < 0.7)
    slouching -> warned (10 s)
    warned -> roasted (30 s, if no cooldown is active)
    any -> good again after ~2 s of good posture (debounced)

Events (the machine -> voice / website contract)::

    session_start, calibrated, good, warn, roast_due, session_end

``roast_due`` carries the session context used to write escalating roasts::

    {"event": "roast_due", "slouch_count": 5, "session_minutes": 23,
     "worst_neck_angle": 41}
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

AWAY = "away"
CALIBRATING = "calibrating"
GOOD = "good"
SLOUCHING = "slouching"
WARNED = "warned"
ROASTED = "roasted"

STATES = (AWAY, CALIBRATING, GOOD, SLOUCHING, WARNED, ROASTED)


@dataclass
class StateMachineConfig:
    """Live-tunable thresholds (the website edits these)."""

    warn_seconds: float = 10.0
    roast_seconds: float = 30.0
    cooldown_seconds: float = 90.0
    good_debounce_seconds: float = 2.0
    calibration_seconds: float = 5.0
    lower_threshold: float = 0.7
    upper_threshold: float = 0.8


class PostureStateMachine:
    def __init__(self, config: Optional[StateMachineConfig] = None):
        self.config = config or StateMachineConfig()
        self.state = AWAY
        self.state_since: Optional[float] = None
        self.session_started_at: Optional[float] = None

        self.slouch_since: Optional[float] = None
        self.good_since: Optional[float] = None
        self.hysteresis_slouch = False
        self.warned = False
        self.roasted = False

        self.slouch_count = 0
        self.roasts_due = 0
        self.worst_neck_angle = 0.0
        self.cooldown_until = 0.0
        self.last_reading: Optional[Dict] = None

        self._calibration_requested = False
        self._calib_samples: List[tuple] = []
        self._calib_started: Optional[float] = None

    # ------------------------------------------------------------------ API
    def update(self, reading: Dict) -> List[Dict]:
        """Advance the machine by one reading; return the events it produced."""
        raw_ts = reading.get("ts")
        ts = float(raw_ts) if raw_ts is not None else time.time()
        self.last_reading = reading
        events: List[Dict] = []

        person = bool(reading.get("person"))
        seated = bool(reading.get("seated"))
        score = reading.get("score")
        score = float(score) if score is not None else None
        neck = reading.get("neck_angle")
        neck = float(neck) if neck is not None else 0.0
        torso = reading.get("torso_angle")
        torso = float(torso) if torso is not None else 0.0

        # -- away: nothing is ever roasted while away --------------------
        if not person or not seated:
            if self.state != AWAY:
                events.append(self._end_session(ts, reason="left"))
            self._reset_episode()
            self._transition(ts, AWAY, events)
            return events

        # -- session start -> calibrating --------------------------------
        if self.state == AWAY:
            self.session_started_at = ts
            self.slouch_count = 0
            self.roasts_due = 0
            self.worst_neck_angle = 0.0
            self.cooldown_until = 0.0
            events.append(
                {
                    "event": "session_start",
                    "ts": ts,
                    "session_minutes": 0.0,
                    "calibration_seconds": self.config.calibration_seconds,
                    "state": CALIBRATING,
                }
            )
            self._transition(ts, CALIBRATING, events)
            self._start_calibration(ts)

        # -- manual recalibration ----------------------------------------
        elif self._calibration_requested and self.state != CALIBRATING:
            self._calibration_requested = False
            events.append({"event": "calibrating", "ts": ts, "state": CALIBRATING})
            self._transition(ts, CALIBRATING, events)
            self._start_calibration(ts)

        # -- calibrating --------------------------------------------------
        if self.state == CALIBRATING:
            if self._add_calibration_sample(ts, neck, torso):
                baseline = self._calibration_result()
                events.append(
                    {
                        "event": "calibrated",
                        "ts": ts,
                        "neck_angle": round(baseline["neck"], 2),
                        "torso_angle": round(baseline["torso"], 2),
                        "neck_baseline": round(baseline["neck"], 2),
                        "torso_baseline": round(baseline["torso"], 2),
                        "samples": baseline["samples"],
                    }
                )
                self._transition(ts, GOOD, events)
                self.good_since = ts
                events.append(
                    {
                        "event": "good",
                        "ts": ts,
                        "session_minutes": self._session_minutes(ts),
                        "reason": "calibrated",
                        "state": GOOD,
                    }
                )
            return events

        # -- hysteresis ---------------------------------------------------
        if score is None:
            return events
        if score < self.config.lower_threshold:
            self.hysteresis_slouch = True
        elif score >= self.config.upper_threshold:
            self.hysteresis_slouch = False

        if self.hysteresis_slouch:
            events.extend(self._slouching(ts, neck, score))
        else:
            events.extend(self._recovering(ts))
        return events

    def request_calibration(self) -> bool:
        """Ask for a recalibration on the next reading (called by Flask)."""
        if self.state == AWAY:
            return False
        self._calibration_requested = True
        return True

    def snapshot(self, now: Optional[float] = None) -> Dict:
        now = float(now if now is not None else time.time())
        return {
            "state": self.state,
            "state_since": self.state_since,
            "session_started_at": self.session_started_at,
            "session_minutes": self._session_minutes(now) if self.session_started_at is not None else 0.0,
            "slouch_count": self.slouch_count,
            "roasts_due": self.roasts_due,
            "worst_neck_angle": round(self.worst_neck_angle, 1),
            "slouch_seconds": round(max(0.0, now - self.slouch_since), 1) if self.slouch_since else 0.0,
            "cooldown_remaining": round(max(0.0, self.cooldown_until - now), 1),
            "calibrating": self.state == CALIBRATING,
            "calibration_progress": (
                round(min(1.0, (now - self._calib_started) / max(self.config.calibration_seconds, 1e-6)), 3)
                if self.state == CALIBRATING and self._calib_started is not None
                else 0.0
            ),
        }

    # ------------------------------------------------------------- helpers
    def _slouching(self, ts: float, neck: float, score: float) -> List[Dict]:
        events: List[Dict] = []
        self.good_since = None

        if self.slouch_since is None:
            self.slouch_since = ts
            self.warned = False
            self.roasted = False
            self.slouch_count += 1
            self.worst_neck_angle = neck
            self._transition(ts, SLOUCHING, events)
        else:
            self.worst_neck_angle = max(self.worst_neck_angle, neck)

        elapsed = ts - self.slouch_since

        if elapsed >= self.config.warn_seconds and not self.warned:
            self.warned = True
            self._transition(ts, WARNED, events)
            events.append(
                {
                    "event": "warn",
                    "ts": ts,
                    "slouch_seconds": round(elapsed, 1),
                    "slouch_count": self.slouch_count,
                    "session_minutes": self._session_minutes(ts),
                    "worst_neck_angle": round(self.worst_neck_angle, 1),
                    "state": WARNED,
                }
            )

        # Cooldown blocks the roast, not the tracking: the state still updates.
        if elapsed >= self.config.roast_seconds and not self.roasted and ts >= self.cooldown_until:
            self.roasted = True
            self.roasts_due += 1
            self.cooldown_until = ts + self.config.cooldown_seconds
            self._transition(ts, ROASTED, events)
            events.append(
                {
                    "event": "roast_due",
                    "ts": ts,
                    "slouch_count": self.slouch_count,
                    "session_minutes": self._session_minutes(ts),
                    "worst_neck_angle": round(self.worst_neck_angle, 1),
                    "slouch_seconds": round(elapsed, 1),
                    "score": round(score, 3),
                    "state": ROASTED,
                }
            )
        return events

    def _recovering(self, ts: float) -> List[Dict]:
        events: List[Dict] = []
        if self.good_since is None:
            self.good_since = ts
        # Debounce the return to good so a brief sit-up cannot dodge the roast.
        if self.state in (SLOUCHING, WARNED, ROASTED) and (ts - self.good_since) >= self.config.good_debounce_seconds:
            self._reset_episode()
            self._transition(ts, GOOD, events)
            events.append(
                {
                    "event": "good",
                    "ts": ts,
                    "session_minutes": self._session_minutes(ts),
                    "reason": "posture_recovered",
                    "state": GOOD,
                }
            )
        return events

    def _reset_episode(self) -> None:
        self.slouch_since = None
        self.warned = False
        self.roasted = False
        self.hysteresis_slouch = False

    def _transition(self, ts: float, new_state: str, events: List[Dict]) -> None:
        if new_state == self.state:
            return
        prev = self.state
        self.state = new_state
        self.state_since = ts
        events.append({"event": "state", "ts": ts, "state": new_state, "prev": prev})

    def _end_session(self, ts: float, reason: str) -> Dict:
        event = {
            "event": "session_end",
            "ts": ts,
            "reason": reason,
            "session_minutes": self._session_minutes(ts),
            "slouch_count": self.slouch_count,
            "roasts": self.roasts_due,
            "state": AWAY,
        }
        self.session_started_at = None
        return event

    def _session_minutes(self, ts: float) -> float:
        if self.session_started_at is None:
            return 0.0
        return round(max(0.0, (ts - self.session_started_at) / 60.0), 1)

    # -- calibration -----------------------------------------------------
    def _start_calibration(self, ts: float) -> None:
        self._calib_started = ts
        self._calib_samples = []

    def _add_calibration_sample(self, ts: float, neck: float, torso: float) -> bool:
        self._calib_samples.append((neck, torso))
        started = self._calib_started if self._calib_started is not None else ts
        elapsed = ts - started
        return elapsed >= self.config.calibration_seconds and len(self._calib_samples) >= 5

    def _calibration_result(self) -> Dict:
        if not self._calib_samples:
            return {"neck": 0.0, "torso": 0.0, "samples": 0}
        n = len(self._calib_samples)
        return {
            "neck": sum(s[0] for s in self._calib_samples) / n,
            "torso": sum(s[1] for s in self._calib_samples) / n,
            "samples": n,
        }
