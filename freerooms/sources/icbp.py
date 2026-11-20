"""ICBP Infozentrum (chemistry | biology | pharmacy library, HCI G 5.1, Hönggerberg): live
occupancy for the whole library and per floor.

The feed is the one https://infoz01.ethz.ch polls every minute (its summary.js / detail.js):
  {"library_is_closed": true, "overall_max": 170, "overall": 0, "no_data": false,
   "ts": "2026-11-19T21:34:29+01:00", "trend": 0, "G": 0, "G_max": 68, "H": 0, "H_max": 68, "J": 0, "J_max": 34}

- `overall`, `<F>`: estimated people (fractional; from Wi-Fi plus an entrance counter) in the
  library and on floor F of the library (G, H, J are HCI floors, the library spans all three).
  `overall_max`, `<F>_max`: seats. The site shows round(100 * overall / overall_max) %.
- `trend`: 1 = filling up, -1 = emptying, 0 = steady (the site rotates an arrow on exactly 1 / -1).
- `library_is_closed`: the site then shows "closed" and treats every floor as 0.
- `no_data`: not used by the site; we read it as "the counts are not valid".
- `ts`: time of the measurement, local time with offset.
"""

from __future__ import annotations

from datetime import datetime

from ..live import Source
from ..places import Place, Sample, status_for, utc_iso

KEY = "icbp"
URL = "https://iz-websrv01.ethz.ch:3000/api/visitors"
SITE = "https://infoz01.ethz.ch/"
# The library is also an ETH study space (studyspaces.json, HCIG5.1). We use that place (name,
# hours, features) and attach our library-wide samples and the floor sub-places to it; ETH's
# own sensor for it has no data, so eth_availability leaves this place to us.
LIBRARY_ID = "eth_studyspaces:HCIG5.1"
NAME = "ICBP Infozentrum"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def floors(raw: dict) -> list[str]:
    """Floor keys: every key X with a numeric X_max partner, except the overall total."""
    return sorted(k[:-4] for k, v in raw.items()
                  if k.endswith("_max") and k != "overall_max" and _num(v) is not None)




def _trend(v) -> int | None:
    n = _num(v)
    return None if n is None else (n > 0) - (n < 0)


def _ts(raw: dict, now: datetime | None) -> str:
    try:
        dt = datetime.fromisoformat(raw["ts"])
        if dt.tzinfo is not None:
            return utc_iso(dt)
    except (KeyError, TypeError, ValueError):
        pass
    return utc_iso(now)


def _check(raw) -> dict:
    if not isinstance(raw, dict) or _num(raw.get("overall_max")) is None:
        raise ValueError(f"unexpected ICBP response: {str(raw)[:200]}")
    return raw


def parse_places(raw) -> list[Place]:
    """The floors only; the library itself comes from eth_studyspaces."""
    raw = _check(raw)
    return [Place(id=f"{KEY}:{f}", uni="ETH", name=f"{NAME} – floor {f}", source=KEY,
                          parent_id=LIBRARY_ID, campus="Zürich Hönggerberg", building="HCI",
                          seats=int(raw[f + "_max"]), url=SITE) for f in floors(raw)]


def _sample(place_id: str, ts: str, count, mx, trend: int | None, closed: bool, no_data: bool) -> Sample:
    mx_n = _num(mx)
    count = None if no_data else _num(count)
    pct = None
    if count is not None and mx_n:
        pct = round(min(100.0, max(0.0, 100.0 * count / mx_n)), 1)
    return Sample(place_id, ts, status=status_for(pct, closed), pct=pct, count=count,
                  max=None if mx_n is None else int(mx_n), trend=None if no_data else trend, closed=closed)


def parse_samples(raw, now: datetime | None = None) -> list[Sample]:
    """One sample for the library and one per floor. `now` is only used when `ts` is missing."""
    raw = _check(raw)
    ts, closed, no_data = _ts(raw, now), bool(raw.get("library_is_closed")), bool(raw.get("no_data"))
    fl = floors(raw)
    overall = raw.get("overall")
    if _num(overall) is None and fl:   # like the site: fall back to the sum of the floors
        parts = [_num(raw.get(f)) for f in fl]
        overall = None if None in parts else sum(parts)
    trend = _trend(raw.get("trend"))
    out = [_sample(LIBRARY_ID, ts, overall, raw["overall_max"], trend, closed, no_data)]
    # The feed has one trend for the whole library; floors get none rather than a guess.
    out += [_sample(f"{KEY}:{f}", ts, raw.get(f), raw[f + "_max"], None, closed, no_data) for f in fl]
    return out


def fetch(http):
    raw = http.get_json(URL)
    return parse_places(raw), parse_samples(raw)


# order 1: after eth_studyspaces, which creates the library place our samples point to.
SOURCES = [Source(key=KEY, every=300, max_age=900, fetch=fetch, order=1)]
