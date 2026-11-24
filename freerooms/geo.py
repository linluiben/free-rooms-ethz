"""Building coordinates and walking distances (Phase 3: "nearest free place").

Coordinates are per building, not per room: one point (the centre of the building outline) is
precise enough to rank places by walking time on a campus. They change rarely, so they ship
with the code in `buildings.json` and are refreshed by hand with `python -m freerooms buildings`:

- UZH: the floor-plan API (ziplaene-api.uzh.ch/v1/map/buildings), lat/lon per building code.
- ETH: OpenStreetMap via the Overpass API. A few ETH outlines carry the building code as `ref`,
  most only in the name ("ETH HCI", "ETH Hauptgebäude (HG)"); we ask for both inside the bounding
  box of each ETH campus, and take the centre of the outline.
- ETH, for buildings OSM can't name: ETH's own building map (mapsearch.ethz.ch), which gives the
  address and its point. It agrees with the OSM centres to ~20 m where both exist.

The user's location never reaches the server: the browser does the distance maths itself.
This module's `distance_m` and `walk_min` are the reference for the UI's copy (and its tests).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import sqlite3
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).parent / "buildings.json"
UZH_URL = "https://ziplaene-api.uzh.ch/v1/map/buildings"
# Tried in order; the main instance sometimes refuses clients, the mirrors carry the same data.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# ETH's building map page: address and its point for a building code. HTTP only (no TLS there).
MAPSEARCH_URL = "http://www.mapsearch.ethz.ch/map.do?gebaeudeMap={code}&lang=de"
MAPSEARCH_GIVE_UP = 3   # consecutive failures: the site is down, don't wait out every code
# (south, west, north, east) around each ETH campus, generous enough to hold every building.
ETH_BBOXES = {
    "Zentrum": (47.366, 8.536, 47.386, 8.562),
    "Hönggerberg": (47.400, 8.496, 47.416, 8.520),
    "Basel": (47.555, 7.575, 47.570, 7.600),   # BSS, D-BSSE
}
# ETH buildings we show rooms or study places in (rooms.txt, study spaces, sensor rooms as of
# Nov 2026). The refresh also adds every ETH building found in the database.
ETH_CODES = (
    "BSS", "CAB", "CHN", "CLA", "ETA", "ETF", "ETZ", "GLC", "HCI", "HCP", "HG", "HIL", "HIT", "HPH",
    "HPK", "HPL", "HPR", "HPT", "HPV", "HWC", "IFW", "LEE", "LFO", "LFV", "LFW", "ML", "MM", "NO", "RZ",
)
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}$")
SPREAD_M = 250        # outlines with the same ref further apart than this: ambiguous, warn

# Walking: 80 m/min (4.8 km/h), and paths are ~25 % longer than the straight line on campus.
WALK_M_PER_MIN = 80.0
DETOUR = 1.25


@dataclass
class Building:
    uni: str            # 'ETH' | 'UZH'
    code: str           # e.g. 'HG', 'KOL'
    lat: float
    lon: float
    source: str         # 'osm' | 'uzh_ziplaene'
    name: str | None = None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_min(metres: float) -> int:
    """Rough walking time in whole minutes (at least 1)."""
    return max(1, math.ceil(metres * DETOUR / WALK_M_PER_MIN))


# --- sources ---

def parse_uzh(raw: dict) -> list[Building]:
    out = []
    for b in raw.get("buildings") or []:
        code, lat, lon = b.get("shortName"), b.get("lat"), b.get("lon")
        if not isinstance(code, str) or not CODE_RE.match(code) or not _valid(lat, lon):
            continue
        out.append(Building("UZH", code, round(float(lat), 6), round(float(lon), 6), "uzh_ziplaene",
                            (b.get("zusatzInformation") or "").strip() or None))
    return out


def overpass_query(codes: list[str] | tuple[str, ...]) -> str:
    refs = "|".join(sorted(set(codes)))
    parts = "".join(f'nwr["ref"~"^({refs})$"]({s},{w},{n},{e});nwr["name"~"^ETH"]({s},{w},{n},{e});'
                    for s, w, n, e in ETH_BBOXES.values())
    return f"[out:json][timeout:60];({parts});out center tags;"


def _score(tags: dict) -> int:
    """How sure we are an OSM element is the ETH building itself (not an entrance or a shop)."""
    text = " ".join(str(tags.get(k, "")) for k in ("operator", "name", "owner")).lower()
    return (2 if "building" in tags else 0) + (1 if "eth" in text or "eidgen" in text else 0)


# Most ETH outlines in OSM carry the code only in the name: "ETH HCI", "ETH Zürich BSS",
# "ETH: NO", "ETH Hauptgebäude (HG)". Shops and rooms ("ASVZ CAB Move & Relax") don't match.
NAME_RES = (re.compile(r"^ETH(?: Zürich)?:?\s+([A-Z][A-Z0-9]{0,4})$"), re.compile(r"^ETH\b.*\(([A-Z][A-Z0-9]{0,4})\)$"))


def code_from_tags(tags: dict, wanted: set[str]) -> str | None:
    """The ETH building code of an OSM element: `ref`, else the name."""
    ref = str(tags.get("ref", "")).strip()
    if ref in wanted:
        return ref
    name = str(tags.get("name", "")).strip()
    for r in NAME_RES:
        m = r.match(name)
        if m and m.group(1) in wanted:
            return m.group(1)
    return None


def parse_osm(raw: dict, codes: list[str] | tuple[str, ...]) -> list[Building]:
    """One point per wanted code: the centre of the best-matching outline(s)."""
    wanted = set(codes)
    found: dict[str, list[tuple[int, float, float]]] = {}
    for e in raw.get("elements") or []:
        tags = e.get("tags") or {}
        ref = code_from_tags(tags, wanted)
        c = e.get("center") or e
        if ref is None or not _valid(c.get("lat"), c.get("lon")):
            continue
        found.setdefault(ref, []).append((_score(tags), float(c["lat"]), float(c["lon"])))
    out = []
    for code in sorted(found):
        best = max(s for s, _, _ in found[code])
        pts = [(la, lo) for s, la, lo in found[code] if s == best]
        lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        spread = max(distance_m(lat, lon, la, lo) for la, lo in pts)
        if spread > SPREAD_M:
            log.warning("OSM has %d outlines with ref=%s up to %.0f m apart; using their middle", len(pts), code, spread)
        out.append(Building("ETH", code, round(lat, 6), round(lon, 6), "osm"))
    missing = sorted(wanted - set(found))
    if missing:
        log.warning("no OSM element with the code in ref or name for ETH buildings: %s", ", ".join(missing))
    return out


_MS_POINT = re.compile(r"center\s*:\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]")
_MS_ADDR = re.compile(r"html\s*:\s*'([^']+)'")


def parse_mapsearch(html: str, code: str) -> Building | None:
    """ETH's map page for one building: its point, if it lies on an ETH campus."""
    m = _MS_POINT.search(html)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not any(s <= lat <= n and w <= lon <= e for s, w, n, e in ETH_BBOXES.values()):
        log.warning("ETH map puts %s at %.5f, %.5f, outside every ETH campus; ignored", code, lat, lon)
        return None
    a = _MS_ADDR.search(html)
    return Building("ETH", code, round(lat, 6), round(lon, 6), "eth_mapsearch", a.group(1).strip() if a else None)


