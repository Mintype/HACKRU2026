"""A plain-text record of every insult we have said, and the never-repeat list.

Stored as one line per insult in ``data/insults.txt`` so it stays readable and
hand-editable.  The writer feeds the tail of this list to Gemini as a
do-not-repeat list, and it is also used to stop the hand-written fallbacks from
recycling lines that have already been said.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

import config


def normalise(text: str) -> str:
    """Case/whitespace-insensitive key for comparisons."""
    return " ".join((text or "").lower().split())


class InsultLog:
    def __init__(self, path: Optional[str | Path] = None, max_exclude: int = 300):
        self.path = Path(path or config.INSULTS_PATH)
        self.max_exclude = int(max_exclude)
        self._lock = threading.Lock()
        self._lines: Optional[List[str]] = None

    # -- reading ----------------------------------------------------------
    def _load(self) -> List[str]:
        if self._lines is None:
            try:
                raw = self.path.read_text(encoding="utf-8")
                self._lines = [line.strip() for line in raw.splitlines() if line.strip()]
            except OSError:
                self._lines = []
        return self._lines

    def used(self, limit: Optional[int] = None) -> List[str]:
        """All recorded insults, newest last (optionally just the last ``limit``)."""
        with self._lock:
            lines = list(self._load())
        if limit and limit > 0:
            return lines[-limit:]
        return lines

    def has(self, text: str) -> bool:
        key = normalise(text)
        with self._lock:
            return any(normalise(line) == key for line in self._load())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._load())

    # -- writing ----------------------------------------------------------
    def add(self, text: str) -> bool:
        """Record a line we actually said. Returns False if blank or a repeat."""
        clean = " ".join((text or "").split())
        if not clean:
            return False
        key = normalise(clean)
        with self._lock:
            lines = self._load()
            if any(normalise(line) == key for line in lines):
                return False
            lines.append(clean)
            self._lines = lines
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(clean + "\n")
            except OSError:
                return False
        return True
