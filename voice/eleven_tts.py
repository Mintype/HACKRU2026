"""ElevenLabs text-to-speech.

Preferred over Gemini for this project: the free tier is measured in characters
per month rather than a handful of requests per day, so the roast pool can
actually be voiced without hitting a wall.

Two things the API is picky about, both handled here:

* **Free plans cannot use "library" voices.** The account's own *premade* voices
  work; asking for a library one returns HTTP 402. So the configured voice is
  tried first and, if the plan rejects it, the account's own voices are listed
  and tried instead.
* The API returns raw audio. We ask for 16-bit PCM (``output_format=pcm_24000``)
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
from typing import Dict, List, Optional, Tuple

from . import wavutil

log = logging.getLogger(__name__)

API_ROOT = "https://api.elevenlabs.io/v1"

#: "Sarah - Mature, Reassuring, Confident" — a *premade* voice, so it works on a
#: free plan. (Library voices such as Rachel need a paid subscription.)
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
#: Flash is the fast, cheap model; a good fit for one-liners on a free plan.
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


class PlanError(RuntimeError):
    """The subscription is not allowed to use this voice or model."""


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
        #: Which voice answered last.
        self.voice_used: Optional[str] = None
        self._voices: Optional[List[Dict[str, str]]] = None
        self._discovered: Optional[List[str]] = None

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

    # -- account voices ----------------------------------------------------
    def list_voices(self, refresh: bool = False) -> List[Dict[str, str]]:
        """Voices this account may use. Free plans only get the premade ones."""
        if self._voices is not None and not refresh:
            return self._voices
        if not self.available:
            return []
        request = urllib.request.Request(
            f"{API_ROOT}/voices", headers={"xi-api-key": self.api_key}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        voices = [
            {
                "voice_id": str(item.get("voice_id", "")),
                "name": str(item.get("name", "")),
                "category": str(item.get("category", "")),
            }
            for item in (payload.get("voices") or [])
            if item.get("voice_id")
        ]
        self._voices = voices
        return voices

    def _discover_voice_ids(self) -> List[str]:
        """Ask the API which voices are really usable, once."""
        if self._discovered is not None:
            return self._discovered
        try:
            self._discovered = [voice["voice_id"] for voice in self.list_voices(refresh=True)]
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.debug("could not list ElevenLabs voices: %s", exc)
            self._discovered = []
        return self._discovered

    def _voice_queue(self) -> List[str]:
        """The configured voice, then whatever else the account can actually use."""
        queue: List[str] = []
        for voice_id in [self.voice_id, *self._discover_voice_ids()]:
            if voice_id and voice_id not in queue:
                queue.append(voice_id)
        return queue

    # -- request -----------------------------------------------------------
    def build_request(
        self, text: str, settings: Dict[str, float], voice_id: Optional[str] = None
    ) -> Tuple[str, Dict[str, str], bytes]:
        """(url, headers, json body) — split out so it can be tested offline."""
        voice = voice_id or self.voice_id
        url = f"{API_ROOT}/text-to-speech/{voice}?output_format={self.output_format}"
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

        settings = self.settings_for(tier)
        problems: List[str] = []
        queue = self._voice_queue()
        tried: List[str] = []

        while queue:
            voice_id = queue.pop(0)
            tried.append(voice_id)
            try:
                pcm = self._call(text, settings, voice_id)
            except PlanError as exc:
                # Free plans cannot use library voices; widen the queue to the
                # account's own voices and carry on.
                problems.append(f"{voice_id}: {exc}")
                for other in self._discover_voice_ids():
                    if other not in tried and other not in queue:
                        queue.append(other)
                continue
            except Exception as exc:  # noqa: BLE001 - report and try the next
                problems.append(f"{voice_id}: {str(exc)[:80]}")
                continue

            if not pcm:
                problems.append(f"{voice_id}: empty audio")
                continue

            wavutil.write_wav(
                out_path, pcm, sample_rate=self._sample_rate(), channels=1, sample_width=2
            )
            wavutil.normalize(out_path)
            if self.voice_used != voice_id:
                log.info("ElevenLabs is speaking with voice %s", voice_id)
            self.voice_used = voice_id
            self.last_error = None
            return True

        self.last_error = "; ".join(problems)[:400] or "no usable voice"
        return False

    def _call(
        self,
        text: str,
        settings: Dict[str, float],
        voice_id: Optional[str] = None,
        with_style: bool = True,
    ) -> bytes:
        body_settings = dict(settings)
        if not with_style:
            body_settings.pop("style", None)  # some models reject it
        url, headers, body = self.build_request(text, body_settings, voice_id)
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
            if exc.code in (401, 402, 403):
                raise PlanError(detail or f"HTTP {exc.code}") from exc
            if exc.code == 422 and with_style:
                return self._call(text, settings, voice_id, with_style=False)
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