def _valid(lat, lon) -> bool:
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and 45 < lat < 48.5 and 5.5 < lon < 11


def fetch_uzh(http) -> list[Building]:
    return parse_uzh(http.get_json(UZH_URL))


def fetch_osm(http, codes, urls=OVERPASS_URLS) -> list[Building]:
    q = urllib.parse.urlencode({"data": overpass_query(codes)})
    errors = []
    for url in urls:
        try:
            return parse_osm(http.get_json(f"{url}?{q}"), codes)
        except Exception as e:   # next mirror
            log.warning("Overpass %s failed: %s", url, e)
            errors.append(f"{url}: {e}")
    raise RuntimeError("every Overpass instance failed: " + "; ".join(errors))


def fetch_mapsearch(http, codes, pause: float = 1.0) -> list[Building]:
    """One page per code, politely spaced; stops early when the site is down."""
    out, missing, failed = [], [], 0
    for i, code in enumerate(codes):
        if failed >= MAPSEARCH_GIVE_UP:
            log.warning("ETH map pages unreachable; skipping the remaining %d codes", len(codes) - i)
            missing += codes[i:]
            break
        if i and pause:
            time.sleep(pause)
        try:
            b = parse_mapsearch(http.get_text(MAPSEARCH_URL.format(code=urllib.parse.quote(code))), code)
            failed = 0
        except Exception as e:
            log.warning("ETH map page for %s failed: %s", code, e)
            b, failed = None, failed + 1
        (out if b else missing).append(b or code)
    if missing:
        log.warning("no coordinates for ETH buildings: %s", ", ".join(missing))
    return out


