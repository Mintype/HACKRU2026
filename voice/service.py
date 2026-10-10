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
        self._lock = threading.Lock()
        self.pool = RoastPool(
            text_source=self._text_source,
            synthesize=self._synthesize,
            context_provider=self.context_provider,
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
        if self.on_played is not None:
            try:
                self.on_played(clip, event)
            except Exception:
                log.warning("on_played callback failed", exc_info=True)
        return clip

    def _nice(self) -> bool:
        return bool(self.settings.get("nice_mode")) if self.settings is not None else False

    def _fallback_text(self, tier: str) -> str:
        with self._lock:
            exclude = list(self._recent)
        return fallback_one(tier=tier if not self._nice() else "mild", nice=self._nice(), exclude=exclude)

    def _text_source(self, tier: str, count: int) -> List[Dict]:
        nice = self._nice()
        with self._lock:
            exclude = list(self._recent)
        items: List[Dict] = []
        if self.writer.available:
            items = self.writer.generate(
                count=count,
                tier=tier,
                context=self.context_provider(),
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
            )
        return items[:count]

    def _synthesize(self, text: str) -> Optional[str]:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        path = config.ROAST_DIR / f"roast_{digest}.wav"
        if path.exists() and path.stat().st_size > 0:
            return str(path)
        if self.tts.available and self.tts.synthesize(text, path):
            return str(path)
        if self.local_tts.available and self.local_tts.synthesize(text, path):
            return str(path)
        return None

    # -- status -----------------------------------------------------------
    def status(self) -> Dict:
        with self._lock:
            recent = list(self._recent)
        return {
            "gemini_text": self.writer.available,
            "gemini_text_error": self.writer.last_error,
            "gemini_tts": self.tts.available,
            "gemini_tts_error": self.tts.last_error,
            "local_tts": self.local_tts.engine,
            "nice_mode": self._nice(),
            "recent": recent[-5:],
            "pool": self.pool.status(),
        }
