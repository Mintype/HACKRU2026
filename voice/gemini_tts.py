"""Gemini text-to-speech.

Gemini TTS models return raw PCM; we wrap it in a WAV with a little leading
silence.  Model names are ``preview`` and change often -- check Google's models
page on the day and set ``GEMINI_TTS_MODEL`` / ``GEMINI_TTS_VOICE``.

Returns ``False`` on any failure so the caller can fall back to a local engine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from . import wavutil

log = logging.getLogger(__name__)

DEFAULT_STYLE = (
    "Speak like a dramatic sports commentator who is deeply disappointed in "
    "someone's posture: punchy, playful, PG."
)


class GeminiTTS:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        style: str = DEFAULT_STYLE,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        self.model = model or os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
        self.voice = voice or os.environ.get("GEMINI_TTS_VOICE", "Kore")
        self.style = style
        self._client = None
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def synthesize(self, text: str, out_path: str | Path) -> bool:
        if not text.strip():
            return False
        if not self.available:
            self.last_error = "no GEMINI_API_KEY"
            return False
        try:
            pcm, sample_rate, channels = self._call(text)
            if not pcm:
                self.last_error = "empty audio response"
                return False
            wavutil.write_wav(out_path, pcm, sample_rate=sample_rate, channels=channels, sample_width=2)
            wavutil.normalize(out_path)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Gemini TTS failed (%s); falling back to local TTS", exc)
            return False

    def _call(self, text: str):
        from google.genai import types

        client = self._get_client()
        prompt = f"{self.style}\n\nSay exactly this, nothing else:\n{text}"

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
            model=self.model,
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
