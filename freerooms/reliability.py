"""Unreliable rooms (Phase 4, issue #20): rooms that often get booked at short notice.

Each room fetch replaces the room's intervals. Before that happens, `record_fetch` compares the
old busy intervals with the new ones and writes what changed to `booking_changes`. A booking that
appears less than SHORT_NOTICE before it starts, and wasn't there in the previous fetch, is a
short-notice booking. `scores` counts them per room over the last SCORE_WEEKS weeks and flags rooms
with many of them; the API passes the flag on and the UI keeps flagged rooms off the door-sign plate.

Avoiding false positives (a flagged room is shown with a warning, so being wrong is costly):
- The first fetch of a room, and fetches after or into an *unknown* state (empty response,
  malformed data: n_entries 0), are never compared: there is nothing trustworthy to compare with.
- Failed fetches keep the old data and never reach `record_fetch`.
- Only the overlap of the two fetch windows is compared, minus the last day of each (we don't
  rely on whether a source's end date is inclusive). New days entering the window as it moves
  forward, and past days leaving it, are not changes.
- Bookings are matched on (start, end) of busy intervals only: a new title or type is not a
  change. A time that was there before as closed/cancelled ("restored"), a booking overlapping
  one that disappeared in the same fetch ("moved"), and a booking that comes back after it
  disappeared earlier ("restored": a source hiccup that dropped bookings and then recovered)
  are recorded, but never count.
- The lead time is measured from the *previous* fetch (`max_lead_s`): the booking was made at
  some point after it, so its real lead time is at most that. Only a booking that is short notice
  even in that worst case counts, so long gaps between fetches can't make old bookings look new.
  Bookings that had already ended when the previous fetch looked are ignored (late paperwork).

The score is built from signals per room; user feedback ("often locked", Phase 5, issue #21) can
be added as another signal in `assess` without touching the diff.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import store
from .availability import TZ

SHORT_NOTICE = timedelta(hours=24)   # appeared less than this before it starts
SCORE_WEEKS = 4                      # look back this far
FLAG_PER_WEEK = 1.0                  # flag at this many short-notice bookings per week on average ...
FLAG_MIN = 3                         # ... and at least this many in the look-back window

COUNTED = "added"    # the only change kind that counts; others: 'removed', 'moved', 'restored'


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _local(t: datetime) -> datetime:
    """Aware instant -> naive Zurich wall-clock time, like the interval times."""
    return t.astimezone(TZ).replace(tzinfo=None)


def _seconds_until(start: datetime, t: datetime) -> int:
    """Seconds from instant t to the naive Zurich wall-clock time start."""
    return int((start.replace(tzinfo=TZ) - t).total_seconds())


def compare_window(old: tuple[date, date], new: tuple[date, date]) -> tuple[datetime, datetime]:
    """Range [lo, hi) of start times compared between two fetches. Windows are (first, last) days,
    both inclusive; the last day of each is left out, in case a source treats it as exclusive."""
    lo = max(old[0], new[0])
    hi = min(old[1], new[1])
    return datetime(lo.year, lo.month, lo.day), datetime(hi.year, hi.month, hi.day)


def diff(old_rows, new_rows, lo: datetime, hi: datetime, prev_fetch: datetime) -> list[tuple[str, tuple]]:
    """Changes between two fetches of one room, as [(change, row)], rows (start, end, kind, title, raw_type).

    Only busy intervals starting in [lo, hi) that hadn't ended by `prev_fetch` (naive Zurich time)
    are compared. change: 'added', 'removed', 'moved' (overlaps a removed one) or 'restored' (the same
    time was there before as closed/cancelled)."""
    def scope(rows, kinds=("busy",)):
        out = {}
        for row in rows:
            s, e = _dt(row[0]), _dt(row[1])
            if row[2] in kinds and lo <= s < hi and e > prev_fetch:
                out.setdefault((s, e), row)
        return out

    old_busy, new_busy = scope(old_rows), scope(new_rows)
    old_other = scope(old_rows, ("closed", "cancelled"))
    removed = [k for k in old_busy if k not in new_busy]
    out = []
    for k, row in sorted(new_busy.items()):
        if k in old_busy:
            continue
        if k in old_other:
            change = "restored"
        elif any(s < k[1] and k[0] < e for s, e in removed):
            change = "moved"
        else:
            change = "added"
        out.append((change, row))
    out += [("removed", old_busy[k]) for k in sorted(removed)]
    return out


def record_fetch(conn: sqlite3.Connection, uni: str, rid: str, rows, n_raw: int,
                 window: tuple[date, date], now: datetime | None = None) -> list[tuple[str, tuple]]:
    """Call before a fetch replaces room `rid`'s intervals with `rows` (n_raw raw entries, covering the
    days `window`, both inclusive). Records the changes against the stored data; returns them."""
    now = now or datetime.now(timezone.utc)
    if not n_raw:                     # empty or malformed answer: the room is unknown, not empty
        return []
    room = conn.execute("SELECT n_entries, fetched_at FROM rooms WHERE id=?", (rid,)).fetchone()
    if not room or not room["n_entries"] or not room["fetched_at"]:
        return []                     # first fetch of this room, or nothing trustworthy before
    win = store.get_meta(conn, store.window_key(uni))
    if not win:
        return []
    prev = datetime.fromisoformat(room["fetched_at"])
    w_lo, w_hi = (date.fromisoformat(x) for x in win.split("/"))
    # The room may have kept older data when its last fetch failed: its window then started earlier.
    own_lo = _local(prev).date()
    old = (max(w_lo, own_lo), min(w_hi, own_lo + (w_hi - w_lo)))
    lo, hi = compare_window(old, window)
    old_rows = conn.execute("SELECT start, end, kind, title, raw_type FROM intervals WHERE room_id=?", (rid,)).fetchall()
    changes = diff([tuple(r) for r in old_rows], rows, lo, hi, _local(prev)) if lo < hi else []

    stamp, prev_s = now.isoformat(timespec="seconds"), prev.isoformat(timespec="seconds")
    for i, (change, row) in enumerate(changes):
        if change == "added" and conn.execute(
                "SELECT 1 FROM booking_changes WHERE room_id=? AND start=? AND end=? AND change='removed'",
                (rid, row[0], row[1])).fetchone():
            changes[i] = change, row = "restored", row   # it was there before and briefly went missing
        s = _dt(row[0])
        conn.execute(
            """INSERT INTO booking_changes(room_id, detected_at, prev_fetch, start, end, change, lead_s, max_lead_s, title)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, stamp, prev_s, row[0], row[1], change, _seconds_until(s, now), _seconds_until(s, prev), row[3]))
    conn.execute("UPDATE rooms SET watch_since=COALESCE(watch_since, ?) WHERE id=?", (prev_s, rid))
    return changes


