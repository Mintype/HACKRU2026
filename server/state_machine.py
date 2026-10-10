"""The posture state machine.

Vision only reports readings; voice only does what it is told.  Every decision
about *when to roast* lives here.

States and thresholds follow the build guide::

    away -> (seated ~2 s) -> calibrating -> good
    good -> slouching (score < 0.7)
    slouching -> roasted (10 s, if no cooldown is active)
    any -> good again after ~2 s of good posture (debounced)

Events (the machine -> voice / website contract)::

    session_start, calibrated, good, roast_due, session_end

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
ROASTED = "roasted"

STATES = (AWAY, CALIBRATING, GOOD, SLOUCHING, ROASTED)


@dataclass
class StateMachineConfig:
    """Live-tunable thresholds (the website edits these)."""

    roast_seconds: float = 10.0
    cooldown_seconds: float = 90.0
    good_debounce_seconds: float = 2.0
    calibration_seconds: float = 5.0
    lower_threshold: float = 0.7
    upper_threshold: float = 0.8
    #: Score below which posture counts as properly bad, so it roasts without
    #: waiting out ``roast_seconds``. 0 disables the score trigger.
    bad_score_threshold: float = 0.45
    #: The bad score must hold this long, so one noisy frame cannot fire an API call.
    bad_score_seconds: float = 3.0


class PostureStateMachine:
    def __init__(self, config: Optional[StateMachineConfig] = None):
        self.config = config or StateMachineConfig()
        self.state = AWAY
        self.state_since: Optional[float] = None
        self.session_started_at: Optional[float] = None

        self.slouch_since: Optional[float] = None
        self.good_since: Optional[float] = None
        self.bad_since: Optional[float] = None
        self.hysteresis_slouch = False
        self.roasted = False

        self.slouch_count = 0
        self.roasts_due = 0
        self.worst_neck_angle = 0.0
        self.worst_torso_angle = 0.0
        self.slouch_part = "even"
        self._part_votes: Dict[str, int] = {"neck": 0, "torso": 0, "even": 0}
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
            self.worst_torso_angle = 0.0
            self.slouch_part = "even"
            self._part_votes = {"neck": 0, "torso": 0, "even": 0}
            self.bad_since = None
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
            events.extend(self._slouching(ts, reading))
        else:
            events.extend(self._recovering(ts))
        return events

    def request_calibration(self) -> bool:
        """Ask for a recalibration on the next reading (called by Flask)."""
        if self.state == AWAY:
            return False
        self._calibration_requested = True
        return True

    def time_to_roast(self, now: Optional[float] = None) -> Dict:
        """How long until the next roast, and what will cause it.

        A debugging aid: the website and ``run.py --timers`` show this ticking
        down, so you can see which trigger is about to fire and whether the
        cooldown is holding it back.
        """
        now = float(now if now is not None else time.time())
        empty = {"seconds": None, "trigger": None, "blocked_by": None, "ready": False}
        if self.slouch_since is None:
            return empty

        elapsed = now - self.slouch_since
        waits: List[tuple] = []
        if not self.roasted:
            waits.append(("sustained_slouch", max(0.0, self.config.roast_seconds - elapsed)))
        if self.config.bad_score_threshold > 0 and self.bad_since is not None:
            waits.append(
                ("bad_posture", max(0.0, self.config.bad_score_seconds - (now - self.bad_since)))
            )
        if not waits:
            return empty

        trigger, wait = min(waits, key=lambda item: item[1])
        cooldown = max(0.0, self.cooldown_until - now)
        return {
            "seconds": round(max(wait, cooldown), 1),
            "trigger": trigger,
            "blocked_by": "cooldown" if cooldown > wait else None,
            "ready": max(wait, cooldown) <= 0.0,
        }

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
            "worst_torso_angle": round(self.worst_torso_angle, 1),
            "slouch_part": self.slouch_part,
            "slouch_seconds": round(max(0.0, now - self.slouch_since), 1) if self.slouch_since else 0.0,
            "bad_seconds": round(max(0.0, now - self.bad_since), 1) if self.bad_since else 0.0,
            "cooldown_remaining": round(max(0.0, self.cooldown_until - now), 1),
            "next_roast": self.time_to_roast(now),
            "calibrating": self.state == CALIBRATING,
            "calibration_progress": (
                round(min(1.0, (now - self._calib_started) / max(self.config.calibration_seconds, 1e-6)), 3)
                if self.state == CALIBRATING and self._calib_started is not None
                else 0.0
            ),
        }

    # ------------------------------------------------------------- helpers
    def _slouching(self, ts: float, reading: Dict) -> List[Dict]:
        events: List[Dict] = []
        self.good_since = None

        neck = float(reading.get("neck_angle") or 0.0)
        torso = float(reading.get("torso_angle") or 0.0)
        score = reading.get("score")
        score = float(score) if score is not None else 0.0
        part = str(reading.get("slouch_part") or "even")

        if self.slouch_since is None:
            self.slouch_since = ts
            self.roasted = False
            self.slouch_count += 1
            self.worst_neck_angle = neck
            self.worst_torso_angle = torso
            self.bad_since = ts
            self._part_votes = {"neck": 0, "torso": 0, "even": 0}
            self._transition(ts, SLOUCHING, events)
        else:
            self.worst_neck_angle = max(self.worst_neck_angle, neck)
            self.worst_torso_angle = max(self.worst_torso_angle, torso)

        # Which part keeps giving way over this episode; that is what the roast
        # should be about.
        if part in self._part_votes:
            self._part_votes[part] += 1
        self.slouch_part = max(self._part_votes, key=self._part_votes.get)

        elapsed = ts - self.slouch_since

        # Score-based trigger: proper bad posture roasts now rather than waiting
        # out roast_seconds. It has to hold for bad_score_seconds first, so a
        # single noisy reading cannot fire it.
        if self.config.bad_score_threshold > 0 and score < self.config.bad_score_threshold:
            if self.bad_since is None:
                self.bad_since = ts
        else:
            self.bad_since = None

        # Cooldown blocks roasts, not tracking: the state still updates.
        fired = False
        if elapsed >= self.config.roast_seconds and not self.roasted and ts >= self.cooldown_until:
            events.extend(self._fire_roast(ts, elapsed, score, "sustained_slouch"))
            fired = True
        if (
            not fired
            and self.bad_since is not None
            and (ts - self.bad_since) >= self.config.bad_score_seconds
            and ts >= self.cooldown_until
        ):
            events.extend(self._fire_roast(ts, elapsed, score, "bad_posture"))
        return events

    def _fire_roast(self, ts: float, elapsed: float, score: float, reason: str) -> List[Dict]:
        """Emit ``roast_due`` and start the cooldown that rate-limits the next one."""
        self.roasted = True
        self.roasts_due += 1
        self.cooldown_until = ts + self.config.cooldown_seconds
        events: List[Dict] = []
        self._transition(ts, ROASTED, events)
        events.append(
            {
                "event": "roast_due",
                "ts": ts,
                "reason": reason,
                "slouch_count": self.slouch_count,
                "session_minutes": self._session_minutes(ts),
                "worst_neck_angle": round(self.worst_neck_angle, 1),
                "worst_torso_angle": round(self.worst_torso_angle, 1),
                "slouch_part": self.slouch_part,
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
        if self.state in (SLOUCHING, ROASTED) and (ts - self.good_since) >= self.config.good_debounce_seconds:
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
        self.roasted = False
        self.hysteresis_slouch = False
        self.bad_since = None

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
