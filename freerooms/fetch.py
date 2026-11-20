"""Fetch room list + allocations from the API and store them in SQLite."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from . import store
from .api import ApiClient, query_id
from .availability import TZ
from .campus import canonical

log = logging.getLogger(__name__)

# Lecture halls, seminar rooms, computer rooms.
DEFAULT_TYPECODES = ("3430", "3420", "3650")
DEFAULT_SITES = ("Zürich Zentrum", "Zürich Hönggerberg")

ROOMS_FILE = Path(__file__).with_name("rooms.txt")
SOURCE = "eth_rooms"      # key in store.source_health
UNI = "ETH"


def load_allowlist(path: Path = ROOMS_FILE) -> frozenset[str]:
    """Centrally managed rooms ("building floor room"), see rooms.txt for why."""
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return frozenset(ln for ln in lines if ln and not ln.startswith("#"))


CLOSED_TYPES = {7, 8}     # 8: nightly/weekend building closure, 7: holiday closure
CANCELLED_TYPES = {13}    # not held this semester (VVZ: "Findet nicht statt") -> room is free
# 2: regular booking (lecture, exercise, meeting, event)
# 5: construction / repair / technical reservation ("Umbau", "Reparatur")
# 11: seen on doctoral courses (D-MATH), otherwise like 2
BUSY_TYPES = {2, 5, 11}


def validate(alloc: object) -> str | None:
    """Why a raw allocation is unusable, or None if it looks sane.

    A room with any unusable entry is stored as unknown: silently dropping the
    entry could hide a booking and make the room look free.
    """
    if not isinstance(alloc, dict):
        return f"not an object: {type(alloc).__name__}"
    times = []
    for key in ("date_from", "date_to"):
        v = alloc.get(key)
        try:
            dt = datetime.fromisoformat(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return f"invalid {key}: {v!r}"
        if dt.tzinfo is not None:  # we store naive Europe/Zurich local time
            return f"unexpected timezone in {key}: {v!r}"
        times.append(dt)
    if times[1] < times[0]:
        return f"date_to before date_from: {alloc['date_from']} > {alloc['date_to']}"
    serie = alloc.get("belegungsserie")
    if serie is not None and not isinstance(serie, dict):
        return f"belegungsserie is {type(serie).__name__}"
    event = (serie or {}).get("veranstaltung")
    if event is not None and not isinstance(event, dict):
        return f"veranstaltung is {type(event).__name__}"
    return None


def normalize(alloc: dict) -> tuple[str, str, str, str | None, int] | None:
    """Turn one raw allocation into (start, end, kind, title, raw_type), or None to drop it.

    Cancelled entries are kept as kind 'cancelled' for display only; they never block a room.
    """
    serie = alloc.get("belegungsserie") or {}
    btype = serie.get("belegungstyp")
    event = serie.get("veranstaltung") or {}
    if btype in CANCELLED_TYPES or event.get("findetStatt") == 0:
        kind = "cancelled"
    elif btype in CLOSED_TYPES:
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
        typecodes=DEFAULT_TYPECODES, max_age: float | None = None) -> dict:
    """Fetch everything. `max_age` (seconds): when the data should count as stale (default 4 h)."""
    try:
        stats = _run(conn, client, days, buildings, sites, typecodes)
    except Exception as e:
        store.record_attempt(conn, SOURCE, error=f"{type(e).__name__}: {e}", max_age=max_age)
        raise
    if stats["rooms"] and not stats["ok"]:
        store.record_attempt(conn, SOURCE, error=f"all {stats['rooms']} allocation fetches failed", max_age=max_age)
        raise RuntimeError("fetch failed for every room")
    store.record_attempt(conn, SOURCE, counts=stats, max_age=max_age)
    store.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.commit()
    return stats


def _run(conn, client, days, buildings, sites, typecodes) -> dict:
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
    conn.execute(f"DELETE FROM rooms WHERE uni=? AND id NOT IN ({','.join('?' * len(keep))})", (UNI, *keep))
    conn.commit()

    today = datetime.now(TZ).date()
    start, end = today, today + timedelta(days=days)
    stats = {"rooms": len(rooms), "ok": 0, "empty": 0, "failed": 0, "malformed": 0, "intervals": 0}

    for i, r in enumerate(rooms, 1):
        rid = query_id(r)
        try:
            allocs = client.allocations(rid, start, end)
        except Exception as e:  # keep old data for this room
            log.error("[%d/%d] %s: fetch failed: %s", i, len(rooms), rid, e)
            stats["failed"] += 1
            continue
        if isinstance(allocs, list):
            problems = [validate(a) for a in allocs]
        else:
            allocs, problems = [allocs], [f"response is {type(allocs).__name__}, not a list"]
        bad = [(a, why) for a, why in zip(allocs, problems) if why]
        rows = [n for a, why in zip(allocs, problems) if not why and (n := normalize(a)) is not None]
        n_raw = len(allocs)
        if bad:
            stats["malformed"] += 1
            prev = conn.execute("SELECT n_entries FROM rooms WHERE id=?", (rid,)).fetchone()
            if prev and prev[0]:
                # Like a failed fetch: keep the last good data rather than a partial picture.
                log.warning("[%d/%d] %s: %d malformed entries, keeping previous data; first: %s (%r)",
                            i, len(rooms), rid, len(bad), bad[0][1], bad[0][0])
                stats["ok"] += 1
                continue
            # No good data yet: n_entries=0 shows the room as unknown, never as free.
            # The valid entries are still stored for the timeline.
            log.warning("[%d/%d] %s: %d malformed entries, marking unknown; first: %s (%r)",
                        i, len(rooms), rid, len(bad), bad[0][1], bad[0][0])
            n_raw = 0
        elif not allocs:
            # Every normal room has at least the nightly closure entries.
            log.warning("[%d/%d] %s: empty response (wrong id or unmanaged room?)", i, len(rooms), rid)
            stats["empty"] += 1
        _save_room(conn, r, rid, rows, n_raw, start, end)
        stats["ok"] += 1
        stats["intervals"] += len(rows)
        log.debug("[%d/%d] %s: %d raw, %d kept", i, len(rooms), rid, len(allocs), len(rows))

    store.set_meta(conn, store.window_key(UNI), f"{start.isoformat()}/{end.isoformat()}")
    conn.commit()
    log.info("fetch done: %s", stats)
    return stats


def _save_room(conn, r: dict, rid: str, rows, n_raw: int, start: date, end: date) -> None:
    conn.execute(
        """INSERT INTO rooms(id, building, floor, room, type, typecode, seats, workplaces, site, uni, campus,
                              fetched_at, n_entries)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET building=excluded.building, floor=excluded.floor,
             room=excluded.room, type=excluded.type, typecode=excluded.typecode, seats=excluded.seats,
             workplaces=excluded.workplaces,
             site=excluded.site, uni=excluded.uni, campus=excluded.campus, fetched_at=excluded.fetched_at, n_entries=excluded.n_entries""",
        (rid, r["building"], r["floor"], r["room"], r.get("type"), r.get("typecode"), r.get("seats"),
         r.get("workplaces"), r.get("locationName"), UNI, canonical(r.get("locationName")), datetime.now(timezone.utc).isoformat(timespec="seconds"), n_raw),
    )
    # Replace everything for this room (also drops past/stale entries).
    conn.execute("DELETE FROM intervals WHERE room_id=?", (rid,))
    conn.executemany(
        "INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES (?,?,?,?,?,?)",
        [(rid, *row) for row in rows],
    )
    conn.commit()


UNIS = ("ETH", "UZH")
SOURCES = {"ETH": SOURCE, "UZH": "uzh_rooms"}   # source_health key per university (uzh.SOURCE)


def parse_unis(value: str | None) -> tuple[str, ...]:
    """'ETH,UZH' -> ('ETH', 'UZH'); None/empty -> both. Unknown names are an error."""
    unis = tuple(dict.fromkeys(u.strip().upper() for u in (value or ",".join(UNIS)).split(",") if u.strip()))
    bad = [u for u in unis if u not in UNIS]
    if bad or not unis:
        raise ValueError(f"unknown universities {bad}, choose from {', '.join(UNIS)}")
    return unis


def run_uni(conn: sqlite3.Connection, uni: str, eth: dict | None = None, uzh: dict | None = None) -> dict:
    """Fetch one university's rooms; `eth`/`uzh` are the keyword arguments of fetch.run / uzh.run."""
    if uni == "ETH":
        return run(conn, **(eth or {}))
    from . import uzh as uzh_mod   # uzh imports nothing from here, but keep fetch light to import
    return uzh_mod.run(conn, **(uzh or {}))


def run_all(conn: sqlite3.Connection, unis: tuple[str, ...] = UNIS, eth: dict | None = None,
            uzh: dict | None = None) -> dict[str, dict | None]:
    """Fetch every university in `unis`. One failing never stops the others; each records its
    own health. Returns {uni: stats, or None if it failed}."""
    out: dict[str, dict | None] = {}
    for uni in unis:
        try:
            out[uni] = run_uni(conn, uni, eth, uzh)
        except (OSError, ValueError) as e:   # network trouble, HTTP errors, bad answers: no traceback needed
            log.error("%s room fetch failed, keeping the previous data: %s: %s", uni, type(e).__name__, e)
            out[uni] = None
        except Exception:
            log.exception("%s room fetch failed, keeping the previous data", uni)
            out[uni] = None
    return out
