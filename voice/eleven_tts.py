"""ElevenLabs text-to-speech.

Preferred over Gemini for this project: the free tier is measured in characters
per month rather than a handful of requests per day, so the roast pool can
actually be voiced without hitting a wall.

The API returns raw audio. We ask for 16-bit PCM (``output_format=pcm_24000``)
so it drops straight into the same WAV pipeline the rest of the project uses --
Bluetooth playback and the Chromecast route both already expect a WAV file.

Mood is expressed through ``voice_settings`` rather than a prose style prompt:
a lower ``stability`` and higher ``style`` make the delivery more animated, so
the tier of an insult still changes how it sounds.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import wavutil

log = logging.getLogger(__name__)

API_ROOT = "https://api.elevenlabs.io/v1"

#: "Rachel" — a long-standing default that exists on every account.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
#: Flash is the fast, cheap model; good fit for one-liners on a free plan.
DEFAULT_MODEL = "eleven_flash_v2_5"
DEFAULT_OUTPUT_FORMAT = "pcm_24000"

#: tier -> voice_settings. Lower stability + more style = more animated.
TIER_SETTINGS: Dict[str, Dict[str, float]] = {
    "mild": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.15},
    "medium": {"stability": 0.40, "similarity_boost": 0.75, "style": 0.35},
    "spicy": {"stability": 0.25, "similarity_boost": 0.80, "style": 0.60},
    "nice": {"stability": 0.65, "similarity_boost": 0.75, "style": 0.10},
}
DEFAULT_TIER_SETTINGS: Dict[str, float] = {"stability": 0.45, "similarity_boost": 0.75, "style": 0.30}


class ElevenLabsTTS:
    def __init__(
        self,
        api_key: Optional[str] = None,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        output_format: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY") or ""
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID
        self.model = model or os.environ.get("ELEVENLABS_MODEL") or DEFAULT_MODEL
        self.output_format = (
            output_format or os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or DEFAULT_OUTPUT_FORMAT
        )
        self.timeout = float(timeout)
        self.last_error: Optional[str] = None
        #: Which voice answered last (parallel to GeminiTTS.model_used).
        self.voice_used: Optional[str] = None

    # -- capability --------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def ready(self) -> bool:
        # ElevenLabs bills per character and reports usage on the account, so
        # there is nothing to cool down here; failures are per request.
        return self.available

    def settings_for(self, tier: Optional[str] = None) -> Dict[str, float]:
        return dict(TIER_SETTINGS.get(str(tier or "").lower(), DEFAULT_TIER_SETTINGS))

    def _sample_rate(self) -> int:
        if self.output_format.startswith("pcm_"):
            try:
                return int(self.output_format.split("_", 1)[1])
            except (IndexError, ValueError):
                pass
        return 24000

    # -- request -----------------------------------------------------------
    def build_request(self, text: str, settings: Dict[str, float]) -> Tuple[str, Dict[str, str], bytes]:
        """(url, headers, json body) — split out so it can be tested offline."""
        url = f"{API_ROOT}/text-to-speech/{self.voice_id}?output_format={self.output_format}"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        body = json.dumps(
            {"text": text, "model_id": self.model, "voice_settings": settings}
        ).encode("utf-8")
        return url, headers, body

    def synthesize(self, text: str, out_path: str | Path, tier: Optional[str] = None) -> bool:
        if not text.strip():
            return False
        if not self.available:
            self.last_error = "no ELEVENLABS_API_KEY"
            return False
        try:
            pcm = self._call(text, self.settings_for(tier))
            if not pcm:
                self.last_error = "empty audio response"
                return False
            wavutil.write_wav(
                out_path, pcm, sample_rate=self._sample_rate(), channels=1, sample_width=2
            )
            wavutil.normalize(out_path)
            self.voice_used = self.voice_id
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("ElevenLabs TTS failed (%s); falling back", exc)
            return False

    def _call(self, text: str, settings: Dict[str, float], with_style: bool = True) -> bytes:
        body_settings = dict(settings)
        if not with_style:
            body_settings.pop("style", None)  # some models reject it
        url, headers, body = self.build_request(text, body_settings)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if exc.code == 422 and with_style:
                return self._call(text, settings, with_style=False)
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
