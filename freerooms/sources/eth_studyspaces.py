"""ETH study spaces (metadata, hourly) and their live availability (every 5 min).

Both feeds are public and power ETH's own study-space finder (the Vue app in
/etc.clientlibs/ethz/design/clientlibs/default.js); they are joined on `roomGlobalIdentifier`
(e.g. 'HGH30.1'), which becomes the place id 'eth_studyspaces:HGH30.1' for both sources.

`eth_studyspaces` -> Places (no samples), from studyspaces.json. `Place.hours` has this shape:

    {
      "lines":  ["Mon - Fri: 06.00 - 21.00", "Sat - Sun: Closed",
                 "Meal times: Mon - Fri: 11.15 - 14.00 and 17.30 - 19.30",
                 "Learning environment outside meal times"],      # raw, as ETH shows them (English)
      "week":   {"mon": [["06:00", "21:00"]], ..., "sat": [], "sun": []},
                # opening intervals per weekday; [] = closed, missing day = not stated
      "meals":  {"mon": [["11:15", "14:00"], ["17:30", "19:30"]], ...},
                # mensa meal times: no study seats then; {} when there are none
      "access_24h": false,           # 24-hour access with an ETH card (outside "week")
      "state": "open",               # ETH's stateLabel: 'open' | 'closed' | 'mealTime'
      "is_open": true,               # ETH's isOpen; ETH's "open now" filter is
                                     # is_open and state != 'mealTime'
      "next_change": "2026-11-19T23:00:00+00:00",  # nextMealChangeAt (UTC ISO): when ETH
                                     # recomputes state; observed as next midnight local time
      "next_open_date": "2026-11-20" | null,       # only when closed now
      "notice": {"en": "...", "de": "..."} | null, # temporary notice, e.g. a closure day
      "sensor": true | null,         # occupancy sensor active (ETH's occupancy.activeFlag)
      "as_of": "2026-11-19T20:43:00+00:00"         # fetch time: state/is_open are a snapshot
    }

`state`/`is_open` are at most an hour old (`as_of`); for "open now" prefer computing from `week`
and `meals` with the current Zurich time. Lines that don't parse stay in `lines` only.

`Place.features` is ETH's [{key, label}] (e.g. 'um.ruhig' Quiet environment, 'food.mensa'),
plus {"key": "verfueg.24h", ...} when has24HourAccess (the same pseudo key ETH's filter uses).
`seats` 0 in the feed means unknown (ETH hides it) and is stored as None. Campus codes:
ZZ = Zürich Zentrum, ZH = Zürich Hönggerberg, AC = Basel (as labelled in ETH's app).

`eth_availability` -> Samples (no places), from studyspaces/availability.json. Per entry:
status green|yellow|red|unknown, and for sensor rooms `occupancy` (people), `maxPeople`,
`occupancyPercentage` (0-100, ETH rounds it to steps of 20), `trend` steady|filling|emptying,
`timestamp` (ISO 8601 with offset and microseconds). As in ETH's app, an entry without an
`occupancy` count has no data: it becomes status 'unknown' with pct/count/trend None.
pct is computed from occupancy/maxPeople (more precise than ETH's 20 % steps, capped at 100),
falling back to occupancyPercentage. ts is the sensor timestamp when there is a count, else the
fetch time rounded to the minute. The feed has no open/closed info, so Sample.closed is always
False; use the place's hours. The feed also lists rooms that are not in studyspaces.json; their
samples are dropped by places.add_samples (with a warning).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..live import Source
from ..places import Place, Sample, utc_iso

BASE = "https://ethz.ch"
STUDYSPACES_URL = BASE + "/bin/ethz/studyspaces.json"
AVAILABILITY_URL = BASE + "/bin/ethz/studyspaces/availability.json"
PREFIX = "eth_studyspaces:"
CAMPUS = {"ZZ": "Zürich Zentrum", "ZH": "Zürich Hönggerberg", "AC": "Basel"}
STATUSES = {"green", "yellow", "red", "unknown"}
TRENDS = {"filling": 1, "steady": 0, "emptying": -1}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FEATURE_24H = {"key": "verfueg.24h", "label": "24-hour access via ETH card"}

_DAY = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_DAYS_RE = re.compile(rf"^{_DAY}(?:\s*-\s*{_DAY})?$")
_SPAN_RE = re.compile(r"^(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})$")


def _days(spec: str) -> list[str] | None:
    m = _DAYS_RE.match(spec.strip())
    if not m:
        return None
    a = DAYS.index(m.group(1).lower())
    b = DAYS.index(m.group(2).lower()) if m.group(2) else a
    return DAYS[a:b + 1] if a <= b else DAYS[a:] + DAYS[:b + 1]


def _spans(spec: str) -> list[list[str]] | None:
    """'07.00 - 19.00 and 17.30 - 19.30' -> [['07:00', '19:00'], ...]; 'Closed' -> []."""
    if spec.strip().lower() == "closed":
        return []
    out = []
    for part in re.split(r"\s+and\s+|\s*,\s*", spec.strip()):
        m = _SPAN_RE.match(part)
        if not m:
            return None
        h1, m1, h2, m2 = m.groups()
        out.append([f"{int(h1):02d}:{m1}", f"{int(h2):02d}:{m2}"])
    return out


def parse_hours(lines: list[str]) -> tuple[dict, dict]:
    """ETH's hour lines -> (week, meals), both {day: [[from, to], ...]}."""
    week: dict[str, list] = {}
    meals: dict[str, list] = {}
    for line in lines or []:
        target, text = week, line
        m = re.match(r"^Meal times?:\s*(.*)$", line, re.I)
        if m:
            target, text = meals, m.group(1)
        days_spec, sep, spec = text.partition(": ")
        days, spans = (_days(days_spec), _spans(spec)) if sep else (None, None)
        if days is None or spans is None:
            continue  # free text ('24-hour access via ETH card', ...), kept in "lines"
        for d in days:
            target.setdefault(d, []).extend(spans)
    return week, meals