def fetch_eth(http, codes, urls=OVERPASS_URLS, pause: float = 1.0) -> list[Building]:
    """OSM outline centres; ETH's own map for the codes OSM doesn't name."""
    osm = fetch_osm(http, codes, urls)
    have = {b.code for b in osm}
    return osm + fetch_mapsearch(http, [c for c in codes if c not in have], pause)


# --- file and table ---

def load_file(path: Path = BUNDLED) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"generated": None, "buildings": []}


def write_file(data: dict, path: Path = BUNDLED) -> None:
    """One building per line, so a refresh reads as a small diff."""
    rows = sorted(data["buildings"], key=lambda b: (b["uni"], b["code"]))
    head = {k: v for k, v in data.items() if k != "buildings"}
    body = ",\n".join("  " + json.dumps(b, ensure_ascii=False) for b in rows)
    text = json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "buildings": [\n' + body + "\n ]\n}\n"
    json.loads(text)   # never write a file we can't read back
    Path(path).write_text(text)


def merge(data: dict, uni: str, buildings: list[Building], source_note: str) -> dict:
    """Replace one university's buildings in the file data, keep the others."""
    keep = [b for b in data.get("buildings") or [] if b["uni"] != uni]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = {**(data.get("sources") or {}), uni: {"fetched": now, "from": source_note, "count": len(buildings)}}
    return {"generated": now, "sources": sources, "buildings": keep + [asdict(b) for b in buildings]}


def sync(conn: sqlite3.Connection, path: Path = BUNDLED) -> int:
    """Load the buildings file into the table when the file is newer than what the table holds."""
    data = load_file(path)
    stamp = data.get("generated")
    have = conn.execute("SELECT value FROM meta WHERE key='buildings_file'").fetchone()
    if stamp is None or (have and have[0] == stamp):
        return 0
    conn.execute("DELETE FROM buildings")
    conn.executemany("INSERT OR REPLACE INTO buildings(uni, code, lat, lon, name, source) VALUES (?,?,?,?,?,?)",
                     [(b["uni"], b["code"], b["lat"], b["lon"], b.get("name"), b.get("source")) for b in data["buildings"]])
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('buildings_file', ?)", (stamp,))
    conn.commit()
    return len(data["buildings"])


def coords(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[float, float]]:
    return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT uni, code, lat, lon FROM buildings")}


def eth_codes(conn: sqlite3.Connection | None) -> list[str]:
    codes = set(ETH_CODES)
    if conn is not None:
        for (b,) in conn.execute("SELECT DISTINCT building FROM rooms WHERE uni='ETH' UNION "
                                 "SELECT DISTINCT building FROM places WHERE uni='ETH' AND building IS NOT NULL"):
            if b and CODE_RE.match(b):
                codes.add(b)
    return sorted(codes)


def refresh(http, unis: tuple[str, ...], conn: sqlite3.Connection | None = None, path: Path = BUNDLED,
            pause: float = 1.0) -> dict[str, object]:
    """Fetch the coordinates of `unis`, merge them into the buildings file and load it.
    Returns per university the number of buildings, or the error text."""
    data, res = load_file(path), {}
    for uni in unis:
        try:
            if uni == "UZH":
                data = merge(data, "UZH", fetch_uzh(http), UZH_URL)
            elif uni == "ETH":
                codes = eth_codes(conn)
                data = merge(data, "ETH", fetch_eth(http, codes, pause=pause),
                             "OpenStreetMap (Overpass), code in ref or name; else ETH map " + MAPSEARCH_URL)
            else:
                raise ValueError(f"unknown university {uni}")
            res[uni] = data["sources"][uni]["count"]
        except Exception as e:
            log.exception("building coordinates for %s failed", uni)
            res[uni] = f"FAILED: {e}"
    if any(isinstance(v, int) for v in res.values()):
        write_file(data, path)
        if conn is not None:
            sync(conn, path)
    return res
