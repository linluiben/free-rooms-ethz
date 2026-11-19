"""UZH library (UB) occupancy from the Occusense widgets embedded on
https://www.ub.uzh.ch/de/ub-besuchen/belegung-alle.html

Each widget is a tiny HTML page with a ProgressBar.js call whose label reads
`value: 'ca. 30% frei von 120'`, `value: '&gt; 95% frei von 120'` or `value: 'geschlossen'`.
The same page has other `value:` strings (e.g. `value: 'translate(-50%, -50%)'`), so we look
for the one that reads like an occupancy label. Numbers are rounded to 5 %.

Choices:
- pct (share OCCUPIED) = 100 - % free. "> 95% frei" becomes pct 3, matching the widget's own bar
  (`bar.animate(0.97)`); "< 5% frei" (not seen yet) is treated the same way, as pct 97.
- "geschlossen" -> closed=True, pct None, status None. The widget then has no seat count, so
  seats falls back to the number in the static table below.
- status: green below 70 % occupied, yellow below 90 %, red from 90 % (i.e. "ca. 10% frei").
- ts: fetch time floored to the minute (UTC). trend: None (the widget has none).
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone

from .. import live
from ..places import Place, Sample, utc_iso

log = logging.getLogger(__name__)

KEY = "uzh_ub"
WIDGET_URL = "https://hbzwwws04.ub.uzh.ch/occusense/occusense_progressbar_150.php?id={}"
LOC_URL = "https://www.ub.uzh.ch/de/ub-besuchen/standorte/{}.html"
DELAY = 0.5           # seconds between widget requests
GREEN_BELOW, YELLOW_BELOW = 70, 90   # pct occupied thresholds
EDGE_OCCUPIED = 3.0   # pct used for "> 95% frei" (the widget's bar shows 0.97 free)

# Widget id -> (name, campus, building, fallback seats, location page slug, lat, lon).
# Ids and names from belegung-alle.html (checked 2026-11-19). Buildings and coordinates from
# the UZH floor-plan API (ziplaene-api.uzh.ch/v1/map/search), hardcoded here. Fallback seats are
# the widget's "von N" (or the location page's number for widgets that were closed when checked).
LIBRARIES = {
    10: ("UB Betriebswirtschaft – Lernraum", "Zentrum", "PLR", 120, "betriebswirtschaft_lernraum", 47.37392, 8.552927),
    15: ("Digital Library Space", "Zentrum", "SOC", 40, "digtial-library-space", 47.373999, 8.549605),
    19: ("UB Sozialwissenschaften", "Oerlikon", "AFL", 80, "sozialwissenschaften", 47.410822, 8.540082),
    2: ("UB Lernzentrum Irchel West", "Irchel", "Y63", 220, "lernzentrum_irchel", 47.3955, 8.549948),
    23: ("UB Lernzentrum Irchel Ost/Süd", "Irchel", "Y63", 210, "lernzentrum_irchel", 47.3955, 8.549948),
    11: ("UB Medizin Careum Erdgeschoss", "Zentrum", "C1R", 85, "medizin_careum", 47.375399, 8.553557),
    16: ("UB Medizin Careum Lernraum", "Zentrum", "C1R", 310, "medizin_careum_lernraum", 47.375399, 8.553557),
    12: ("UB Naturwissenschaften", "Irchel", "Y15", 250, "naturwissenschaften", 47.397538, 8.549988),
    21: ("UB Pflanzenwissenschaften", "Botanischer Garten", "BOT", 40, "pflanzenwissenschaften", 47.358897, 8.560108),
    17: ("UB Psychologie", "Oerlikon", "BIN", 180, "psychologie", 47.414532, 8.549137),
    20: ("UB Veterinärmedizin", "Irchel", "TFA", 50, "veterinaermedizin", 47.400915, 8.55163),
    18: ("UB Volkswirtschaft", "Zentrum", "KOL", 74, "volkswirtschaft", 47.374544, 8.548613),
}

_VALUE = re.compile(r"""\bvalue\s*:\s*(['"])(.*?)\1""", re.S)
_FREE = re.compile(r"^(?:ca\.?\s*)?([<>])?\s*(\d+(?:[.,]\d+)?)\s*%\s*frei(?:\s+von\s+(\d+))?", re.I)


def parse_widget(text: str) -> dict:
    """Parse one widget page into {'closed', 'free', 'seats', 'pct'}; raises ValueError if no
    occupancy label is found. 'free' and 'pct' are None when closed; 'seats' None if not shown."""
    for _, raw in _VALUE.findall(text):
        label = " ".join(html.unescape(raw).split())
        if label.lower().startswith("geschlossen"):
            return {"closed": True, "free": None, "seats": None, "pct": None}
        m = _FREE.match(label)
        if m:
            cmp, free, seats = m.group(1), float(m.group(2).replace(",", ".")), m.group(3)
            if not 0 <= free <= 100:
                raise ValueError(f"free share out of range: {label!r}")
            if cmp == ">":
                pct = EDGE_OCCUPIED
            elif cmp == "<":
                pct = 100 - EDGE_OCCUPIED
            else:
                pct = 100 - free
            return {"closed": False, "free": free, "seats": int(seats) if seats else None, "pct": pct}
    raise ValueError("no occupancy label in widget page")


def status_for(pct: float | None) -> str | None:
    if pct is None:
        return None
    return "green" if pct < GREEN_BELOW else "yellow" if pct < YELLOW_BELOW else "red"


def place_id(wid: int) -> str:
    return f"{KEY}:{wid}"


def make_place(wid: int, seats: int | None = None) -> Place:
    name, campus, building, fallback, slug, lat, lon = LIBRARIES[wid]
    return Place(id=place_id(wid), uni="UZH", name=name, source=KEY, campus=campus, building=building,
                 seats=seats or fallback, lat=lat, lon=lon, url=LOC_URL.format(slug))


def make_sample(wid: int, parsed: dict, ts: str) -> Sample:
    return Sample(place_id=place_id(wid), ts=ts, status=status_for(parsed["pct"]), pct=parsed["pct"],
                  max=parsed["seats"], closed=parsed["closed"])


def fetch(http, now: datetime | None = None, sleep=time.sleep) -> tuple[list[Place], list[Sample]]:
    """Fetch all widgets one after another. A failing widget is skipped with a warning (its place
    is still returned with the static metadata); if every widget fails, raise."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    ts = utc_iso(now)
    places, samples, errors = [], [], []
    for i, wid in enumerate(LIBRARIES):
        if i:
            sleep(DELAY)
        try:
            parsed = parse_widget(http.get_text(WIDGET_URL.format(wid)))
        except Exception as e:
            log.warning("uzh_ub widget %s failed: %s: %s", wid, type(e).__name__, e)
            errors.append(f"{wid}: {type(e).__name__}: {e}")
            places.append(make_place(wid))
            continue
        places.append(make_place(wid, parsed["seats"]))
        samples.append(make_sample(wid, parsed, ts))
    if not samples:
        raise RuntimeError(f"all {len(errors)} UZH UB widgets failed; first: {errors[0]}")
    return places, samples


SOURCES = [live.Source(key=KEY, every=300, max_age=900, fetch=fetch)]
