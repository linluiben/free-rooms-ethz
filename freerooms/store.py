"""SQLite storage. Times are stored as naive Europe/Zurich local ISO strings,
exactly as the API returns them."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,   -- query id, e.g. 'HG F 26.3'
    building    TEXT NOT NULL,
    floor       TEXT NOT NULL,
    room        TEXT NOT NULL,
    type        TEXT,
    typecode    TEXT,
    seats       INTEGER,
    workplaces  INTEGER,            -- computer rooms have workplaces instead of seats
    site        TEXT,               -- locationName, e.g. 'Zürich Zentrum'
    fetched_at  TEXT,               -- last successful allocation fetch (UTC ISO)
    n_entries   INTEGER             -- raw entries in last fetch (0 => unknown)
);
CREATE TABLE IF NOT EXISTS intervals (
    room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- 'busy' | 'closed'
    title       TEXT,
    raw_type    INTEGER
);
CREATE INDEX IF NOT EXISTS intervals_room ON intervals(room_id, start);
CREATE TABLE IF NOT EXISTS room_details (
    room_id     TEXT PRIMARY KEY,   -- cached /v2/rooms/{id} response (equipment, windows, ...)
    fetched_at  TEXT NOT NULL,      -- UTC ISO
    json        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)")}
    if "workplaces" not in cols:  # added after the first release
        conn.execute("ALTER TABLE rooms ADD COLUMN workplaces INTEGER")
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
