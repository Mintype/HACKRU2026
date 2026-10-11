"""Central paths and a dependency-free ``.env`` loader for Posture Coach.

Everything that touches the filesystem imports its paths from here so the
project can be relocated (laptop -> Raspberry Pi) without edits.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Runtime data (SQLite db, generated roast audio, saved settings).
DATA_DIR = Path(os.environ.get("POSTURE_DATA_DIR") or (BASE_DIR / "data"))
ROAST_DIR = DATA_DIR / "roasts"
DB_PATH = DATA_DIR / "posture.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
#: Every insult we have actually said, one per line — the never-repeat list.
INSULTS_PATH = DATA_DIR / "insults.txt"

# MediaPipe ``.task`` model files are downloaded here on first use.
MODELS_DIR = BASE_DIR / "models"

# Flask template + static assets (single-page website).
WEB_DIR = BASE_DIR / "web"

ENV_PATH = BASE_DIR / ".env"


def ensure_dirs() -> None:
    """Create the runtime directories if they are missing."""
    for path in (DATA_DIR, ROAST_DIR, MODELS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_env(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs from ``.env`` without clobbering real env vars.

    Uses ``python-dotenv`` when installed and falls back to a tiny parser so a
    fresh checkout still works before dependencies are installed.
    """
    path = path or ENV_PATH
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(str(path), override=False)
        return
    except Exception:  # pragma: no cover - optional dependency
        pass

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
