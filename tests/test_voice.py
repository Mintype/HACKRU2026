import json
import tempfile
import unittest
import wave
from pathlib import Path

from voice import roasts, wavutil
from voice.gemini_text import build_prompt, load_prompt_template, parse_roasts, validate


class TestRoasts(unittest.TestCase):
    def test_tiers_escalate(self):
        self.assertEqual(roasts.tier_for(1), "mild")
        self.assertEqual(roasts.tier_for(3), "mild")
        self.assertEqual(roasts.tier_for(4), "medium")
        self.assertEqual(roasts.tier_for(6), "medium")
        self.assertEqual(roasts.tier_for(7), "spicy")

    def test_fallback_batch_count_and_exclusion(self):
        batch = roasts.fallback_batch(count=5, tier="mild")
        self.assertEqual(len(batch), 5)
        for item in batch:
            self.assertEqual(item["tier"], "mild")
            self.assertTrue(item["text"])

        excluded = [item["text"] for item in roasts.fallback_batch(count=9, tier="mild")]
        more = roasts.fallback_batch(count=1, tier="mild", exclude=excluded)
        self.assertNotIn(more[0]["text"], excluded)

    def test_nice_mode_uses_encouragement(self):
        batch = roasts.fallback_batch(count=3, nice=True)
        self.assertTrue(all(item["tier"] == "nice" for item in batch))
        self.assertTrue(all(item["text"] in roasts.NICE for item in batch))

    def test_fallback_one_always_returns_something(self):
        self.assertTrue(roasts.fallback_one(tier="spicy"))


class TestPromptAndValidation(unittest.TestCase):
    def test_prompt_has_safety_rules_and_context(self):
        prompt = build_prompt(
            count=5,
            tier="medium",
            context={"slouch_count": 5, "session_minutes": 23, "worst_neck_angle": 41},
            exclude=["old line"],
        )
        self.assertIn("never the person", prompt.lower())
        self.assertIn("no appearance", prompt.lower())
        self.assertIn("slouch number 5", prompt)
        self.assertIn("41 degrees", prompt)
        self.assertIn("old line", prompt)
        self.assertIn("medium", prompt.lower())

    def test_prompt_files_are_loaded_and_mode_specific(self):
        roast = load_prompt_template(False)
        nice = load_prompt_template(True)
        self.assertNotEqual(roast, nice)
        self.assertIn("insult", roast.lower())
        self.assertIn("encouragement", nice.lower())

    def test_prompt_names_the_failing_part(self):
        neck = build_prompt(count=3, tier="mild", context={"slouch_part": "neck"})
        self.assertIn("head is jutting forward", neck)
        torso = build_prompt(count=3, tier="mild", context={"slouch_part": "torso"})
        self.assertIn("upper back is hunching", torso)
        rich = build_prompt(
            count=3,
            tier="mild",
            context={"slouch_count": 7, "neck_drift": 18.0, "torso_drift": 2.0},
        )
        self.assertIn("slouch number 7", rich)
        self.assertIn("neck 18.0 deg", rich)

    def test_every_token_is_substituted(self):
        for nice_mode in (False, True):
            prompt = build_prompt(
                count=4,
                tier="spicy",
                nice=nice_mode,
                context={"slouch_count": 2, "session_minutes": 3, "worst_neck_angle": 30},
                exclude=["a previous line"],
            )
            self.assertNotIn("{{", prompt, f"unsubstituted token in nice={nice_mode}")
            self.assertIn("4", prompt)

    def test_fallback_lines_are_short_enough_to_say_in_one_breath(self):
        from voice.gemini_text import MAX_WORDS

        for tier in ("mild", "medium", "spicy"):
            for line in roasts.tier_pool(tier):
                self.assertLessEqual(len(line.split()), MAX_WORDS, line)

    def test_parse_plain_json(self):
        raw = json.dumps({"roasts": [{"text": "Sit up.", "tier": "mild"}]})
        self.assertEqual(parse_roasts(raw), [{"text": "Sit up.", "tier": "mild"}])

    def test_parse_fenced_json(self):
        raw = '```json\n{"roasts": [{"text": "Sit up.", "tier": "mild"}]}\n```'
        self.assertEqual(parse_roasts(raw)[0]["text"], "Sit up.")

    def test_parse_broken_json_still_finds_text(self):
        raw = '{"roasts": [{"text": "Sit up please", "tier": "mild",}]}'
        self.assertEqual(parse_roasts(raw)[0]["text"], "Sit up please")

    def test_validate_drops_long_blocklisted_and_dupes(self):
        items = [
            {"text": "Sit up straight", "tier": "mild"},
            {"text": " ".join(["word"] * 30), "tier": "mild"},
            {"text": "you are ugly", "tier": "mild"},
            {"text": "Sit up straight", "tier": "mild"},
            {"text": "", "tier": "mild"},
        ]
        out = validate(items, tier="mild")
        self.assertEqual([i["text"] for i in out], ["Sit up straight"])

    def test_validate_respects_exclude(self):
        items = [{"text": "Sit up straight", "tier": "mild"}, {"text": "Sit tall", "tier": "mild"}]
        out = validate(items, exclude=["sit up straight"])
        self.assertEqual([i["text"] for i in out], ["Sit tall"])


