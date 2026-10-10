import unittest
from typing import Dict, List

from server.state_machine import (
    AWAY,
    CALIBRATING,
    GOOD,
    ROASTED,
    SLOUCHING,
    WARNED,
    PostureStateMachine,
    StateMachineConfig,
)


def reading(ts, score, *, person=True, seated=True, neck=15.0, torso=6.0) -> Dict:
    return {
        "ts": float(ts),
        "person": person,
        "seated": seated,
        "neck_angle": neck,
        "torso_angle": torso,
        "score": score,
    }


def feed(machine, start, seconds, score, **kwargs) -> List[Dict]:
    events: List[Dict] = []
    for i in range(int(seconds)):
        events += machine.update(reading(start + i, score, **kwargs))
    return events


def event_names(events) -> List[str]:
    return [e["event"] for e in events]


def with_session(machine, t0=0.0, good_until=10.0):
    """Get to a calibrated GOOD state at t0 + calibration_seconds."""
    machine.update(reading(t0, 0.95, neck=12.0, torso=4.0))
    for i in range(1, int(good_until) + 1):
        machine.update(reading(t0 + i, 0.95, neck=12.0, torso=4.0))
    return t0 + good_until


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.cfg = StateMachineConfig()
        self.machine = PostureStateMachine(self.cfg)

    def test_away_until_seated(self):
        self.machine.update(reading(0, score=None, person=False, seated=False))
        self.assertEqual(self.machine.state, AWAY)
        # A present but standing person is also "away".
        self.machine.update(reading(1, 0.95, seated=False))
        self.assertEqual(self.machine.state, AWAY)

    def test_session_starts_and_calibrates(self):
        events = self.machine.update(reading(0, 0.95, neck=12.0, torso=4.0))
        self.assertIn("session_start", event_names(events))
        self.assertEqual(self.machine.state, CALIBRATING)

        for i in range(1, 7):
            events += self.machine.update(reading(i, 0.95, neck=12.0, torso=4.0))
        self.assertIn("calibrated", event_names(events))
        self.assertIn("good", event_names(events))
        self.assertEqual(self.machine.state, GOOD)
        baseline = self.machine._calibration_result()
        self.assertAlmostEqual(baseline["neck"], 12.0, places=6)
        self.assertAlmostEqual(baseline["torso"], 4.0, places=6)

    def test_warn_then_roast_timing(self):
        start = with_session(self.machine)
        self.assertEqual(self.machine.state, GOOD)

        events = []
        events += feed(self.machine, start, 10, 0.5)
        self.assertEqual(self.machine.state, SLOUCHING)
        self.assertNotIn("warn", event_names(events))

        events = feed(self.machine, start + 10, 1, 0.5)
        self.assertIn("warn", event_names(events))
        self.assertEqual(self.machine.state, WARNED)

        events = []
        events += feed(self.machine, start + 11, 19, 0.5)
        self.assertNotIn("roast_due", event_names(events))
        events += feed(self.machine, start + 30, 1, 0.5)
        due = [e for e in events if e["event"] == "roast_due"]
        self.assertEqual(len(due), 1)
        self.assertEqual(self.machine.state, ROASTED)
        self.assertEqual(due[0]["slouch_count"], 1)
        self.assertGreaterEqual(due[0]["slouch_seconds"], 30.0)

    def test_good_debounce_blocks_dodging(self):
        start = with_session(self.machine)
        feed(self.machine, start, 20, 0.5)  # slouching
        self.assertEqual(self.machine.state, WARNED)

        # Only 1 second of good posture: must NOT reset the slouch timer.
        feed(self.machine, start + 20, 1, 0.95)
        self.assertEqual(self.machine.state, WARNED)
        # Back to slouching; the episode continues, so the roast is still due.
        events = feed(self.machine, start + 21, 12, 0.5)
        self.assertIn("roast_due", event_names(events))

    def test_recovery_after_two_good_seconds(self):
        start = with_session(self.machine)
        feed(self.machine, start, 5, 0.5)
        self.assertEqual(self.machine.state, SLOUCHING)
        events = feed(self.machine, start + 5, 3, 0.95)
        self.assertIn("good", event_names(events))
        self.assertEqual(self.machine.state, GOOD)
        self.assertIsNone(self.machine.slouch_since)

    def test_cooldown_blocks_repeat_roasts(self):
        start = with_session(self.machine)
        first = feed(self.machine, start, 31, 0.5)
        due = [e for e in first if e["event"] == "roast_due"]
        self.assertEqual(len(due), 1)
        first_roast_ts = due[0]["ts"]

        # Recover, then slouch again while the cooldown is still active.
        feed(self.machine, start + 31, 3, 0.95)
        self.assertEqual(self.machine.state, GOOD)

        events = feed(self.machine, start + 34, 60, 0.5)
        early = [e for e in events if e["event"] == "roast_due"]
        self.assertEqual(early, [])  # still inside the 90 s cooldown

        events = feed(self.machine, start + 94, 40, 0.5)
        later = [e for e in events if e["event"] == "roast_due"]
        self.assertEqual(len(later), 1)
        self.assertGreaterEqual(later[0]["ts"], first_roast_ts + self.cfg.cooldown_seconds)

    def test_only_one_roast_per_slouch_episode(self):
        start = with_session(self.machine)
        events = feed(self.machine, start, 200, 0.5)
        due = [e for e in events if e["event"] == "roast_due"]
        self.assertEqual(len(due), 1)

    def test_hysteresis_band_does_not_flip_state(self):
        start = with_session(self.machine)
        feed(self.machine, start, 3, 0.75)  # between lower (0.7) and upper (0.8)
        self.assertEqual(self.machine.state, GOOD)
        feed(self.machine, start + 3, 3, 0.5)
        self.assertEqual(self.machine.state, SLOUCHING)
        feed(self.machine, start + 6, 3, 0.75)
        self.assertEqual(self.machine.state, SLOUCHING)  # needs >= 0.8 to recover

    def test_standing_up_ends_session(self):
        start = with_session(self.machine)
        events = self.machine.update(reading(start, 0.95, person=False, seated=False))
        names = event_names(events)
        self.assertIn("session_end", names)
        self.assertEqual(self.machine.state, AWAY)
        end = [e for e in events if e["event"] == "session_end"][0]
        self.assertEqual(end["reason"], "left")

    def test_no_roast_while_away(self):
        start = with_session(self.machine)
        feed(self.machine, start, 40, 0.5)
        self.machine.update(reading(start + 40, None, person=False, seated=False))
        events = self.machine.update(reading(start + 41, 0.5, person=False, seated=False))
        self.assertNotIn("roast_due", event_names(events))

    def test_manual_calibration_request(self):
        start = with_session(self.machine)
        feed(self.machine, start, 2, 0.5)
        self.assertTrue(self.machine.request_calibration())
        events = []
        for i in range(0, 7):
            events += self.machine.update(reading(start + 2 + i, 0.95, neck=10.0, torso=3.0))
        self.assertIn("calibrated", event_names(events))
        self.assertEqual(self.machine.state, GOOD)

    def test_snapshot_fields(self):
        start = with_session(self.machine)
        feed(self.machine, start, 15, 0.5)
        snap = self.machine.snapshot(start + 15)
        for key in ("state", "slouch_count", "roasts_due", "worst_neck_angle", "cooldown_remaining"):
            self.assertIn(key, snap)
        self.assertEqual(snap["state"], WARNED)
        self.assertGreater(snap["slouch_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
