"""Gemini text-to-speech.

Gemini TTS models return raw PCM; we wrap it in a WAV with a little leading
silence.  Model names are ``preview`` and change often -- check Google's models
page on the day and set ``GEMINI_TTS_MODEL`` / ``GEMINI_TTS_VOICE``.

Returns ``False`` on any failure so the caller can fall back to a local engine.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import wavutil

log = logging.getLogger(__name__)

#: Gemini's free tier allows only a handful of TTS requests per day, so once a
#: 429 comes back we stop asking for a while instead of burning the quota (and
#: the logs) on every clip the pool wants.
MAX_BACKOFF_SECONDS = 6 * 3600

#: How long to skip a model that failed for a reason other than quota (renamed,
#: retired, not enabled for this key).
MODEL_SKIP_SECONDS = 6 * 3600

#: TTS models in preference order. Gemini's free tier limits requests *per
#: model*, and these names change often, so the chain is the safety net: if the
#: preferred one is out of quota or gone, the next one answers.
DEFAULT_TTS_MODEL = "gemini-3.8-flash-lite-tts"
FALLBACK_MODELS = (
    "gemini-3.8-flash-lite-tts",
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
)


def is_quota_error(message: str) -> bool:
    text = (message or "").upper()
    return "RESOURCE_EXHAUSTED" in text or "429" in text or "QUOTA" in text


def quota_backoff_seconds(message: str, default: float = 3600.0) -> float:
    """Pull the retry hint out of a Gemini 429 response."""
    delay = default
    match = re.search(r"retryDelay'?\s*:\s*'?(\d+)s", message or "")
    if match:
        delay = float(match.group(1))
    else:
        match = re.search(r"retry in (\d+)h(\d+)m", message or "")
        if match:
            delay = int(match.group(1)) * 3600 + int(match.group(2)) * 60
    return max(60.0, min(delay, MAX_BACKOFF_SECONDS))


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
VOICE_PROMPT_FILE = "voice.txt"

#: Used when voice/prompts/voice.txt is missing or empty.
DEFAULT_VOICE_PROMPT = """\
You are the voice of a posture-coaching webcam. You say one short line out loud
to someone who is slouching in front of other people.

Delivery: natural and human. Commit to the mood. Do not read punctuation aloud,
do not announce the line, do not add anything before or after it. Never mention
appearance, body, identity or ability. Keep it PG.

[mood mild]
Dry and amused, like a friend who has seen this before.

[mood medium]
Theatrical and mock-grand, like a sports commentator narrating a disaster.

[mood spicy]
Exasperated coach at the end of their rope. Full commitment, still PG.