def _ms_to_iso(ms) -> str | None:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    return utc_iso(datetime.fromtimestamp(ms / 1000, timezone.utc))


def _name(title: str, description: str) -> str:
    if not description:
        return title
    if not title or title.replace(" ", "") in description.replace(" ", ""):
        return description
    return f"{description} ({title})"


def parse_places(raw: dict, now: datetime | None = None) -> list[Place]:
    as_of = utc_iso((now or datetime.now(timezone.utc)).replace(second=0, microsecond=0))
    out = []
    for e in raw.get("data") or []:
        a = e.get("attributes") or {}
        rid = a.get("roomGlobalIdentifier")
        if not rid:
            continue
        lines = [str(x) for x in a.get("hours") or []]
        week, meals = parse_hours(lines)
        meta = a.get("occupancy") if isinstance(a.get("occupancy"), dict) else {}
        notice = {k: meta[k] for k in ("en", "de") if meta.get(k)} or None
        features = [{"key": f.get("key"), "label": f.get("label")}
                    for f in a.get("features") or [] if isinstance(f, dict) and f.get("key")]
        access_24h = a.get("has24HourAccess") is True
        if access_24h and not any(f["key"] == FEATURE_24H["key"] for f in features):
            features.append(dict(FEATURE_24H))
        url = a.get("locationPageUrl")
        if url and url.startswith("/"):
            url = BASE + url
        seats = a.get("seats")
        out.append(Place(
            id=PREFIX + rid, uni="ETH", source="eth_studyspaces",
            name=_name(a.get("title") or rid, (a.get("description") or "").strip()),
            campus=CAMPUS.get(a.get("campus"), a.get("campus")),
            building=a.get("building"),
            seats=seats if isinstance(seats, int) and seats > 0 else None,
            hours={
                "lines": lines, "week": week, "meals": meals, "access_24h": access_24h,
                "state": a.get("stateLabel"), "is_open": a.get("isOpen"),
                "next_change": _ms_to_iso(a.get("nextMealChangeAt")),
                "next_open_date": a.get("nextOpenDate"),
                "notice": notice, "sensor": meta.get("activeFlag"), "as_of": as_of,
            },
            features=features, url=url))
    return out


def _ts(value) -> str | None:
    """'2026-11-19T21:40:20.28724+01:00' -> '2026-11-19T20:40:20+00:00' (None if unusable).
    The fraction is dropped first: Python 3.10's fromisoformat wants exactly 3 or 6 digits."""
    if not isinstance(value, str):
        return None
    s = re.sub(r"(T\d{2}:\d{2}:\d{2})\.\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo("Europe/Zurich"))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return utc_iso(dt)


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_samples(raw: dict, now: datetime | None = None) -> list[Sample]:
    fetched = utc_iso((now or datetime.now(timezone.utc)).replace(second=0, microsecond=0))
    out = []
    for e in raw.get("data") or []:
        a = e.get("attributes") or {}
        rid = a.get("roomGlobalIdentifier") or e.get("id")
        if not rid:
            continue
        count = _num(a.get("occupancy"))
        if count is None:  # no data (ETH's app shows the same: status unknown)
            out.append(Sample(PREFIX + rid, fetched, status="unknown"))
            continue
        mx = _num(a.get("maxPeople"))
        mx = int(mx) if mx else None
        if mx:
            pct = min(100.0, round(100 * count / mx, 1))
        else:
            pct = _num(a.get("occupancyPercentage"))
        status = a.get("status") if a.get("status") in STATUSES else "unknown"
        out.append(Sample(PREFIX + rid, _ts(a.get("timestamp")) or fetched, status=status,
                          pct=pct, count=count, max=mx, trend=TRENDS.get(a.get("trend"))))
    return out


def fetch_places(http) -> tuple[list[Place], list[Sample]]:
    return parse_places(http.get_json(STUDYSPACES_URL)), []


def fetch_samples(http) -> tuple[list[Place], list[Sample]]:
    return [], parse_samples(http.get_json(AVAILABILITY_URL))


SOURCES = [
    Source(key="eth_studyspaces", every=3600, max_age=3 * 3600, fetch=fetch_places, order=0),
    Source(key="eth_availability", every=300, max_age=900, fetch=fetch_samples, order=1),
]
