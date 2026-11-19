"""Study places (study spaces, libraries) and their live occupancy samples."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Traffic light for sources that only give a number (ETH sends its own status). ETH does not
# publish its thresholds, so these are ours: green below 60 % occupied, yellow below 85 %, else red.
GREEN_BELOW, YELLOW_BELOW = 60.0, 85.0
_warned_unknown: set[str] = set()   # place ids already warned about (once per process)


def status_for(pct: float | None, closed: bool = False) -> str | None:
    """Shared traffic light: None when closed (check `closed` first), 'unknown' without a number."""
    if closed:
        return None
    if pct is None:
        return "unknown"
    return "green" if pct < GREEN_BELOW else "yellow" if pct < YELLOW_BELOW else "red"


@dataclass
class Place:
    id: str                       # '<source>:<native id>'
    uni: str                      # 'ETH' | 'UZH'
    name: str
    source: str
    parent_id: str | None = None
    campus: str | None = None
    building: str | None = None
    seats: int | None = None
    hours: object = None          # stored as JSON, shape is up to the source
    features: list = field(default_factory=list)  # [{"key": ..., "label": ...}]
    lat: float | None = None
    lon: float | None = None
    url: str | None = None


@dataclass
class Sample:
    place_id: str
    ts: str                       # UTC ISO, seconds
    status: str | None = None     # 'green' | 'yellow' | 'red' | 'unknown'
    pct: float | None = None      # share OCCUPIED, 0-100
    count: float | None = None
    max: int | None = None
    trend: int | None = None      # -1 / 0 / 1
    closed: bool = False


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def upsert_places(conn: sqlite3.Connection, places: list[Place]) -> int:
    now = utc_iso()
    conn.executemany(
        """INSERT INTO places(id, parent_id, uni, campus, building, name, seats, hours_json, features_json,
                              lat, lon, source, url, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, uni=excluded.uni, campus=excluded.campus,
             building=excluded.building, name=excluded.name, seats=COALESCE(excluded.seats, places.seats), hours_json=excluded.hours_json,
             features_json=excluded.features_json, lat=COALESCE(excluded.lat, places.lat),
             lon=COALESCE(excluded.lon, places.lon), source=excluded.source, url=excluded.url,
             updated_at=excluded.updated_at""",
        [(p.id, p.parent_id, p.uni, p.campus, p.building, p.name, p.seats,
          None if p.hours is None else json.dumps(p.hours, ensure_ascii=False),
          json.dumps(p.features, ensure_ascii=False), p.lat, p.lon, p.source, p.url, now) for p in places])
    return len(places)


def add_samples(conn: sqlite3.Connection, samples: list[Sample]) -> int:
    """Store samples; returns how many were new.

    Skipped: a repeated (place, ts); a sample for a place we don't know (its metadata source
    hasn't run, or the feed lists rooms that aren't study places; warned once per place); and a
    sample without a number when the place's previous sample had none either, with the same
    status and closed flag (otherwise every silent sensor would add a row every 5 minutes).
    """
    known = {r[0] for r in conn.execute("SELECT id FROM places")}
    new_unknown = sorted({s.place_id for s in samples} - known - _warned_unknown)
    if new_unknown:
        _warned_unknown.update(new_unknown)
        log.warning("samples for %d places not in the places table skipped (not repeated): %s",
                    len(new_unknown), ", ".join(new_unknown[:5]) + (" ..." if len(new_unknown) > 5 else ""))
    last = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        """SELECT o.place_id, o.pct, o.status, o.closed FROM occupancy o
           WHERE o.ts = (SELECT MAX(ts) FROM occupancy WHERE place_id = o.place_id)""")}
    rows = []
    for s in samples:
        if s.place_id not in known:
            continue
        if s.pct is None and s.place_id in last and last[s.place_id] == (None, s.status, int(s.closed)):
            continue
        rows.append((s.place_id, s.ts, s.status, s.pct, s.count, s.max, s.trend, int(s.closed)))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO occupancy(place_id, ts, status, pct, count, max, trend, closed) VALUES (?,?,?,?,?,?,?,?)",
        rows)
    return conn.total_changes - before


def latest(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Every place with its most recent sample (or None) and that sample's age in seconds."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT p.*, o.ts, o.status, o.pct, o.count, o.max, o.trend, o.closed
           FROM places p LEFT JOIN occupancy o ON o.place_id = p.id
             AND o.ts = (SELECT MAX(ts) FROM occupancy WHERE place_id = p.id)
           ORDER BY p.uni, p.campus, p.name""").fetchall()
    out = []
    for r in rows:
        sample = None
        if r["ts"]:
            sample = {k: r[k] for k in ("ts", "status", "pct", "count", "max", "trend")}
            sample["closed"] = bool(r["closed"])
            sample["age_s"] = int((now - datetime.fromisoformat(r["ts"])).total_seconds())
        out.append({
            "id": r["id"], "parent_id": r["parent_id"], "uni": r["uni"], "campus": r["campus"],
            "building": r["building"], "name": r["name"], "seats": r["seats"],
            "hours": json.loads(r["hours_json"]) if r["hours_json"] else None,
            "features": json.loads(r["features_json"]) if r["features_json"] else [],
            "lat": r["lat"], "lon": r["lon"], "source": r["source"], "url": r["url"], "latest": sample,
        })
    return out
