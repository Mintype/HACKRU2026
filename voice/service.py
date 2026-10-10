"""Orchestrates the roast pipeline.

Ties together the roast pool (Gemini + fallbacks), the audio player and the
session context, and exposes the two entry points the server needs:

* :meth:`on_roast_due` -- called when the state machine fires ``roast_due``.
* :meth:`test_roast`    -- the "play a roast now" demo button.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

import config
from .gemini_text import RoastWriter
from .gemini_tts import GeminiTTS
from .insults import InsultLog
from .player import AudioPlayer
from .pool import Clip, RoastPool
from .roasts import TIERS, fallback_batch, fallback_one, tier_for
from .tts_local import LocalTTS

log = logging.getLogger(__name__)


class RoastService:
    def __init__(
        self,
        *,
        player: AudioPlayer,
        writer: Optional[RoastWriter] = None,
        tts: Optional[GeminiTTS] = None,
        local_tts: Optional[LocalTTS] = None,
        settings=None,
        context_provider: Optional[Callable[[], Dict]] = None,
        on_played: Optional[Callable[[Clip, Dict], None]] = None,
        pool_size: int = 9,
        log_fn=log.info,
    ):
        self.player = player
        self.writer = writer if writer is not None else RoastWriter()
        self.tts = tts if tts is not None else GeminiTTS()
        self.local_tts = local_tts if local_tts is not None else LocalTTS()
        self.settings = settings
        self.context_provider = context_provider or (lambda: {})
        self.on_played = on_played
        self.log = log_fn

        self._recent: Deque[str] = deque(maxlen=10)
        self.insults = InsultLog()
        self._lock = threading.Lock()
        self.pool = RoastPool(
            text_source=self._text_source,
            synthesize=self._synthesize,
            context_provider=self.context_provider,
            voice_of=self._voice_from_path,
            tiers=TIERS,
            size=pool_size,
            log_fn=log_fn,
        )

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if not self.player.is_alive():
            self.player.start()
        if not self.pool.is_alive():
            self.pool.start()

    def stop(self) -> None:
        self.pool.stop()
        self.player.stop()

    # -- entry points -----------------------------------------------------
    def on_roast_due(self, event: Dict) -> Optional[Clip]:
        count = int(event.get("slouch_count") or 1)
        tier = tier_for(count)
        clip = self.pool.take(tier) or self.pool.take_any()
        if clip is None:
            text = self._fallback_text(tier)
            clip = Clip(text=text, tier=tier, path=self._synthesize(text), source="fallback")
        return self._deliver(clip, event)

    def test_roast(self) -> Optional[Clip]:
        clip = self.pool.take_any()
        if clip is None:
            text = self._fallback_text("mild")
            clip = Clip(text=text, tier="mild", path=self._synthesize(text), source="fallback")
        return self._deliver(clip, {"event": "test_roast"})

    # -- internals --------------------------------------------------------
    def _deliver(self, clip: Clip, event: Dict) -> Optional[Clip]:
        if not self.player.play_clip(clip):
            return None
        with self._lock:
            self._recent.append(clip.text)
        # Record it so it is never said again.
        self.insults.add(clip.text)
        if self.on_played is not None:
            try:
                self.on_played(clip, event)
            except Exception:
                log.warning("on_played callback failed", exc_info=True)
        return clip

    def _nice(self) -> bool:
        return bool(self.settings.get("nice_mode")) if self.settings is not None else False

    def _exclude_list(self) -> List[str]:
        """Everything we must not say again, most relevant first.

        Ordering matters because only the head of this list is shown to the
        model: the lines just said, then what is already queued, then older
        history.
        """
        with self._lock:
            recent = list(self._recent)
        candidates = (
            list(reversed(recent))
            + list(self.pool.texts())
            + list(reversed(self.insults.used(limit=self.insults.max_exclude)))
        )
        seen = set()
        out: List[str] = []
        for text in candidates:
            key = " ".join((text or "").lower().split())
            if key and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    def _fallback_text(self, tier: str) -> str:
        exclude = self._exclude_list()
        nice = self._nice()
        context = self.context_provider() or {}
        text = fallback_one(
            tier="mild" if nice else tier,
            nice=nice,
            exclude=exclude,
            part=context.get("slouch_part"),
        )
        return self._personalise_text(text)

    def _personalise_text(self, text: str) -> str:
        """Fill ``{n}`` if present, and never let a literal placeholder through.

        Clips are pre-generated at startup (before any slouch has been counted),
        so a leftover ``{n}`` must never be synthesised into audio.
        """
        if "{n}" not in text:
            return text
        count = (self.context_provider() or {}).get("slouch_count")
        cleaned = text.replace("{n}", str(count) if count else "")
        return " ".join(cleaned.replace(" .", ".").replace(" ,", ",").split())

    def _text_source(self, tier: str, count: int) -> List[Dict]:
        nice = self._nice()
        exclude = self._exclude_list()
        context = self.context_provider() or {}
        items: List[Dict] = []
        if self.writer.available:
            items = self.writer.generate(
                count=count,
                tier=tier,
                context=context,
                nice=nice,
                exclude=exclude,
            )
        items = [i for i in items if i.get("text")]
        if len(items) < count:
            items += fallback_batch(
                count=count - len(items),
                tier=tier,
                nice=nice,
                exclude=exclude + [i["text"] for i in items],
                part=context.get("slouch_part"),
            )
        for item in items[:count]:
            item["text"] = self._personalise_text(item["text"])
        return items[:count]

    def _tts_mode(self) -> str:
        """auto = Gemini when it can, else local; or force one for consistency."""
        if self.settings is None:
            return "auto"
        mode = str(self.settings.get("tts_mode") or "auto").lower()
        return mode if mode in {"auto", "gemini", "local"} else "auto"

    @staticmethod
    def _voice_from_path(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        name = str(path)
        if name.endswith("_gemini.wav"):
            return "gemini"
        if name.endswith("_local.wav"):
            return "local"
        return "unknown"

    def _synthesize(self, text: str, tier: Optional[str] = None) -> Optional[str]:
        """Voice one line, honouring the voice-mode setting.

        The voice is part of the filename, so the cache is voice-aware and a
        clip made by one engine is never mistaken for the other. Clips are
        pre-generated, so the mix is decided by whichever engine is available
        while the pool is filling.
        """
        mode = self._tts_mode()
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]

        def attempt(voice: str) -> Optional[str]:
            path = config.ROAST_DIR / f"roast_{digest}_{voice}.wav"
            if path.exists() and path.stat().st_size > 0:
                return str(path)
            if voice == "gemini":
                if mode == "local" or not self.tts.ready:
                    return None
                return str(path) if self.tts.synthesize(text, path, tier) else None
            if mode == "gemini" or not self.local_tts.available:
                return None
            return str(path) if self.local_tts.synthesize(text, path) else None

        if mode == "local":
            return attempt("local")
        if mode == "gemini":
            return attempt("gemini")
        return attempt("gemini") or attempt("local")

    # -- status -----------------------------------------------------------
    def status(self) -> Dict:
        with self._lock:
            recent = list(self._recent)
        return {
            "gemini_text": self.writer.available,
            "gemini_text_error": self.writer.last_error,
            "gemini_tts": self.tts.available,
            "gemini_tts_ready": getattr(self.tts, "ready", self.tts.available),
            "gemini_tts_error": self.tts.last_error,
            "local_tts": self.local_tts.engine,
            "tts_mode": self._tts_mode(),
            "insults_used": self.insults.count,
            "insults_file": str(self.insults.path),
            "nice_mode": self._nice(),
            "recent": recent[-5:],
            "pool": self.pool.status(),
        }
