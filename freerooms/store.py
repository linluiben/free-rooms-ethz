"""SQLite storage. Times are stored as naive Europe/Zurich local ISO strings,
exactly as the API returns them."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
CREATE TABLE IF NOT EXISTS source_health (
    source        TEXT PRIMARY KEY, -- e.g. 'eth_rooms'
    last_attempt  TEXT,             -- UTC ISO
    last_success  TEXT,             -- UTC ISO, data is as old as this
    last_error    TEXT,             -- error of the last attempt, NULL if it went fine
    counts        TEXT,             -- JSON, row counts of the last success
    max_age_s     INTEGER           -- older data counts as stale
);
"""

DEFAULT_MAX_AGE = 4 * 3600


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
    # DBs from before source_health: take the room fetch time from meta.
    if conn.execute("INSERT OR IGNORE INTO source_health(source, last_attempt, last_success, max_age_s) "
                    "SELECT 'eth_rooms', value, value, ? FROM meta WHERE key='last_fetch'",
                    (DEFAULT_MAX_AGE,)).rowcount:
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_attempt(conn: sqlite3.Connection, source: str, error: str | None = None,
                   counts: dict | None = None, max_age: float | None = None) -> None:
    """Note a fetch attempt for `source`: a success (error=None, with row counts) or a failure.
    A failure keeps the last success and its counts, so the data age stays honest."""
    now = _utcnow()
    max_age_s = int(max_age or DEFAULT_MAX_AGE)
    if error is None:
        conn.execute(
            """INSERT INTO source_health(source, last_attempt, last_success, last_error, counts, max_age_s)
               VALUES (?,?,?,NULL,?,?)
               ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,
                 last_success=excluded.last_success, last_error=NULL, counts=excluded.counts,
                 max_age_s=excluded.max_age_s""",
            (source, now, now, json.dumps(counts or {}), max_age_s))
    else:
        conn.execute(
            """INSERT INTO source_health(source, last_attempt, last_error, max_age_s) VALUES (?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,
                 last_error=excluded.last_error, max_age_s=excluded.max_age_s""",
            (source, now, error[:500], max_age_s))
    conn.commit()


def health(conn: sqlite3.Connection, sources: tuple[str, ...] = (), now: datetime | None = None) -> dict:
    """Freshness per source. `sources` are always listed, even if never fetched (then stale)."""
    now = now or datetime.now(timezone.utc)
    rows = {r["source"]: r for r in conn.execute("SELECT * FROM source_health")}
    out = {}
    for key in dict.fromkeys((*sources, *sorted(rows))):
        r = rows.get(key)
        max_age = r["max_age_s"] if r and r["max_age_s"] else DEFAULT_MAX_AGE
        ok = r["last_success"] if r else None
        age = int((now - datetime.fromisoformat(ok)).total_seconds()) if ok else None
        out[key] = {
            "last_attempt": r["last_attempt"] if r else None,
            "last_success": ok,
            "last_error": r["last_error"] if r else None,
            "counts": json.loads(r["counts"]) if r and r["counts"] else {},
            "age_s": age,
            "max_age_s": max_age,
            "stale": age is None or age > max_age,
        }
    status = "stale" if any(s["stale"] for s in out.values()) else "ok"
    return {"status": status, "now": now.isoformat(timespec="seconds"), "sources": out}
