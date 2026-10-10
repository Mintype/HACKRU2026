import json
import pathlib
import tempfile
import unittest
import wave
from pathlib import Path

from voice import roasts, wavutil
from voice.gemini_text import build_prompt, load_prompt_template, parse_roasts, validate

import config


def offline_writer():
    """A RoastWriter that cannot reach the network, whatever .env says."""
    from voice.gemini_text import RoastWriter

    writer = RoastWriter()
    writer.api_key = ""
    return writer


def offline_eleven():
    """An ElevenLabsTTS that cannot reach the network."""
    from voice.eleven_tts import ElevenLabsTTS

    tts = ElevenLabsTTS()
    tts.api_key = ""
    return tts


def offline_service(**kwargs):
    from voice.service import RoastService

    kwargs.setdefault("writer", offline_writer())
    kwargs.setdefault("eleven", offline_eleven())
    return RoastService(**kwargs)


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


class TestVoiceMode(unittest.TestCase):
    """A pool must be able to use a single voice, on purpose."""

    class _Settings:
        def __init__(self, mode):
            self._mode = mode

        def get(self, key, default=None):
            if key == "tts_mode":
                return self._mode
            if key == "nice_mode":
                return False
            return default

    class _Player:
        route = "off"

        def play_clip(self, clip):
            return True

    class _Local:
        available = True
        engine = "fake"

        def synthesize(self, text, path):
            pathlib.Path(path).write_bytes(b"RIFF....")
            return True

    def _service(self, mode):
        return offline_service(
            player=self._Player(),
            local_tts=self._Local(),
            settings=self._Settings(mode),
            context_provider=lambda: {},
            pool_size=2,
        )

    def _voice(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            original = config.ROAST_DIR
            config.ROAST_DIR = pathlib.Path(tmp)
            try:
                return self._service(mode)._synthesize("Sit up.")
            finally:
                config.ROAST_DIR = original

    def test_local_mode_uses_only_the_local_engine(self):
        self.assertTrue(self._voice("local").endswith("_local.wav"))

    def test_auto_uses_local_without_an_elevenlabs_key(self):
        self.assertTrue(self._voice("auto").endswith("_local.wav"))

    def test_an_unknown_mode_falls_back_to_auto(self):
        self.assertTrue(self._voice("nonsense").endswith("_local.wav"))

    def test_voice_is_recovered_from_the_filename(self):
        from voice.service import RoastService

        self.assertEqual(RoastService._voice_from_path("/x/roast_ab_elevenlabs.wav"), "elevenlabs")
        self.assertEqual(RoastService._voice_from_path("/x/roast_ab_local.wav"), "local")
        # left over from when Gemini TTS was wired in
        self.assertEqual(RoastService._voice_from_path("/x/roast_ab_gemini.wav"), "gemini")
        self.assertEqual(RoastService._voice_from_path("/x/roast_ab.wav"), "unknown")
        self.assertIsNone(RoastService._voice_from_path(None))


class TestLocalVoiceSelection(unittest.TestCase):
    def test_local_tts_voice_can_be_chosen(self):
        import pathlib

        from voice.tts_local import LocalTTS

        tts = LocalTTS(voice="Microsoft Zira Desktop")
        tts.engine = "sapi"
        script = " ".join(tts._command("Sit up.", pathlib.Path("/tmp/x.wav")))
        self.assertIn("SelectVoice", script)
        self.assertIn("Microsoft Zira Desktop", script)

        tts.engine = "espeak-ng"
        command = tts._command("Sit up.", pathlib.Path("/tmp/x.wav"))
        self.assertIn("Microsoft Zira Desktop", command)

    def test_default_espeak_voice_does_not_select_on_windows(self):
        import pathlib

        from voice.tts_local import LocalTTS

        tts = LocalTTS(voice="en")
        tts.engine = "sapi"
        script = " ".join(tts._command("Sit up.", pathlib.Path("/tmp/x.wav")))
        self.assertNotIn("SelectVoice", script)


class TestLiveRoasts(unittest.TestCase):
    """Score-triggered roasts can be generated live, without blocking vision."""

    class _Settings:
        def __init__(self, source="live", cooldown=30.0):
            self._values = {
                "roast_source": source,
                "live_api_cooldown_seconds": cooldown,
                "tts_mode": "local",
                "nice_mode": False,
            }

        def get(self, key, default=None):
            return self._values.get(key, default)

    class _Player:
        route = "off"

        def __init__(self):
            self.played = []

        def play_clip(self, clip):
            self.played.append(clip.text)
            return True

    class _NoLocalTTS:
        available = False
        engine = None

        def synthesize(self, text, path):
            return False

    def _service(self, **kwargs):
        return offline_service(
            player=self._Player(),
            local_tts=self._NoLocalTTS(),
            settings=self._Settings(**kwargs),
            context_provider=lambda: {},
            pool_size=1,
        )

    def test_live_mode_queues_instead_of_blocking(self):
        service = self._service()
        self.assertIsNone(service.on_roast_due({"slouch_count": 1}))
        self.assertEqual(service._live_queue.qsize(), 1)

    def test_api_cooldown_gates_a_second_call(self):
        service = self._service(cooldown=30.0)
        self.assertTrue(service._api_cooldown_ok())
        service._note_api_call()
        self.assertFalse(service._api_cooldown_ok())
        self.assertGreater(service._api_cooldown_seconds(), 0)

    def test_pool_refilling_pauses_in_live_mode(self):
        self.assertFalse(self._service()._should_refill_pool())
        self.assertTrue(self._service(source="pool")._should_refill_pool())

    def test_live_generation_always_returns_a_clip(self):
        service = self._service(source="pool")
        clip = service._make_live_clip("mild", {"event": "roast_due"})
        self.assertTrue(clip.text)
        self.assertEqual(clip.tier, "mild")

    def test_clips_made_outside_the_pool_are_tagged_with_their_voice(self):
        # Live roasts and fallbacks bypass the pool, which is where the tagging
        # used to happen, so their voice never reached the database.
        service = self._service(source="pool")
        service._synthesize = lambda text, tier=None: "/tmp/roast_ab_gemini.wav"
        clip = service._take_pooled("mild")
        self.assertEqual(clip.voice, "gemini")
        self.assertEqual(clip.path, "/tmp/roast_ab_gemini.wav")


class TestInsultLog(unittest.TestCase):
    """data/insults.txt — the never-repeat list."""

    def test_records_dedupes_and_limits(self):
        from voice.insults import InsultLog

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "insults.txt"
            log = InsultLog(path=path)
            self.assertEqual(log.count, 0)
            self.assertTrue(log.add("Sit up."))
            self.assertFalse(log.add("  sit   UP.  "))  # same line, different spacing/case
            self.assertTrue(log.add("Sit tall."))
            self.assertEqual(log.count, 2)
            self.assertEqual(log.used(), ["Sit up.", "Sit tall."])
            self.assertEqual(log.used(limit=1), ["Sit tall."])
            self.assertTrue(log.has("SIT UP."))
            self.assertFalse(log.has("never said this"))
            self.assertEqual(InsultLog(path=path).count, 2)  # survives a restart

    def test_delivered_insults_are_recorded_and_excluded(self):
        from voice.insults import InsultLog
        from voice.pool import Clip

        class Player:
            route = "off"

            def play_clip(self, clip):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            service = offline_service(player=Player(), context_provider=lambda: {})
            service.insults = InsultLog(path=pathlib.Path(tmp) / "insults.txt")
            service._deliver(Clip(text="Sit up, you comma.", tier="mild"), {})
            self.assertEqual(service.insults.count, 1)
            self.assertIn("Sit up, you comma.", service._exclude_list())
            service._deliver(Clip(text="SIT UP, YOU COMMA.", tier="mild"), {})
            self.assertEqual(service.insults.count, 1)


class TestOriginality(unittest.TestCase):
    def test_fallbacks_skip_lines_already_said(self):
        said = roasts.MILD[:9]
        batch = roasts.fallback_batch(count=1, tier="mild", exclude=said)
        self.assertNotIn(batch[0]["text"], said)

    def test_fallbacks_degrade_rather_than_go_silent(self):
        # The hand-written pool is finite; once every line is used it may repeat
        # rather than say nothing at all.
        batch = roasts.fallback_batch(count=3, tier="mild", exclude=roasts.MILD)
        self.assertEqual(len(batch), 3)

    def test_prompt_bans_everything_already_said(self):
        # Callers pass the exclude list most-relevant-first, so the newest lines
        # are the ones shown to the model.
        said = [f"old line {i}" for i in range(30)]
        prompt = build_prompt(count=3, tier="mild", exclude=list(reversed(said)))
        self.assertIn("brand new", prompt)
        self.assertIn("old line 29", prompt)

    def test_exclude_list_puts_the_newest_first(self):
        from voice.insults import InsultLog
        from voice.pool import Clip

        class Player:
            route = "off"

            def play_clip(self, clip):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            service = offline_service(player=Player(), context_provider=lambda: {})
            service.insults = InsultLog(path=pathlib.Path(tmp) / "insults.txt")
            service._deliver(Clip(text="first thing said", tier="mild"), {})
            service._deliver(Clip(text="second thing said", tier="mild"), {})
            self.assertEqual(service._exclude_list()[0], "second thing said")


class TestProviderOrder(unittest.TestCase):
    """auto is ElevenLabs -> local, and nothing else."""

    class _Settings:
        def __init__(self, mode):
            self._values = {"tts_mode": mode, "nice_mode": False}

        def get(self, key, default=None):
            return self._values.get(key, default)

    class _Player:
        route = "off"

        def play_clip(self, clip):
            return True

    class _Voice:
        def __init__(self, available=True):
            self.available = available
            self.ready = available
            self.last_error = None

        def synthesize(self, text, path, tier=None):
            if not self.available:
                return False
            pathlib.Path(path).write_bytes(b"RIFF....")
            return True

    def _service(self, mode, eleven=True, local=True):
        return offline_service(
            player=self._Player(),
            eleven=self._Voice(eleven),
            local_tts=self._Voice(local),
            settings=self._Settings(mode),
            context_provider=lambda: {},
            pool_size=2,
        )

    def _voice_for(self, mode, **kwargs):
        service = self._service(mode, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            original = config.ROAST_DIR
            config.ROAST_DIR = pathlib.Path(tmp)
            try:
                return service._synthesize("Sit up.", "mild")
            finally:
                config.ROAST_DIR = original

    def test_auto_prefers_elevenlabs(self):
        self.assertTrue(self._voice_for("auto").endswith("_elevenlabs.wav"))

    def test_auto_falls_back_to_local_without_a_key(self):
        self.assertTrue(self._voice_for("auto", eleven=False).endswith("_local.wav"))

    def test_forced_elevenlabs_is_silent_without_a_key(self):
        self.assertIsNone(self._voice_for("elevenlabs", eleven=False))

    def test_forced_local_never_uses_elevenlabs(self):
        self.assertTrue(self._voice_for("local").endswith("_local.wav"))

    def test_gemini_is_no_longer_part_of_the_voice_path(self):
        path = self._voice_for("gemini")
        self.assertIsNotNone(path)
        self.assertFalse(path.endswith("_gemini.wav"))


class TestElevenLabs(unittest.TestCase):
    def test_request_carries_key_voice_and_format(self):
        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="k", voice_id="VOICE123", model="m1", output_format="pcm_24000")
        url, headers, body = tts.build_request("Sit up.", tts.settings_for("spicy"))
        self.assertIn("/text-to-speech/VOICE123", url)
        self.assertIn("output_format=pcm_24000", url)
        self.assertEqual(headers["xi-api-key"], "k")
        payload = json.loads(body)
        self.assertEqual(payload["text"], "Sit up.")
        self.assertEqual(payload["model_id"], "m1")
        self.assertIn("stability", payload["voice_settings"])

    def test_mood_varies_by_tier(self):
        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="k")
        mild, spicy = tts.settings_for("mild"), tts.settings_for("spicy")
        self.assertNotEqual(mild, spicy)
        self.assertLess(spicy["stability"], mild["stability"])  # more animated
        self.assertGreater(spicy["style"], mild["style"])
        self.assertEqual(tts.settings_for("nonsense"), tts.settings_for(None))

    def test_synthesize_writes_a_playable_wav(self):
        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="k")
        tts._call = lambda text, settings, voice_id=None, with_style=True: b"\x00\x00" * 12000
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "line.wav"
            self.assertTrue(tts.synthesize("Sit up.", out, "mild"))
            with wave.open(str(out), "rb") as wav:
                self.assertEqual(wav.getframerate(), 24000)
                self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(tts.voice_used, tts.voice_id)

    def test_without_a_key_it_refuses(self):
        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="")
        tts.api_key = ""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(tts.synthesize("Sit up.", pathlib.Path(tmp) / "x.wav", "mild"))
        self.assertIn("ELEVENLABS_API_KEY", tts.last_error or "")

    def test_a_library_voice_falls_back_to_one_the_plan_allows(self):
        from voice.eleven_tts import ElevenLabsTTS, PlanError

        tts = ElevenLabsTTS(api_key="k", voice_id="library-voice")
        tts._discover_voice_ids = lambda: ["premade-voice"]
        calls = []

        def fake_call(text, settings, voice_id=None, with_style=True):
            calls.append(voice_id)
            if voice_id == "library-voice":
                raise PlanError("Free users cannot use library voices via the API")
            return b"\x00\x00" * 10

        tts._call = fake_call
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(tts.synthesize("Sit up.", pathlib.Path(tmp) / "x.wav", "mild"))
        self.assertEqual(calls, ["library-voice", "premade-voice"])
        self.assertEqual(tts.voice_used, "premade-voice")

    def test_voice_queue_prefers_the_configured_voice(self):
        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="k", voice_id="mine")
        tts._discover_voice_ids = lambda: ["mine", "other"]
        self.assertEqual(tts._voice_queue(), ["mine", "other"])

    def test_list_voices_parses_the_account_payload(self):
        import io
        import urllib.request

        from voice.eleven_tts import ElevenLabsTTS

        payload = (
            b'{"voices":[{"voice_id":"v1","name":"One","category":"premade"},'
            b'{"voice_id":"v2","name":"Two","category":"library"}]}'
        )
        tts = ElevenLabsTTS(api_key="k")
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=None: io.BytesIO(payload)
        try:
            voices = tts.list_voices()
        finally:
            urllib.request.urlopen = original
        self.assertEqual([v["voice_id"] for v in voices], ["v1", "v2"])
        self.assertEqual(voices[0]["category"], "premade")

    def test_a_402_is_reported_as_a_plan_error(self):
        import io
        import urllib.error
        import urllib.request

        from voice.eleven_tts import ElevenLabsTTS, PlanError

        tts = ElevenLabsTTS(api_key="k")

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 402, "Payment Required", {}, io.BytesIO(b'{"detail":"paid"}')
            )

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(PlanError):
                tts._call("Sit up.", tts.settings_for("mild"))
        finally:
            urllib.request.urlopen = original

    def test_a_422_retries_without_the_style_setting(self):
        import io
        import urllib.error
        import urllib.request

        from voice.eleven_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="k")
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(json.loads(request.data.decode())["voice_settings"])
            if len(seen) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"detail":"bad"}')
                )

            class Response:
                def read(self):
                    return b"\x00\x00" * 10

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            pcm = tts._call("Sit up.", tts.settings_for("mild"))
        finally:
            urllib.request.urlopen = original
        self.assertEqual(len(seen), 2)
        self.assertIn("style", seen[0])
        self.assertNotIn("style", seen[1])
        self.assertTrue(pcm)


if __name__ == "__main__":
    unittest.main()
