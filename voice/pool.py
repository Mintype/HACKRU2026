"""The pre-generated roast pool.

A background worker keeps ~8-10 ready-to-play clips per tier.  Taking a clip is
instant and non-blocking, so the roast path never waits on the network.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


@dataclass
class Clip:
    text: str
    tier: str = "mild"
    path: Optional[str] = None
    source: str = "gemini"
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict:
        return {"text": self.text, "tier": self.tier, "path": self.path, "source": self.source}


class RoastPool(threading.Thread):
    """Keeps buckets of ready clips, refilling them in the background.

    ``text_source(tier, count) -> [{"text": ..., "tier": ...}]``
    ``synthesize(text) -> path | None``
    """

    def __init__(
        self,
        *,
        text_source: Callable[[str, int], List[Dict]],
        synthesize: Callable[[str], Optional[str]],
        context_provider: Optional[Callable[[], Dict]] = None,
        tiers: Sequence[str] = ("mild", "medium", "spicy"),
        size: int = 9,
        refill_interval: float = 1.5,
        max_batch: int = 4,
        log_fn: Callable[[str], None] = log.info,
    ):
        super().__init__(name="roast-pool", daemon=True)
        self.text_source = text_source
        self.synthesize = synthesize
        self.context_provider = context_provider
        self.tiers = tuple(tiers)
        self.size = max(1, int(size))
        self.refill_interval = float(refill_interval)
        self.max_batch = max(1, int(max_batch))
        self.log = log_fn

        self._lock = threading.Lock()
        self._buckets: Dict[str, Deque[Clip]] = {tier: deque() for tier in self.tiers}
        self._stop = threading.Event()
        self.generated = 0
        self.failures = 0
        self.last_refill_at: Optional[float] = None

    # -- public API -------------------------------------------------------
    def take(self, tier: str) -> Optional[Clip]:
        with self._lock:
            bucket = self._buckets.get(tier)
            if bucket:
                return bucket.popleft()
        return None

    def take_any(self) -> Optional[Clip]:
        with self._lock:
            best_tier = max(self._buckets, key=lambda t: len(self._buckets[t]), default=None)
            if best_tier and self._buckets[best_tier]:
                return self._buckets[best_tier].popleft()
        return None

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {tier: len(bucket) for tier, bucket in self._buckets.items()}

    @property
    def ready(self) -> int:
        return sum(self.counts().values())

    def status(self) -> Dict:
        return {
            "ready": self.ready,
            "target": self.size * len(self.tiers),
            "counts": self.counts(),
            "generated": self.generated,
            "failures": self.failures,
            "last_refill_at": self.last_refill_at,
            "running": self.is_alive() and not self._stop.is_set(),
        }

    def stop(self) -> None:
        self._stop.set()

    # -- worker -----------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refill_once()
            except Exception as exc:  # never let the pool thread die
                self.failures += 1
                log.warning("roast pool refill failed: %s", exc)
            self._stop.wait(self.refill_interval)

    def refill_once(self) -> int:
        """Top up every bucket that is below target; returns clips produced."""
        produced = 0
        context = self.context_provider() if self.context_provider else {}
        for tier in self.tiers:
            with self._lock:
                need = self.size - len(self._buckets[tier])
            if need <= 0:
                continue
            batch = min(need, self.max_batch)
            try:
                items = self.text_source(tier, batch) or []
            except Exception as exc:
                self.failures += 1
                log.warning("text source failed for %s: %s", tier, exc)
                continue
            for item in items:
                if self._stop.is_set():
                    return produced
                text = (item or {}).get("text")
                if not text:
                    continue
                path = None
                try:
                    path = self.synthesize(text)
                except Exception as exc:
                    self.failures += 1
                    log.warning("TTS failed for %r: %s", text, exc)
                clip = Clip(
                    text=text,
                    tier=str(item.get("tier") or tier),
                    path=path,
                    source=str(item.get("source") or "gemini"),
                )
                with self._lock:
                    self._buckets[tier].append(clip)
                self.generated += 1
                produced += 1
        self.last_refill_at = time.time()
        return produced
