"""Unreliable rooms (Phase 4, issue #20): rooms that often get booked at short notice.

Each room fetch replaces the room's intervals. Before that happens, `record_fetch` compares the
old busy and closed intervals with the new ones and writes what changed to `booking_changes`. A booking that
appears less than SHORT_NOTICE before it starts, and wasn't there in the previous fetch, is a
short-notice booking. `scores` counts them per room over the last SCORE_WEEKS weeks and flags rooms
with many of them; the API passes the flag on, the UI notes it (and, with "Prefer reliable rooms" on,
keeps flagged rooms off the door-sign plate) and the `now` CLI marks them.

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

Short-notice closures count too (e.g. "Hauptgebäude ab 13:30 geschlossen"): a `closed` interval that
newly closes at least MIN_CLOSURE of time the previous fetch showed as open, less than SHORT_NOTICE
before that time, under the same guards (compare window, unknown states, lead from the previous fetch,
already ended, a closure that comes back after a hiccup). Shifting or splitting closures that were
already there (UZH cuts them at midnight) adds no closed time and doesn't count. A closure usually
hits every room of a building at once, so it is weighed less than a booking (CLOSURE_WEIGHT) and all
closures one fetch found for a room are one event: a building has to close at short notice often
(8 times in 4 weeks, with no bookings) before its rooms are flagged, and then they all are, rightly.

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
CLOSURE_WEIGHT = 0.5                 # a short-notice closure counts as half a short-notice booking
MIN_CLOSURE = timedelta(minutes=30)  # a closure must newly close at least this much time to count

COUNTED = "added"    # the only booking change that counts; others: 'removed', 'moved', 'restored'
CLOSURE = "closure"  # the only closure change that counts; others: 'closure_removed', 'closure_restored',
                     # 'closure_shifted' (newly closes less than MIN_CLOSURE)


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

    Only busy and closed intervals starting in [lo, hi) that hadn't ended by `prev_fetch` (naive Zurich
    time) are compared. Bookings: 'added', 'removed', 'moved' (overlaps a removed one) or 'restored' (the
    same time was there before as closed/cancelled). Closures: 'closure' (newly closes at least
    MIN_CLOSURE; returned as (change, row, first newly closed instant)), 'closure_shifted' (less) or
    'closure_removed'."""
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

    # Closures: what counts is the time a new closure closes that no old closure did.
    old_closed = scope(old_rows, ("closed",))
    new_closed = scope(new_rows, ("closed",))
    covering = [(_dt(r[0]), _dt(r[1])) for r in old_rows if r[2] == "closed"]
    for k, row in sorted(new_closed.items()):
        if k in old_closed:
            continue
        first, total = _uncovered(max(k[0], prev_fetch), min(k[1], hi), covering)
        if total >= MIN_CLOSURE:
            out.append(("closure", row, first))
        else:
            out.append(("closure_shifted", row))
    out += [("closure_removed", old_closed[k]) for k in sorted(old_closed) if k not in new_closed]
    return out


def _uncovered(s: datetime, e: datetime, covering) -> tuple[datetime | None, timedelta]:
    """(first uncovered instant, total uncovered time) of [s, e) minus the intervals `covering`.
    A gap of up to a minute between covering intervals counts as covered (UZH's 23:59 ends)."""
    first, total, t = None, timedelta(0), s
    for cs, ce in sorted(covering):
        if ce <= t or cs >= e:
            continue
        if cs - t > timedelta(minutes=1):
            first = first or t
            total += cs - t
        t = max(t, ce)
        if t >= e:
            break
    if e - t > timedelta(minutes=1):
        first = first or t
        total += e - t
    return first, total


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
    for i, (change, row, *first) in enumerate(changes):
        gone = {"added": ("removed", "restored"), "closure": ("closure_removed", "closure_restored")}.get(change)
        if gone and conn.execute(
                "SELECT 1 FROM booking_changes WHERE room_id=? AND start=? AND end=? AND change=?",
                (rid, row[0], row[1], gone[0])).fetchone():
            change = gone[1]                               # it was there before and briefly went missing
        changes[i] = change, row
        # A closure's lead time counts to the first time it newly closes (an evening closure brought
        # forward to 13:30: from 13:30). Its start and end stay the interval's, for 'closure_restored'.
        s = first[0] if change == "closure" else _dt(row[0])
        conn.execute(
            """INSERT INTO booking_changes(room_id, detected_at, prev_fetch, start, end, change, lead_s, max_lead_s, title)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, stamp, prev_s, row[0], row[1], change, _seconds_until(s, now), _seconds_until(s, prev), row[3]))
    conn.execute("UPDATE rooms SET watch_since=COALESCE(watch_since, ?) WHERE id=?", (prev_s, rid))
    return changes


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def weighted(short_notice: int, closures: int = 0) -> float:
    return short_notice + CLOSURE_WEIGHT * closures


def assess(short_notice: int, weeks: float, closures: int = 0) -> tuple[bool, str | None]:
    """(unreliable, reason) from a room's signals: short-notice bookings, and short-notice closure
    events weighted CLOSURE_WEIGHT. Phase 5 feedback ("often locked") goes here too."""
    score = weighted(short_notice, closures)
    if score >= FLAG_MIN and score / weeks >= FLAG_PER_WEEK:
        span = "week" if weeks <= 1 else f"{round(weeks)} weeks"
        if not short_notice:
            what = _n(closures, "short-notice closure")
        else:
            what = _n(short_notice, "short-notice booking") + (f", {_n(closures, 'closure')}" if closures else "")
        return True, f"{what} in the last {span}"
    return False, None


def scores(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict]:
    """Per room with any short-notice booking or closure in the last SCORE_WEEKS weeks:
    {"short_notice": n, "closures": m, "per_week": weighted rate, "unreliable": bool, "reason": str | None}.
    All closures one fetch found for a room are one closure (a building closed for a weekend is
    several intervals, but one announcement)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(weeks=SCORE_WEEKS)
    watched = {r["id"]: r["watch_since"] for r in conn.execute("SELECT id, watch_since FROM rooms")}
    out = {}
    for rid, n, m in conn.execute(
            """SELECT room_id, COUNT(CASE WHEN change=? THEN 1 END), COUNT(DISTINCT CASE WHEN change=? THEN detected_at END)
               FROM booking_changes WHERE change IN (?, ?) AND max_lead_s < ? AND detected_at >= ? GROUP BY room_id""",
            (COUNTED, CLOSURE, COUNTED, CLOSURE, int(SHORT_NOTICE.total_seconds()),
             since.isoformat(timespec="seconds"))).fetchall():
        start = watched.get(rid)
        # Rooms watched for less than the look-back window are judged on the time they were watched
        # (at least a week, so a few days of history can't produce a high rate).
        observed = (now - datetime.fromisoformat(start)) / timedelta(weeks=1) if start else SCORE_WEEKS
        weeks = min(SCORE_WEEKS, max(1.0, observed))
        flag, reason = assess(n, weeks, m)
        out[rid] = {"short_notice": n, "closures": m, "per_week": round(weighted(n, m) / weeks, 2),
                    "unreliable": flag, "reason": reason}
    return out


def annotate(rooms: list[dict], by_room: dict[str, dict]) -> list[dict]:
    """Add `unreliable` (bool) and `unreliable_reason` to each room dict."""
    for r in rooms:
        s = by_room.get(r["id"]) or {}
        r["unreliable"] = bool(s.get("unreliable"))
        r["unreliable_reason"] = s.get("reason")
    return rooms
