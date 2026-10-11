"""SQLite logging: sessions, posture readings, state changes and roasts.

Each thread gets its own connection (SQLite connections must not be shared
across threads).  Session statistics are computed from the log, not kept in
memory, so a browser refresh can always rebuild the whole session.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       REAL    NOT NULL,
    ended_at         REAL,
    neck_baseline    REAL,
    torso_baseline   REAL,
    slouch_count     INTEGER DEFAULT 0,
    roasts           INTEGER DEFAULT 0,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    ts          REAL    NOT NULL,
    person      INTEGER,
    seated      INTEGER,
    neck_angle  REAL,
    torso_angle REAL,
    score       REAL,
    state       TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS state_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    ts          REAL    NOT NULL,
    state       TEXT    NOT NULL,
    prev        TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS roasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    ts          REAL    NOT NULL,
    text        TEXT    NOT NULL,
    tier        TEXT,
    source      TEXT,
    slouch_count INTEGER,
    neck_angle  REAL,
    voice       TEXT,
    origin      TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_readings_session ON readings(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_state_session ON state_changes(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_roasts_session ON roasts(session_id, ts);
"""


class Database:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # -- connections ------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = sqlite3.connect(str(self.path), timeout=15)
        try:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(roasts)")}
        for column, ddl in (
            ("voice", "ALTER TABLE roasts ADD COLUMN voice TEXT"),
            ("origin", "ALTER TABLE roasts ADD COLUMN origin TEXT"),
        ):
            if column not in existing:
                conn.execute(ddl)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- writes -----------------------------------------------------------
    def start_session(self, ts: Optional[float] = None, neck_baseline=None, torso_baseline=None) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO sessions (started_at, neck_baseline, torso_baseline) VALUES (?, ?, ?)",
            (float(ts if ts is not None else time.time()), neck_baseline, torso_baseline),
        )
        conn.commit()
        return int(cur.lastrowid)

    def set_session_baseline(self, session_id: Optional[int], neck: float, torso: float) -> None:
        if session_id is None:
            return
        conn = self._conn()
        conn.execute(
            "UPDATE sessions SET neck_baseline = ?, torso_baseline = ? WHERE id = ?",
            (float(neck), float(torso), int(session_id)),
        )
        conn.commit()

    def end_session(self, session_id: Optional[int], ts: Optional[float] = None) -> None:
        if session_id is None:
            return
        conn = self._conn()
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (float(ts if ts is not None else time.time()), int(session_id)),
        )
        conn.commit()

    def log_reading(self, session_id: Optional[int], reading: Dict) -> None:
        conn = self._conn()
        raw_ts = reading.get("ts")
        ts = float(raw_ts) if raw_ts is not None else time.time()
        conn.execute(
            """INSERT INTO readings
               (session_id, ts, person, seated, neck_angle, torso_angle, score, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                1 if reading.get("person") else 0,
                1 if reading.get("seated") else 0,
                reading.get("neck_angle"),
                reading.get("torso_angle"),
                reading.get("score"),
                reading.get("state"),
            ),
        )
        conn.commit()

    def log_state(self, session_id: Optional[int], ts: float, state: str, prev: Optional[str] = None) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO state_changes (session_id, ts, state, prev) VALUES (?, ?, ?, ?)",
            (session_id, float(ts), state, prev),
        )
        conn.commit()

    def log_roast(
        self,
        session_id: Optional[int],
        ts: float,
        text: str,
        tier: Optional[str] = None,
        source: Optional[str] = None,
        slouch_count: Optional[int] = None,
        neck_angle: Optional[float] = None,
        voice: Optional[str] = None,
        origin: str = "auto",
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO roasts
               (session_id, ts, text, tier, source, slouch_count, neck_angle, voice, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, float(ts), text, tier, source, slouch_count, neck_angle, voice, origin),
        )
        conn.commit()
        return int(cur.lastrowid)

    # -- reads ------------------------------------------------------------
    def latest_session_id(self) -> Optional[int]:
        row = self._conn().execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def history(self, session_id: Optional[int], limit: int = 1800) -> List[Dict]:
        if session_id is None:
            return []
        rows = self._conn().execute(
            """SELECT ts, score, state, neck_angle, torso_angle FROM readings
               WHERE session_id = ? ORDER BY ts DESC LIMIT ?""",
            (int(session_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def session_roasts(self, session_id: Optional[int], limit: int = 25) -> List[Dict]:
        if session_id is None:
            return []
        rows = self._conn().execute(
            """SELECT ts, text, tier, source, voice, origin, slouch_count, neck_angle
               FROM roasts WHERE session_id = ? ORDER BY ts DESC LIMIT ?""",
            (int(session_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_roasts(self, limit: int = 25) -> List[Dict]:
        """Every roast, newest first, regardless of session.

        The hall of shame uses this so test roasts - and anything said while no
        session was running, which has no session id - still show up.
        """
        rows = self._conn().execute(
            """SELECT ts, text, tier, source, voice, origin, slouch_count, neck_angle
               FROM roasts ORDER BY ts DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def roast_markers(self, session_id: Optional[int]) -> List[Dict]:
        if session_id is None:
            return []
        rows = self._conn().execute(
            "SELECT ts, text, neck_angle FROM roasts WHERE session_id = ? ORDER BY ts",
            (int(session_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def session_stats(self, session_id: Optional[int]) -> Dict:
        empty = {
            "session_id": session_id,
            "duration_seconds": 0.0,
            "good_percent": 0.0,
            "slouch_count": 0,
            "roasts": 0,
            "longest_good_streak": 0.0,
            "avg_score": None,
            "worst_neck_angle": None,
            "readings": 0,
        }
        if session_id is None:
            return empty

        rows = self._conn().execute(
            """SELECT ts, score, state, neck_angle FROM readings
               WHERE session_id = ? ORDER BY ts""",
            (int(session_id),),
        ).fetchall()
        roasts = self._conn().execute(
            "SELECT COUNT(*) AS n FROM roasts WHERE session_id = ?", (int(session_id),)
        ).fetchone()["n"]
        slouches = self._conn().execute(
            "SELECT COUNT(*) AS n FROM state_changes WHERE session_id = ? AND state = 'slouching'",
            (int(session_id),),
        ).fetchone()["n"]

        if not rows:
            empty["roasts"] = int(roasts)
            empty["slouch_count"] = int(slouches)
            return empty

        good = 0
        scores: List[float] = []
        necks: List[float] = []
        longest = 0.0
        current = 0.0
        prev_ts = None
        for row in rows:
            dt = 0.0 if prev_ts is None else max(0.0, min(float(row["ts"]) - prev_ts, 5.0))
            prev_ts = float(row["ts"])
            if row["score"] is not None:
                scores.append(float(row["score"]))
            if row["neck_angle"] is not None:
                necks.append(float(row["neck_angle"]))
            if row["state"] == "good":
                good += 1
                current += dt
                longest = max(longest, current)
            else:
                current = 0.0

        duration = float(rows[-1]["ts"]) - float(rows[0]["ts"])
        return {
            "session_id": session_id,
            "duration_seconds": round(duration, 1),
            "good_percent": round(100.0 * good / len(rows), 1),
            "slouch_count": int(slouches),
            "roasts": int(roasts),
            "longest_good_streak": round(longest, 1),
            "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
            "worst_neck_angle": round(max(necks), 1) if necks else None,
            "readings": len(rows),
        }

    def all_sessions(self, limit: int = 20) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT id, started_at, ended_at FROM sessions ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for row in rows:
            stats = self.session_stats(int(row["id"]))
            stats.update({"started_at": row["started_at"], "ended_at": row["ended_at"]})
            out.append(stats)
        return out
