"""Pure availability logic: given a room's blocking intervals, is it free at time t?

All datetimes are naive Europe/Zurich local time (the API has no offsets).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")

# The API ends days at 23:59, the next day starts at 00:00 -> treat small gaps as contiguous.
MERGE_GAP = timedelta(minutes=1)


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


@dataclass
class Interval:
    start: datetime
    end: datetime
    kind: str            # 'busy' | 'closed'
    title: str | None = None


@dataclass
class Status:
    state: str                    # 'free' | 'busy' | 'closed' | 'unknown'
    until: datetime | None = None  # free: next blocking start; busy/closed: when it frees up
    reason: str | None = None      # title of the current/next event
    next_kind: str | None = None   # free: kind of the thing that ends the free slot

    def minutes(self, t: datetime) -> int | None:
        return None if self.until is None else int((self.until - t).total_seconds() // 60)


def room_status(intervals: list[Interval], t: datetime, has_data: bool,
                window_end: datetime | None = None) -> Status:
    if not has_data or (window_end is not None and t >= window_end):
        return Status("unknown")
    ivs = sorted(intervals, key=lambda i: i.start)

    current = [i for i in ivs if i.start <= t < i.end]
    if current:
        # Prefer 'busy' over 'closed' for display if both overlap.
        cur = next((i for i in current if i.kind == "busy"), current[0])
        end = max(i.end for i in current)
        # Extend through back-to-back / overlapping intervals.
        changed = True
        while changed:
            changed = False
            for i in ivs:
                if i.start <= end + MERGE_GAP and i.end > end:
                    end, changed = i.end, True
        return Status(cur.kind, until=end, reason=cur.title)

    upcoming = [i for i in ivs if i.start > t]
    if not upcoming:
        return Status("free", until=window_end)
    nxt = upcoming[0]
    return Status("free", until=nxt.start, reason=nxt.title, next_kind=nxt.kind)


def open_hours(closed: list[tuple[datetime, datetime]], d0: datetime) -> tuple[datetime, datetime] | str | None:
    """Opening hours on the day starting at d0, from that room's closure intervals.

    Returns (open, close), "closed" if closures cover the whole day, or None if
    there are no closure entries that day (hours unknown). If a building opens
    twice in one day, the span from the first opening to the last closing is used.
    """
    d1 = d0 + timedelta(days=1)
    ivs = sorted((max(s, d0), min(e, d1)) for s, e in closed if e > d0 and s < d1)
    if not ivs:
        return None
    gaps, cursor = [], d0
    for s, e in ivs:
        if s - cursor > MERGE_GAP:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if d1 - cursor > MERGE_GAP:
        gaps.append((cursor, d1))
    if not gaps:
        return "closed"
    return gaps[0][0], gaps[-1][1]


def building_hours(conn: sqlite3.Connection, t: datetime) -> dict[str, dict]:
    """Per building: opening hours on the day of t, as the most common hours among its rooms.

    {"HG": {"open": "07:00", "close": "22:00"}, "XY": {"closed": True}}; buildings
    without closure data are left out.
    """
    d0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    closed: dict[str, list] = {}
    for row in conn.execute(
            "SELECT room_id, start, end FROM intervals WHERE kind='closed' AND end > ? AND start < ?",
            (d0.isoformat(), (d0 + timedelta(days=1)).isoformat())):
        closed.setdefault(row["room_id"], []).append((_dt(row["start"]), _dt(row["end"])))
    votes: dict[str, Counter] = {}
    for r in conn.execute("SELECT id, building FROM rooms"):
        h = open_hours(closed.get(r["id"], []), d0)
        if h is not None:
            votes.setdefault(r["building"], Counter())[h] += 1
    out = {}
    for b, c in sorted(votes.items()):
        h = c.most_common(1)[0][0]
        if h == "closed":
            out[b] = {"closed": True}
        else:
            o, e = h
            out[b] = {"open": o.strftime("%H:%M"), "close": "24:00" if e - d0 >= timedelta(hours=23, minutes=59) else e.strftime("%H:%M")}
    return out


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def query(conn: sqlite3.Connection, t: datetime) -> list[dict]:
    """Status of every stored room at local time t, as JSON-ready dicts."""
    window = conn.execute("SELECT value FROM meta WHERE key='window'").fetchone()
    window_end = None
    if window:
        window_end = datetime.fromisoformat(window[0].split("/")[1]) + timedelta(days=1)

    by_room: dict[str, list[Interval]] = {}
    for row in conn.execute("SELECT room_id, start, end, kind, title FROM intervals WHERE end > ?",
                            (t.isoformat(),)):
        by_room.setdefault(row["room_id"], []).append(
            Interval(_dt(row["start"]), _dt(row["end"]), row["kind"], row["title"]))

    # All intervals of the day containing t, for the timeline in the UI.
    d0 = t.replace(hour=0, minute=0)
    timeline: dict[str, list[list]] = {}
    for row in conn.execute(
            "SELECT room_id, start, end, title, kind FROM intervals WHERE end > ? AND start < ? ORDER BY start",
            (d0.isoformat(), (d0 + timedelta(days=1)).isoformat())):
        timeline.setdefault(row["room_id"], []).append(
            [row["start"][11:16], row["end"][11:16], row["title"], row["kind"]])

    # Rooms without any closure entries: we don't know when the building is locked.
    hours_known = {row[0] for row in conn.execute("SELECT DISTINCT room_id FROM intervals WHERE kind='closed'")}

    out = []
    for r in conn.execute("SELECT * FROM rooms ORDER BY id"):
        st = room_status(by_room.get(r["id"], []), t, has_data=bool(r["n_entries"]), window_end=window_end)
        d = {k: r[k] for k in ("id", "building", "floor", "room", "type", "typecode", "seats", "workplaces", "site")}
        d.update(asdict(st))
        d["until"] = st.until.isoformat(timespec="minutes") if st.until else None
        d["minutes"] = st.minutes(t)
        d["hours_known"] = r["id"] in hours_known
        d["timeline"] = timeline.get(r["id"], [])
        out.append(d)
    return out
