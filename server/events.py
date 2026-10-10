"""A tiny thread-safe publish/subscribe broker for server-sent events.

Every connected browser gets its own queue.  Slow clients are dropped rather
than allowed to stall the vision thread.
"""

from __future__ import annotations

import queue
import threading
from typing import Dict, List, Optional


class EventBroker:
    def __init__(self, max_queue: int = 200):
        self._lock = threading.Lock()
        self._subscribers: List[queue.Queue] = []
        self._max_queue = max_queue
        self.published = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: Dict) -> None:
        """Never blocks and never raises: a dead subscriber must not kill vision."""
        self.published += 1
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Client is too slow: drop the oldest item and try once more.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except queue.Empty:  # pragma: no cover
                    pass

    def drain(self, q: queue.Queue, timeout: Optional[float] = None) -> Optional[Dict]:
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
