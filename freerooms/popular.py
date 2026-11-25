"""Popular times: how full a study place usually is on a weekday, per time slot.

From the stored occupancy samples of the same Zurich weekday in the last WEEKS weeks: each week
gives one value per slot (the median of its samples in that slot), and the usual value is the
median of those weekly values, so a week with more samples doesn't count more. Closed samples and
samples without a number are skipped. A slot is only reported when at least MIN_WEEKS weeks have
data in it; with less history the UI says so instead of guessing.

Past samples never change, so the results only depend on the day (or slot) asked for.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from .availability import TZ
from .places import YELLOW_BELOW, utc_iso

SLOT_MIN = 30          # minutes per slot
WEEKS = 6              # how far back to look
MIN_WEEKS = 3          # weeks with data a slot needs before it's shown
FULL_PCT = YELLOW_BELOW  # "fills up": the usual share taken reaches red (90 %)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _local_start(day: date, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(minutes=minute)


def past_days(day: date, now: datetime, weeks: int = WEEKS) -> list[date]:
    """The same weekday in each of the `weeks` weeks before `day`, if it had started by `now`."""
    return [d for d in (day - timedelta(weeks=k) for k in range(1, weeks + 1)) if _local_start(d) < now]


def _collect(conn: sqlite3.Connection, place_ids: list[str], ranges: list[tuple[date, datetime, datetime]],
             slot_min: int) -> dict[str, dict[int, dict[date, list[float]]]]:
    """Numeric, open samples per place -> slot -> day -> values. One indexed query per range."""
    out: dict[str, dict[int, dict[date, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    if not place_ids:
        return out
    marks = ",".join("?" * len(place_ids))
    for day, start, end in ranges:
        rows = conn.execute(
            f"""SELECT place_id, ts, pct FROM occupancy
                WHERE place_id IN ({marks}) AND ts >= ? AND ts < ? AND pct IS NOT NULL AND closed = 0""",
            (*place_ids, utc_iso(start), utc_iso(end)))
        for pid, ts, pct in rows:
            local = datetime.fromisoformat(ts).astimezone(TZ)
            if local.date() != day:     # can't happen for whole-day ranges; cheap guard anyway
                continue
            out[pid][(local.hour * 60 + local.minute) // slot_min][day].append(pct)
    return out


def _usual(by_day: dict[date, list[float]]) -> tuple[float, int]:
    """(median over weeks of each week's median, number of weeks)."""
    return round(median(median(v) for v in by_day.values()), 1), len(by_day)


def usual_day(conn: sqlite3.Connection, place_ids: list[str], day: date, now: datetime | None = None,
              weeks: int = WEEKS, slot_min: int = SLOT_MIN, min_weeks: int = MIN_WEEKS) -> dict[str, dict]:
    """The usual curve of `day`'s weekday for each place:

    {"weekday": "Wednesday", "slot_min": 30, "window_weeks": 6, "min_weeks": 3,
     "weeks": 4,                       # past same weekdays with any number for this place
     "slots": [{"start": "08:00", "pct": 41.5, "weeks": 4}, ...],   # only slots with >= min_weeks
     "fills_by": "10:30" | None}       # first slot whose usual value is >= FULL_PCT
    """
    now = now or datetime.now(timezone.utc)
    days = past_days(day, now, weeks)
    ranges = [(d, _local_start(d), _local_start(d + timedelta(days=1))) for d in days]  # 23/24/25 h
    data = _collect(conn, place_ids, ranges, slot_min)
    out = {}
    for pid in place_ids:
        slots, seen = [], set()
        for slot, by_day in sorted(data.get(pid, {}).items()):
            seen.update(by_day)
            if len(by_day) >= min_weeks:
                pct, n = _usual(by_day)
                slots.append({"start": f"{slot * slot_min // 60:02d}:{slot * slot_min % 60:02d}", "pct": pct, "weeks": n})
        out[pid] = {
            "weekday": WEEKDAYS[day.weekday()], "slot_min": slot_min, "window_weeks": weeks, "min_weeks": min_weeks,
            "weeks": len(seen), "slots": slots,
            "fills_by": next((s["start"] for s in slots if s["pct"] >= FULL_PCT), None),
        }
    return out


def usual_now(conn: sqlite3.Connection, place_ids: list[str], now: datetime | None = None,
              weeks: int = WEEKS, slot_min: int = SLOT_MIN, min_weeks: int = MIN_WEEKS) -> dict[str, dict]:
    """The usual value of the current slot per place: {id: {"pct", "weeks", "slot"}}; places with
    too little history are left out. Reads only that slot of each past week."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    slot = (local.hour * 60 + local.minute) // slot_min
    ranges = []
    for d in past_days(local.date(), now, weeks):
        start = _local_start(d, slot * slot_min)
        ranges.append((d, start, start + timedelta(minutes=slot_min)))
    data = _collect(conn, place_ids, ranges, slot_min)
    out = {}
    for pid, by_slot in data.items():
        by_day = by_slot.get(slot)
        if by_day and len(by_day) >= min_weeks:
            pct, n = _usual(by_day)
            out[pid] = {"pct": pct, "weeks": n, "slot": f"{slot * slot_min // 60:02d}:{slot * slot_min % 60:02d}"}
    return out


def slot_key(now: datetime | None = None, slot_min: int = SLOT_MIN) -> tuple[str, int]:
    """(Zurich date, slot index): usual_now's result is the same for every `now` with this key."""
    local = (now or datetime.now(timezone.utc)).astimezone(TZ)
    return local.date().isoformat(), (local.hour * 60 + local.minute) // slot_min