class TestWavUtil(unittest.TestCase):
    def test_write_wav_adds_lead_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.wav"
            pcm = b"\x01\x00" * 2400  # 0.1 s of 24 kHz mono 16-bit
            wavutil.write_wav(path, pcm, sample_rate=24000, lead_silence=0.5)
            self.assertTrue(path.exists())
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), 24000)
                expected = 0.5 + 0.1
                self.assertAlmostEqual(wav.getnframes() / 24000.0, expected, places=2)
            self.assertAlmostEqual(wavutil.duration_seconds(path), expected, places=2)

    def test_prepend_silence_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.wav"
            wavutil.write_wav(path, b"\x00\x00" * 100, sample_rate=8000, lead_silence=0.0)
            wavutil.prepend_silence(path, 1.0)
            self.assertAlmostEqual(wavutil.duration_seconds(path), 1.0 + 100 / 8000.0, places=3)

    def test_mime_params(self):
        rate, width, channels = wavutil.pcm_mime_to_params("audio/L16;rate=24000")
        self.assertEqual((rate, width, channels), (24000, 2, 1))


class TestRoastPersonalisation(unittest.TestCase):
    """Fallback lines may contain {n}; it must never be spoken literally."""

    def _service(self, context):
        from voice.service import RoastService

        class DummyPlayer:
            route = "off"

            def play_clip(self, clip):
                return True

        return RoastService(player=DummyPlayer(), context_provider=lambda: context)

    def test_slouch_number_is_substituted(self):
        service = self._service({"slouch_count": 5})
        self.assertEqual(service._personalise_text("Slouch {n}. Ouch."), "Slouch 5. Ouch.")

    def test_missing_count_leaves_text_alone(self):
        service = self._service({})
        self.assertEqual(service._personalise_text("Sit up."), "Sit up.")

    def test_placeholder_never_survives_without_a_count(self):
        service = self._service({})
        self.assertNotIn("{n}", service._personalise_text("Slouch {n}. Sit up."))

    def test_fallback_batch_aims_at_the_failing_part(self):
        neck = roasts.fallback_batch(count=4, tier="mild", part="neck")
        self.assertTrue(any(item["text"] in roasts.NECK for item in neck))
        torso = roasts.fallback_batch(count=4, tier="mild", part="torso")
        self.assertTrue(any(item["text"] in roasts.TORSO for item in torso))

    def test_fallback_pool_leaves_no_braces(self):
        service = self._service({"slouch_count": 7})
        for tier in ("mild", "medium", "spicy"):
            for item in service._text_source(tier, 6):
                self.assertNotIn("{n}", item["text"])


class TestGeminiTTSQuotaBackoff(unittest.TestCase):
    """The Gemini TTS free tier allows only ~10 requests/day, so a 429 must not
    be retried on every clip the pool wants."""

    def test_quota_error_stops_further_attempts(self):
        import pathlib
        import tempfile

        from voice.gemini_tts import GeminiTTS, is_quota_error, quota_backoff_seconds

        self.assertTrue(is_quota_error("429 RESOURCE_EXHAUSTED"))
        self.assertFalse(is_quota_error("connection reset by peer"))
        self.assertAlmostEqual(quota_backoff_seconds("'retryDelay': '14287s'"), 14287.0, delta=1)
        self.assertAlmostEqual(quota_backoff_seconds("retry in 3h58m7s"), 3 * 3600 + 58 * 60, delta=1)

        tts = GeminiTTS(api_key="fake-key")
        calls = {"n": 0}

        def boom(text):
            calls["n"] += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED ... 'retryDelay': '14287s'")

        tts._call = boom
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "line.wav"
            self.assertFalse(tts.synthesize("Sit up.", out))
            self.assertFalse(tts.ready)
            self.assertGreater(tts.backoff_remaining, 0)
            self.assertFalse(tts.synthesize("Sit up again.", out))
            self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