def assess(short_notice: int, weeks: float) -> tuple[bool, str | None]:
    """(unreliable, reason) from a room's signals. Phase 5 feedback ("often locked") goes here too."""
    rate = short_notice / weeks
    if short_notice >= FLAG_MIN and rate >= FLAG_PER_WEEK:
        span = "week" if weeks <= 1 else f"{round(weeks)} weeks"
        return True, f"{short_notice} short-notice bookings in the last {span}"
    return False, None


def scores(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict]:
    """Per room with any short-notice booking in the last SCORE_WEEKS weeks:
    {"short_notice": n, "per_week": rate, "unreliable": bool, "reason": str | None}."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(weeks=SCORE_WEEKS)
    watched = {r["id"]: r["watch_since"] for r in conn.execute("SELECT id, watch_since FROM rooms")}
    out = {}
    for rid, n in conn.execute(
            """SELECT room_id, COUNT(*) FROM booking_changes WHERE change=? AND max_lead_s < ? AND detected_at >= ?
               GROUP BY room_id""",
            (COUNTED, int(SHORT_NOTICE.total_seconds()), since.isoformat(timespec="seconds"))):
        start = watched.get(rid)
        # Rooms watched for less than the look-back window are judged on the time they were watched
        # (at least a week, so a few days of history can't produce a high rate).
        observed = (now - datetime.fromisoformat(start)) / timedelta(weeks=1) if start else SCORE_WEEKS
        weeks = min(SCORE_WEEKS, max(1.0, observed))
        flag, reason = assess(n, weeks)
        out[rid] = {"short_notice": n, "per_week": round(n / weeks, 2), "unreliable": flag, "reason": reason}
    return out


def annotate(rooms: list[dict], by_room: dict[str, dict]) -> list[dict]:
    """Add `unreliable` (bool) and `unreliable_reason` to each room dict."""
    for r in rooms:
        s = by_room.get(r["id"]) or {}
        r["unreliable"] = bool(s.get("unreliable"))
        r["unreliable_reason"] = s.get("reason")
    return rooms
