"""Upright-posture calibration buffer.

On session start (or when the user clicks *Calibrate*) the user sits up
straight for a few seconds; we average both angles over that window and treat
the result as the personal baseline.
"""

from __future__ import annotations

from typing import List, Optional


class Calibrator:
    def __init__(self, duration: float = 5.0, min_samples: int = 8):
        self.duration = float(duration)
        self.min_samples = int(min_samples)
        self.started_at: Optional[float] = None
        self._neck: List[float] = []
        self._torso: List[float] = []

    # -- lifecycle --------------------------------------------------------
    def start(self, ts: float, duration: Optional[float] = None) -> None:
        self.started_at = float(ts)
        if duration is not None:
            self.duration = float(duration)
        self._neck.clear()
        self._torso.clear()

    def reset(self) -> None:
        self.started_at = None
        self._neck.clear()
        self._torso.clear()

    @property
    def active(self) -> bool:
        return self.started_at is not None

    # -- sampling ---------------------------------------------------------
    def add(self, ts: float, neck: float, torso: float) -> bool:
        """Add one sample; return ``True`` once calibration is complete."""
        if self.started_at is None:
            return False
        self._neck.append(float(neck))
        self._torso.append(float(torso))
        elapsed = float(ts) - self.started_at
        return elapsed >= self.duration and len(self._neck) >= self.min_samples

    def progress(self, ts: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, min(1.0, (float(ts) - self.started_at) / max(self.duration, 1e-6)))

    def result(self) -> dict:
        if not self._neck:
            return {"neck": 0.0, "torso": 0.0, "samples": 0}
        return {
            "neck": sum(self._neck) / len(self._neck),
            "torso": sum(self._torso) / len(self._torso),
            "samples": len(self._neck),
        }
