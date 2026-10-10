"""Orchestrates the roast pipeline.

Ties together the roast pool (Gemini + fallbacks), the audio player and the
session context, and exposes the two entry points the server needs:

* :meth:`on_roast_due` -- called when the state machine fires ``roast_due``.
* :meth:`test_roast`    -- the "play a roast now" demo button.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

import config
from .eleven_tts import ElevenLabsTTS
from .gemini_text import RoastWriter
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
        eleven: Optional[ElevenLabsTTS] = None,
        local_tts: Optional[LocalTTS] = None,
        settings=None,
        context_provider: Optional[Callable[[], Dict]] = None,
        on_played: Optional[Callable[[Clip, Dict], None]] = None,
        pool_size: int = 9,
        log_fn=log.info,
    ):
        self.player = player
        self.writer = writer if writer is not None else RoastWriter()
        self.eleven = eleven if eleven is not None else ElevenLabsTTS()
        self.local_tts = local_tts if local_tts is not None else LocalTTS()
        self.settings = settings
        self.context_provider = context_provider or (lambda: {})
        self.on_played = on_played
        self.log = log_fn

        self._recent: Deque[str] = deque(maxlen=10)
        self.insults = InsultLog()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: Live roasts are generated on their own thread so the vision loop never
        #: blocks on the network.
        self._live_queue: "queue.Queue" = queue.Queue(maxsize=4)
        self._live_thread = threading.Thread(target=self._live_loop, name="live-roasts", daemon=True)
        self._api_ready_at = 0.0
        self.pool = RoastPool(
            text_source=self._text_source,
            synthesize=self._synthesize,
            context_provider=self.context_provider,
            voice_of=self._voice_from_path,
            should_refill=self._should_refill_pool,
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
        if not self._live_thread.is_alive():
            self._live_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._live_queue.put_nowait(None)
        except queue.Full:
            pass
        self.pool.stop()
        self.player.stop()

    def _should_refill_pool(self) -> bool:
        """Live roasts and the pool must not both spend the Gemini quota."""
        return self._roast_source() != "live"

    # -- entry points -----------------------------------------------------
    def on_roast_due(self, event: Dict) -> Optional[Clip]:
        count = int(event.get("slouch_count") or 1)
        tier = tier_for(count)

        # Live mode: ask Gemini at the moment of the slouch. The API cooldown
        # keeps a bad stretch from calling the model over and over.
        if self._roast_source() == "live" and self._api_cooldown_ok():
            try:
                self._live_queue.put_nowait((tier, event))
                self._note_api_call()
                return None
            except queue.Full:
                self.log("[voice] live roast queue full; using a pooled clip")
        return self._deliver(self._take_pooled(tier), event)

    def test_roast(self) -> Optional[Clip]:
        """The demo button: always immediate, so the route can report the text."""
        return self._deliver(self._take_pooled("mild"), {"event": "test_roast"})

    def _take_pooled(self, tier: str) -> Clip:
        clip = self.pool.take(tier) or self.pool.take_any()
        if clip is not None:
            return clip
        text = self._fallback_text(tier)
        path = self._synthesize(text, tier)
        return Clip(text=text, tier=tier, path=path, source="fallback", voice=self._voice_from_path(path))

    # -- live generation --------------------------------------------------
    def _roast_source(self) -> str:
        if self.settings is None:
            return "pool"
        source = str(self.settings.get("roast_source") or "pool").lower()
        return source if source in {"live", "pool"} else "pool"

    def _api_cooldown_seconds(self) -> float:
        if self.settings is None:
            return 30.0
        try:
            return max(0.0, float(self.settings.get("live_api_cooldown_seconds") or 0.0))
        except (TypeError, ValueError):
            return 30.0

    def _api_cooldown_ok(self) -> bool:
        return time.time() >= self._api_ready_at

    def _note_api_call(self) -> None:
        self._api_ready_at = time.time() + self._api_cooldown_seconds()

    def _live_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._live_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            tier, event = job
            try:
                self._deliver(self._make_live_clip(tier, event), event)
            except Exception:  # a failed live roast must not kill the thread
                log.warning("live roast generation failed", exc_info=True)

    def _make_live_clip(self, tier: str, event: Dict) -> Clip:
        """One brand-new roast from Gemini, then voice it."""
        items = self.writer.generate(
            count=1,
            tier=tier,
            context=self.context_provider() or {},
            nice=self._nice(),
            exclude=self._exclude_list(),
        )
        items = [item for item in items if item.get("text")]
        if not items:
            return self._take_pooled(tier)
        item = items[0]
        source = str(item.get("source") or "gemini")
        if source == "fallback":
            # Gemini had nothing new; an already-voiced pooled clip beats a repeat.
            pooled = self.pool.take(tier) or self.pool.take_any()
            if pooled is not None:
                return pooled
        text = self._personalise_text(item["text"])
        path = self._synthesize(text, tier)
        return Clip(text=text, tier=tier, path=path, source=source, voice=self._voice_from_path(path))

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
        """auto = ElevenLabs when it can, else local; or force one for consistency."""
        if self.settings is None:
            return "auto"
        mode = str(self.settings.get("tts_mode") or "auto").lower()
        return mode if mode in {"auto", "elevenlabs", "local"} else "auto"

    @staticmethod
    def _voice_from_path(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        name = str(path)
        if name.endswith("_gemini.wav"):
            return "gemini"  # clips left over from when Gemini TTS was wired in
        if name.endswith("_local.wav"):
            return "local"
        if name.endswith("_elevenlabs.wav"):
            return "elevenlabs"
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
            if voice == "elevenlabs":
                if mode == "local" or not self.eleven.available:
                    return None
                return str(path) if self.eleven.synthesize(text, path, tier) else None
            if mode == "elevenlabs" or not self.local_tts.available:
                return None
            return str(path) if self.local_tts.synthesize(text, path) else None

        if mode == "local":
            return attempt("local")
        if mode == "elevenlabs":
            return attempt("elevenlabs")
        # auto: ElevenLabs when a key is present, otherwise the offline voice
        return attempt("elevenlabs") or attempt("local")

    # -- status -----------------------------------------------------------
    def status(self) -> Dict:
        with self._lock:
            recent = list(self._recent)
        return {
            "gemini_text": self.writer.available,
            "gemini_text_error": self.writer.last_error,
            "elevenlabs": self.eleven.available,
            "elevenlabs_voice": self.eleven.voice_name,
            "elevenlabs_error": self.eleven.last_error,
            "local_tts": self.local_tts.engine,
            "tts_mode": self._tts_mode(),
            "roast_source": self._roast_source(),
            "live_pending": self._live_queue.qsize(),
            "api_cooldown_remaining": round(max(0.0, self._api_ready_at - time.time()), 1),
            "insults_used": self.insults.count,
            "insults_file": str(self.insults.path),
            "nice_mode": self._nice(),
            "recent": recent[-5:],
            "pool": self.pool.status(),
        }
