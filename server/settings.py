"""Runtime settings: persisted to JSON so they survive a restart.

The website reads and writes these through ``GET/POST /api/settings``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import config
from .state_machine import StateMachineConfig

DEFAULTS: Dict[str, Any] = {
    # state machine
    "warn_seconds": 10.0,
    "roast_seconds": 30.0,
    "cooldown_seconds": 90.0,
    "good_debounce_seconds": 2.0,
    "calibration_seconds": 5.0,
    "lower_threshold": 0.7,
    "upper_threshold": 0.8,
    # vision
    "delta_angle": 20.0,
    "camera_index": 0,
    "model": "lite",
    # voice / website
    "nice_mode": False,
    "mirror": False,
    "audio_route": "bluetooth",  # bluetooth | cast | off
    "roast_pool_size": 9,
    "cast_name": "",
}

BOUNDS: Dict[str, tuple] = {
    "warn_seconds": (1.0, 600.0),
    "roast_seconds": (2.0, 3600.0),
    "cooldown_seconds": (0.0, 3600.0),
    "good_debounce_seconds": (0.0, 60.0),
    "calibration_seconds": (1.0, 60.0),
    "lower_threshold": (0.05, 1.0),
    "upper_threshold": (0.05, 1.0),
    "delta_angle": (1.0, 60.0),
    "camera_index": (0, 16),
    "roast_pool_size": (1, 50),
}

AUDIO_ROUTES = ("bluetooth", "cast", "off")
MODELS = ("lite", "full", "heavy")


class Settings:
    def __init__(self, path: Optional[str | Path] = None, overrides: Optional[Dict[str, Any]] = None):
        self.path = Path(path or config.SETTINGS_PATH)
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        self._load()
        if overrides:
            for key, value in overrides.items():
                if value is not None:
                    self._values[key] = value

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path.exists():
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    for key, value in stored.items():
                        if key in self._values:
                            self._values[key] = value
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -- access -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + apply a partial update; return the applied values."""
        applied: Dict[str, Any] = {}
        with self._lock:
            for key, value in (patch or {}).items():
                if key not in DEFAULTS:
                    continue
                try:
                    coerced = self._coerce(key, value)
                except (TypeError, ValueError):
                    continue
                self._values[key] = coerced
                applied[key] = coerced
            self._save()
        return applied

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if isinstance(default, int):
            return int(round(float(value)))
        if isinstance(default, float):
            out = float(value)
            low, high = BOUNDS.get(key, (float("-inf"), float("inf")))
            return max(low, min(high, out))
        if key == "audio_route":
            text = str(value).strip().lower()
            return text if text in AUDIO_ROUTES else DEFAULTS[key]
        if key == "model":
            text = str(value).strip().lower()
            return text if text in MODELS else DEFAULTS[key]
        return value

    # -- wiring -----------------------------------------------------------
    def apply_to(self, machine_config: StateMachineConfig) -> None:
        """Copy live-tunable values onto the running state machine config."""
        mapping = (
            "warn_seconds",
            "roast_seconds",
            "cooldown_seconds",
            "good_debounce_seconds",
            "calibration_seconds",
            "lower_threshold",
            "upper_threshold",
        )
        with self._lock:
            for key in mapping:
                setattr(machine_config, key, float(self._values[key]))

    def machine_config(self) -> StateMachineConfig:
        cfg = StateMachineConfig()
        self.apply_to(cfg)
        return cfg
