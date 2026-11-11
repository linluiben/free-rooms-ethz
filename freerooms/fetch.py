"""Fetch room list + allocations from the API and store them in SQLite."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from . import store
from .api import ApiClient, query_id
from .availability import TZ

log = logging.getLogger(__name__)

# Lecture halls, seminar rooms, computer rooms.
DEFAULT_TYPECODES = ("3430", "3420", "3650")
DEFAULT_SITES = ("Zürich Zentrum", "Zürich Hönggerberg")

ROOMS_FILE = Path(__file__).with_name("rooms.txt")


def load_allowlist(path: Path = ROOMS_FILE) -> frozenset[str]:
    """Centrally managed rooms ("building floor room"), see rooms.txt for why."""
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return frozenset(ln for ln in lines if ln and not ln.startswith("#"))


CLOSED_TYPES = {7, 8}     # 8: nightly/weekend building closure, 7: holiday closure
CANCELLED_TYPES = {13}    # cancelled event -> room is actually free
# 2: regular booking (lecture, exercise, meeting, event)
# 5: construction / repair / technical reservation ("Umbau", "Reparatur")
# 11: seen on doctoral courses (D-MATH), otherwise like 2
BUSY_TYPES = {2, 5, 11}


def normalize(alloc: dict) -> tuple[str, str, str, str | None, int] | None:
    """Turn one raw allocation into (start, end, kind, title, raw_type), or None to drop it."""
    serie = alloc.get("belegungsserie") or {}
    btype = serie.get("belegungstyp")
    event = serie.get("veranstaltung") or {}
    if btype in CANCELLED_TYPES or event.get("findetStatt") == 0:
        return None
    if btype in CLOSED_TYPES:
        kind = "closed"
    else:
        if btype not in BUSY_TYPES:
            log.warning("unknown belegungstyp %r, treating as busy: %s", btype, alloc)
        kind = "busy"
    return (alloc["date_from"], alloc["date_to"], kind, event.get("allocationTitle"), btype)


def select_rooms(rooms: list[dict], typecodes=DEFAULT_TYPECODES, sites=DEFAULT_SITES,
                 buildings: tuple[str, ...] | None = None,
                 allowlist: frozenset[str] | None = None) -> list[dict]:
    out = []
    for r in rooms:
        if allowlist is not None and query_id(r) not in allowlist:
            continue
        if typecodes and r.get("typecode") not in typecodes:
            continue
        if sites and r.get("locationName") not in sites:
            continue
        if buildings and r.get("building") not in buildings:
            continue
        out.append(r)
    return out


def run(conn: sqlite3.Connection, client: ApiClient | None = None, days: int = 7,
        buildings: tuple[str, ...] | None = None, sites=DEFAULT_SITES,
        typecodes=DEFAULT_TYPECODES) -> dict:
    client = client or ApiClient()
    all_rooms = client.rooms()
    allow = load_allowlist()
    rooms = select_rooms(all_rooms, typecodes, sites, buildings, allow)
    log.info("room list: %d total, %d selected", len(all_rooms), len(rooms))
    gone = allow - {query_id(r) for r in all_rooms}
    if gone and not buildings:
        log.warning("rooms.txt lists rooms the API no longer knows: %s", ", ".join(sorted(gone)))
    # Drop rooms that are no longer selected (e.g. department rooms from older versions).
    keep = [query_id(r) for r in rooms]
    conn.execute(f"DELETE FROM rooms WHERE id NOT IN ({','.join('?' * len(keep))})", keep)
    conn.commit()

    today = datetime.now(TZ).date()
    start, end = today, today + timedelta(days=days)
    stats = {"rooms": len(rooms), "ok": 0, "empty": 0, "failed": 0, "intervals": 0}

    for i, r in enumerate(rooms, 1):
        rid = query_id(r)
        try:
            allocs = client.allocations(rid, start, end)
        except Exception as e:  # keep old data for this room
            log.error("[%d/%d] %s: fetch failed: %s", i, len(rooms), rid, e)
            stats["failed"] += 1
            continue
        rows = [n for a in allocs if (n := normalize(a)) is not None]
        if not allocs:
            # Every normal room has at least the nightly closure entries.
            log.warning("[%d/%d] %s: empty response (wrong id or unmanaged room?)", i, len(rooms), rid)
            stats["empty"] += 1
        _save_room(conn, r, rid, rows, len(allocs), start, end)
        stats["ok"] += 1
        stats["intervals"] += len(rows)
        log.debug("[%d/%d] %s: %d raw, %d kept", i, len(rooms), rid, len(allocs), len(rows))

    store.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.set_meta(conn, "window", f"{start.isoformat()}/{end.isoformat()}")
    conn.commit()
    log.info("fetch done: %s", stats)
    return stats


def _save_room(conn, r: dict, rid: str, rows, n_raw: int, start: date, end: date) -> None:
    conn.execute(
        """INSERT INTO rooms(id, building, floor, room, type, typecode, seats, workplaces, site, fetched_at, n_entries)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET building=excluded.building, floor=excluded.floor,
             room=excluded.room, type=excluded.type, typecode=excluded.typecode, seats=excluded.seats,
             workplaces=excluded.workplaces,
             site=excluded.site, fetched_at=excluded.fetched_at, n_entries=excluded.n_entries""",
        (rid, r["building"], r["floor"], r["room"], r.get("type"), r.get("typecode"), r.get("seats"),
         r.get("workplaces"), r.get("locationName"), datetime.now(timezone.utc).isoformat(timespec="seconds"), n_raw),
    )
    # Replace everything for this room (also drops past/stale entries).
    conn.execute("DELETE FROM intervals WHERE room_id=?", (rid,))
    conn.executemany(
        "INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES (?,?,?,?,?,?)",
        [(rid, *row) for row in rows],
    )
    conn.commit()
