import json
import tempfile
import unittest
import wave
from pathlib import Path

from voice import roasts, wavutil
from voice.gemini_text import build_prompt, parse_roasts, validate


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
        self.assertIn("NEVER mention appearance", prompt)
        self.assertIn("slouch number 5", prompt)
        self.assertIn("41 degrees", prompt)
        self.assertIn("old line", prompt)
        self.assertIn("medium", prompt.lower())

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


if __name__ == "__main__":
    unittest.main()
