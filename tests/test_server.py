import tempfile
import unittest
from pathlib import Path

from fakes.fake_readings import scripted_readings
from server.db import Database
from server.settings import Settings
from server.state_machine import StateMachineConfig
from voice.pool import RoastPool


class TestFakeReadings(unittest.TestCase):
    def test_yields_well_formed_readings(self):
        gen = scripted_readings(start=1000.0, fps=10.0, cycles=1)
        readings = list(gen)
        self.assertGreater(len(readings), 100)
        for reading in readings:
            self.assertEqual(
                set(reading) >= {"ts", "person", "seated", "neck_angle", "torso_angle", "score"},
                True,
            )

    def test_starts_away_then_sits(self):
        readings = list(scripted_readings(start=0.0, fps=10.0, cycles=1))
        self.assertFalse(readings[0]["person"])
        later = [r for r in readings if r["person"]]
        self.assertTrue(later)
        self.assertTrue(later[0]["seated"])

    def test_timestamps_advance(self):
        readings = list(scripted_readings(start=0.0, fps=10.0, cycles=1))
        deltas = {round(readings[i + 1]["ts"] - readings[i]["ts"], 5) for i in range(50)}
        self.assertEqual(deltas, {0.1})

    def test_readings_report_the_failing_part(self):
        readings = list(scripted_readings(start=0.0, fps=10.0, cycles=1))
        self.assertTrue(all("slouch_part" in r for r in readings))
        parts = {r["slouch_part"] for r in readings}
        self.assertTrue(parts <= {"neck", "torso", "even", None})

    def test_scripted_readings_drive_a_full_session(self):
        from server.state_machine import PostureStateMachine

        machine = PostureStateMachine()
        events = []
        for reading in scripted_readings(start=0.0, fps=10.0, cycles=1):
            events += machine.update(reading)
        names = [e["event"] for e in events]
        self.assertIn("session_start", names)
        self.assertIn("calibrated", names)
        self.assertIn("warn", names)
        self.assertIn("roast_due", names)
        self.assertEqual(names.count("warn"), 3)
        self.assertEqual(machine.slouch_count, 3)
        self.assertEqual(machine.state, "good")


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.session = self.db.start_session(0.0)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_logs_reading_state_and_roast(self):
        for i in range(10):
            state = "good" if i < 7 else "slouching"
            self.db.log_reading(
                self.session,
                {"ts": float(i), "person": True, "seated": True, "neck_angle": 12 + i, "torso_angle": 4, "score": 1.0 if i < 7 else 0.4, "state": state},
            )
        self.db.log_state(self.session, 7.0, "slouching", "good")
        self.db.log_roast(self.session, 8.0, "Sit up.", "mild", "fallback", 1, 40.0)
        self.db.end_session(self.session, 10.0)

        stats = self.db.session_stats(self.session)
        self.assertEqual(stats["readings"], 10)
        self.assertAlmostEqual(stats["good_percent"], 70.0, places=1)
        self.assertEqual(stats["slouch_count"], 1)
        self.assertEqual(stats["roasts"], 1)
        self.assertGreater(stats["longest_good_streak"], 0)

        history = self.db.history(self.session)
        self.assertEqual(len(history), 10)
        roasts = self.db.session_roasts(self.session)
        self.assertEqual(roasts[0]["text"], "Sit up.")

    def test_stats_for_empty_session(self):
        stats = self.db.session_stats(self.session)
        self.assertEqual(stats["readings"], 0)
        self.assertEqual(stats["roasts"], 0)


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_validates_and_persists(self):
        settings = Settings(path=self.path)
        applied = settings.update({"warn_seconds": 12, "roast_seconds": "45", "audio_route": "cast", "unknown": 1})
        self.assertEqual(applied["warn_seconds"], 12.0)
        self.assertEqual(applied["roast_seconds"], 45.0)
        self.assertEqual(applied["audio_route"], "cast")
        self.assertNotIn("unknown", applied)

        reloaded = Settings(path=self.path)
        self.assertEqual(reloaded.get("roast_seconds"), 45.0)
        self.assertEqual(reloaded.get("audio_route"), "cast")

    def test_bounds_and_enum_rejection(self):
        settings = Settings(path=self.path)
        settings.update({"warn_seconds": -5, "audio_route": "nonsense", "model": "gigantic"})
        self.assertGreaterEqual(settings.get("warn_seconds"), 1.0)
        self.assertEqual(settings.get("audio_route"), "bluetooth")
        self.assertEqual(settings.get("model"), "lite")

    def test_tts_mode_is_validated(self):
        settings = Settings(path=self.path)
        settings.update({"tts_mode": "local"})
        self.assertEqual(settings.get("tts_mode"), "local")
        settings.update({"tts_mode": "nonsense"})
        self.assertEqual(settings.get("tts_mode"), "auto")

    def test_apply_to_machine_config(self):
        settings = Settings(path=self.path)
        settings.update({"warn_seconds": 4, "roast_seconds": 9, "lower_threshold": 0.6})
        cfg = StateMachineConfig()
        settings.apply_to(cfg)
        self.assertEqual(cfg.warn_seconds, 4.0)
        self.assertEqual(cfg.roast_seconds, 9.0)
        self.assertEqual(cfg.lower_threshold, 0.6)


class TestRoastPool(unittest.TestCase):
    def test_pool_fills_and_serves_instantly(self):
        texts = iter(f"Sit up {i}" for i in range(1000))

        def text_source(tier, count):
            return [{"text": next(texts), "tier": tier} for _ in range(count)]

        pool = RoastPool(text_source=text_source, synthesize=lambda text, tier=None: None, size=3, tiers=("mild",), log_fn=lambda *_: None)
        pool.refill_once()
        self.assertEqual(pool.counts()["mild"], 3)
        clip = pool.take("mild")
        self.assertIsNotNone(clip)
        self.assertEqual(pool.counts()["mild"], 2)
        self.assertEqual(pool.take("nope"), None)

    def test_take_any_prefers_a_non_empty_bucket(self):
        pool = RoastPool(
            text_source=lambda tier, count: [{"text": f"{tier} line", "tier": tier} for _ in range(count)],
            synthesize=lambda text, tier=None: None,
            size=2,
            tiers=("mild", "spicy"),
            log_fn=lambda *_: None,
        )
        pool.refill_once()
        self.assertIsNotNone(pool.take_any())
        self.assertEqual(pool.ready, 3)

    def test_pool_reports_voice_mix_and_can_be_cleared(self):
        pool = RoastPool(
            text_source=lambda tier, count: [{"text": f"{tier} {i}", "tier": tier} for i in range(count)],
            synthesize=lambda text, tier=None: f"/tmp/{text}.wav",
            voice_of=lambda path: "gemini",
            size=2,
            tiers=("mild",),
            log_fn=lambda *_: None,
        )
        pool.refill_once()
        self.assertEqual(pool.voice_counts(), {"gemini": 2})
        self.assertEqual(pool.clear(), 2)
        self.assertEqual(pool.voice_counts(), {})

    def test_pool_exposes_ready_texts(self):
        pool = RoastPool(
            text_source=lambda tier, count: [{"text": f"{tier} line {i}", "tier": tier} for i in range(count)],
            synthesize=lambda text, tier=None: None,
            size=2,
            tiers=("mild",),
            log_fn=lambda *_: None,
        )
        pool.refill_once()
        self.assertEqual(sorted(pool.texts()), ["mild line 0", "mild line 1"])


if __name__ == "__main__":
    unittest.main()
