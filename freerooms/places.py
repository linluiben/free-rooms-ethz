"""Study places (study spaces, libraries) and their live occupancy samples."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Traffic light for sources that only give a number (ETH sends its own status). ETH does not
# publish its thresholds, so these are ours: green below 70 % occupied, yellow below 90 %, else red.
GREEN_BELOW, YELLOW_BELOW = 70.0, 90.0
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
    note: str | None = None       # caveat for the UI, e.g. "mapping not verified"


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
                              lat, lon, source, url, note, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, uni=excluded.uni, campus=excluded.campus,
             building=excluded.building, name=excluded.name, seats=COALESCE(excluded.seats, places.seats), hours_json=excluded.hours_json,
             features_json=excluded.features_json, lat=COALESCE(excluded.lat, places.lat),
             lon=COALESCE(excluded.lon, places.lon), source=excluded.source, url=excluded.url, note=excluded.note,
             updated_at=excluded.updated_at""",
        [(p.id, p.parent_id, p.uni, p.campus, p.building, p.name, p.seats,
          None if p.hours is None else json.dumps(p.hours, ensure_ascii=False),
          json.dumps(p.features, ensure_ascii=False), p.lat, p.lon, p.source, p.url, p.note, now) for p in places])
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


# Our own trend, for samples without one from the source (UZH libraries, ICBP floors). We compare the
# latest pct with the newest sample that is 15-30 min older; nothing in that window (a gap) -> no trend.
TREND_MIN_S, TREND_MAX_S = 15 * 60, 30 * 60
# A change below 5 % of capacity counts as steady (ETH says its trend reacts to about 5 %).
TREND_DEAD_BAND = 5.0
# Sources that only report pct in steps: a change needs at least one step, seen in the last two samples.
PCT_STEP = {"uzh_ub": 5.0}


def computed_trend(series: list[dict], step: float | None = None) -> int | None:
    """-1 / 0 / 1 from a place's samples (oldest first, the latest last; dicts with ts, pct, closed).

    None when the latest sample has no number or is closed, when there is no sample 15-30 min
    before it, or when a sample from that one on is closed or has no number (no trend across
    a closing or a no-data stretch). With `step` (rounded source), the change must be at least one
    step and the sample before the latest must show it too; otherwise it counts as steady.
    """
    if not series:
        return None
    last = series[-1]
    if last["closed"] or last["pct"] is None:
        return None
    t_last = datetime.fromisoformat(last["ts"])
    ref = None
    for i in range(len(series) - 2, -1, -1):
        age = (t_last - datetime.fromisoformat(series[i]["ts"])).total_seconds()
        if age >= TREND_MIN_S:
            ref = i if age <= TREND_MAX_S else None
            break
    if ref is None:
        return None
    path = series[ref:]
    if any(s["closed"] or s["pct"] is None for s in path):
        return None
    band = max(TREND_DEAD_BAND, step or 0.0)
    delta = last["pct"] - path[0]["pct"]
    if abs(delta) < band:
        return 0
    sign = 1 if delta > 0 else -1
    if step:
        prev = series[-2]
        if prev is path[0] or (prev["pct"] - path[0]["pct"]) * sign < band:
            return 0
    return sign


def latest(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Every place with its most recent sample (or None) and that sample's age in seconds.

    `latest.trend` is the source's trend if it sent one (`trend_source` 'source'), else our own
    from the last 30 min of samples (`computed_trend`, 'computed'), else None. One query: each
    place joined with its samples from the 30 min up to its newest one.
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        f"""SELECT p.*, o.ts, o.status, o.pct, o.count, o.max, o.trend, o.closed
           FROM places p
           LEFT JOIN (SELECT place_id, MAX(ts) AS last FROM occupancy GROUP BY place_id) m ON m.place_id = p.id
           LEFT JOIN occupancy o ON o.place_id = p.id AND o.ts <= m.last
             AND o.ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00', m.last, '-{TREND_MAX_S} seconds')
           ORDER BY p.uni, p.campus, p.name, p.id, o.ts""").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["id"], []).append(r)
    out = []
    for rs in groups.values():
        r = rs[-1]
        sample = None
        if r["ts"]:
            sample = {k: r[k] for k in ("ts", "status", "pct", "count", "max", "trend")}
            sample["closed"] = bool(r["closed"])
            sample["age_s"] = int((now - datetime.fromisoformat(r["ts"])).total_seconds())
            sample["trend_source"] = "source" if r["trend"] is not None else None
            if r["trend"] is None:
                series = [{"ts": x["ts"], "pct": x["pct"], "closed": bool(x["closed"])} for x in rs]
                sample["trend"] = computed_trend(series, PCT_STEP.get(r["source"]))
                sample["trend_source"] = None if sample["trend"] is None else "computed"
        out.append({
            "id": r["id"], "parent_id": r["parent_id"], "uni": r["uni"], "campus": r["campus"],
            "building": r["building"], "name": r["name"], "seats": r["seats"],
            "hours": json.loads(r["hours_json"]) if r["hours_json"] else None,
            "features": json.loads(r["features_json"]) if r["features_json"] else [],
            "lat": r["lat"], "lon": r["lon"], "source": r["source"], "url": r["url"], "note": r["note"], "latest": sample,
        })
    return out


def samples_between(conn: sqlite3.Connection, place_ids: list[str], start: str, end: str) -> dict[str, list[dict]]:
    """Samples with start <= ts < end (UTC ISO) per place id, oldest first. A run of no-data
    samples is stored once (see add_samples), so such a row means "unknown until the next one"."""
    out: dict[str, list[dict]] = {pid: [] for pid in place_ids}
    rows = conn.execute(
        f"""SELECT place_id, ts, status, pct, count, max, trend, closed FROM occupancy
            WHERE place_id IN ({",".join("?" * len(place_ids))}) AND ts >= ? AND ts < ? ORDER BY ts""",
        (*place_ids, start, end))
    for r in rows:
        s = {k: r[k] for k in ("ts", "pct", "count", "max", "status", "trend")}
        s["closed"] = bool(r["closed"])
        out[r["place_id"]].append(s)
    return out