[mood nice]
Warm and genuinely pleased. Sincere, never sarcastic.
"""


def _parse_voice_prompt(raw: str) -> Tuple[str, Dict[str, str]]:
    """Split a voice prompt into the shared base and its ``[mood x]`` sections."""
    base: List[str] = []
    moods: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[mood ") and stripped.endswith("]"):
            current = stripped[len("[mood ") : -1].strip().lower()
            moods.setdefault(current, [])
            continue
        if current is None:
            base.append(line)
        else:
            moods[current].append(line)
    return (
        "\n".join(base).strip(),
        {key: "\n".join(value).strip() for key, value in moods.items() if "\n".join(value).strip()},
    )


def load_voice_prompt() -> Tuple[str, Dict[str, str]]:
    """Read the delivery prompt + moods, so the persona is editable without code."""
    try:
        raw = (PROMPTS_DIR / VOICE_PROMPT_FILE).read_text(encoding="utf-8")
    except OSError:
        return _parse_voice_prompt(DEFAULT_VOICE_PROMPT)
    base, moods = _parse_voice_prompt(raw)
    if not base:
        return _parse_voice_prompt(DEFAULT_VOICE_PROMPT)
    return base, moods


class GeminiTTS:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        default_mood: str = "medium",
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        #: Preferred model; the rest of :data:`FALLBACK_MODELS` is tried after it.
        self.model = model or os.environ.get("GEMINI_TTS_MODEL") or DEFAULT_TTS_MODEL
        self.voice = voice or os.environ.get("GEMINI_TTS_VOICE", "Kore")
        #: Overrides the shared part of voice/prompts/voice.txt when set.
        self.style = style or os.environ.get("GEMINI_TTS_STYLE") or ""
        self.default_mood = default_mood
        self._client = None
        self.last_error: Optional[str] = None
        #: model -> epoch until which it is skipped (out of quota, or renamed away)
        self._model_cooldown: Dict[str, float] = {}
        #: Which model actually answered last.
        self.model_used: Optional[str] = None

    def style_for(self, tier: Optional[str] = None) -> str:
        """Delivery prompt for a tier: shared base plus that tier's mood.

        Read from voice/prompts/voice.txt on each call, so the persona can be
        tuned while the app is running.
        """
        base, moods = load_voice_prompt()
        base = self.style or base
        mood = moods.get(str(tier or "").lower()) or moods.get(self.default_mood) or ""
        return f"{base}\n\n{mood}".strip() if mood else base

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def candidates(self) -> List[str]:
        """Preferred model first, then the known fallbacks."""
        seen: set = set()
        out: List[str] = []
        for model in [self.model, *FALLBACK_MODELS]:
            if model and model not in seen:
                seen.add(model)
                out.append(model)
        return out

    def _cooling_down(self, model: str) -> bool:
        until = self._model_cooldown.get(model, 0.0)
        if not until:
            return False
        if time.time() >= until:
            self._model_cooldown.pop(model, None)
            return False
        return True

    @property
    def ready(self) -> bool:
        """True while at least one model in the chain is usable."""
        return self.available and any(not self._cooling_down(m) for m in self.candidates())

    @property
    def backoff_remaining(self) -> float:
        """Seconds until the soonest skipped model frees up (0 when ready)."""
        waits = [self._model_cooldown[m] - time.time() for m in self.candidates() if self._cooling_down(m)]
        return max(0.0, min(waits)) if waits else 0.0

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def synthesize(self, text: str, out_path: str | Path, tier: Optional[str] = None) -> bool:
        """Voice a line, walking the model chain until one answers."""
        if not text.strip():
            return False
        if not self.available:
            self.last_error = "no GEMINI_API_KEY"
            return False

        style = self.style_for(tier)
        problems: List[str] = []
        for model in self.candidates():
            if self._cooling_down(model):
                continue
            try:
                pcm, sample_rate, channels = self._call(text, style, model)
            except Exception as exc:
                message = str(exc)
                quota = is_quota_error(message)
                delay = quota_backoff_seconds(message) if quota else MODEL_SKIP_SECONDS
                self._model_cooldown[model] = time.time() + delay
                problems.append(f"{model}: {'quota' if quota else message[:80]}")
                log.warning(
                    "Gemini TTS model %s unavailable (%s); skipping it for %.0f min",
                    model,
                    "quota" if quota else message[:80],
                    delay / 60.0,
                )
                continue
            if not pcm:
                self._model_cooldown[model] = time.time() + MODEL_SKIP_SECONDS
                problems.append(f"{model}: empty audio")
                continue

            wavutil.write_wav(out_path, pcm, sample_rate=sample_rate, channels=channels, sample_width=2)
            wavutil.normalize(out_path)
            if self.model_used != model:
                log.info("Gemini TTS is speaking with %s", model)
            self.model_used = model
            self.last_error = None
            return True

        self.last_error = "; ".join(problems)[:400] or "all Gemini TTS models are cooling down"
        return False

    def _call(self, text: str, style: str, model: Optional[str] = None):
        from google.genai import types

        client = self._get_client()
        prompt = f"{style}\n\nSay exactly this, nothing else:\n{text}"

        speech_config = None
        try:
            speech_config = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                )
            )
        except Exception:
            speech_config = None

        config_kwargs = {"response_modalities": ["AUDIO"]}
        if speech_config is not None:
            config_kwargs["speech_config"] = speech_config

        response = client.models.generate_content(
            model=model or self.model,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        for part in _iter_parts(response):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                mime = getattr(inline, "mime_type", "") or "audio/L16;rate=24000"
                rate, width, channels = wavutil.pcm_mime_to_params(mime)
                return data, rate, channels
        return b"", 24000, 1


def _iter_parts(response):
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            yield part
