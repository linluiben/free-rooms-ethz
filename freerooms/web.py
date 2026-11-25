"""Tiny stdlib web server: static UI + JSON API reading from SQLite.

GET /                         -> UI (with ?view=spaces or ?view=near the study places are inlined too)
GET /api/rooms?at=ISO         -> status of every room at `at` (default: now, Zurich time), each with
                                 `unreliable` and `unreliable_reason` (frequent short-notice bookings)
GET /api/room?id=..&date=..   -> one room's intervals for a day (for the timeline view)
GET /api/roomdetail?id=..     -> room attributes (seats, windows, food, sockets, ...), cached from ETH,
                                 plus `unreliable` and `unreliable_reason`;
                                 404 for rooms of other universities (ETH has no details for them)
GET /api/places               -> study places with their latest live occupancy sample
GET /api/place?id=..&date=..  -> one study place (and its sub-places) with that day's samples
GET /api/buildings            -> building coordinates [{uni, code, lat, lon, name, source}]
GET /api/health               -> data freshness per source (last success, error, row counts, stale)
GET /healthz                  -> ok
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import fetch, geo, live, places, reliability, store, uzh
from .api import ApiClient
from .availability import TZ, building_hours, now_local, query

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
DETAIL_TTL = timedelta(days=7)   # room attributes rarely change
SOURCES = (fetch.SOURCE,)          # always listed in /api/health, even before the first fetch


def room_sources(unis: tuple[str, ...]) -> tuple[str, ...]:
    """Health keys of the enabled universities' room fetches, e.g. ('eth_rooms', 'uzh_rooms')."""
    return tuple(fetch.SOURCES[u] for u in unis)


def simplify_detail(raw: dict) -> dict:
    """Keep only what the UI shows: tabs of (name, label, value), no attachments or empty values."""
    tabs = []
    for tab in raw.get("tabs") or []:
        items = [{"name": a.get("name"), "label": a.get("label"), "value": a.get("value")}
                 for a in tab.get("attributes") or [] if not a.get("isAttachment") and a.get("value")]
        if items and tab.get("title"):
            tabs.append({"title": tab["title"], "items": items})
    return {"description": raw.get("description"), "info": raw.get("infoText"), "tabs": tabs}


def with_coords(items: list[dict], where: dict[tuple[str, str], tuple[float, float]]) -> list[dict]:
    """Give rooms and places their building's lat/lon. A place's own coordinates win (UZH libraries
    carry theirs); without a known building both stay None and the UI says "distance unknown"."""
    for x in items:
        if x.get("lat") is None or x.get("lon") is None:
            x["lat"], x["lon"] = where.get((x.get("uni"), x.get("building")), (None, None))
    return items


def make_handler(db_path: str, client: ApiClient | None = None, live_sources: tuple[str, ...] = (),
                 rooms_sources: tuple[str, ...] = SOURCES):
    client = client or ApiClient()
    sources = (*rooms_sources, *live_sources)  # listed in health even before their first fetch
    detail_lock = threading.Lock()   # one upstream request at a time
    conn = store.connect(db_path)
    try:
        n = geo.sync(conn)               # buildings.json -> table, when the file is newer
        if n:
            log.info("loaded coordinates of %d buildings", n)
    finally:
        conn.close()
    class Handler(BaseHTTPRequestHandler):
        server_version = "free-rooms-ethz"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    # Inline the current data so the first paint needs no extra request.
                    data = json.dumps(self._rooms({k: v for k, v in qs.items() if k == "at"}), ensure_ascii=False).replace("</", "<\\/")
                    html = (STATIC / "index.html").read_text().replace("/*INITIAL_DATA*/null", data, 1)
                    if qs.get("view") in ("spaces", "near"):
                        data = json.dumps(self._places(), ensure_ascii=False).replace("</", "<\\/")
                        html = html.replace("/*INITIAL_PLACES*/null", data, 1)
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                elif url.path == "/overpass.woff2":   # the UI's typeface (OFL, see static/overpass-OFL.txt)
                    self._send(200, (STATIC / "overpass.woff2").read_bytes(), "font/woff2", "public, max-age=604800")
                elif url.path == "/healthz":
                    self._send(200, b"ok", "text/plain")
                elif url.path == "/api/health":
                    h = self._health()  # 503 when stale, so plain uptime monitors notice
                    self._json(h, 503 if h["status"] == "stale" else 200)
                elif url.path == "/api/buildings":
                    self._json(self._buildings())
                elif url.path == "/api/places":
                    self._json(self._places())
                elif url.path == "/api/place":
                    p = self._place(qs)
                    self._json(p or {"error": "unknown place"}, 200 if p else 404)
                elif url.path == "/api/rooms":
                    self._json(self._rooms(qs))
                elif url.path == "/api/room":
                    self._json(self._room(qs))
                elif url.path == "/api/roomdetail":
                    body = self._detail(qs)
                    if body is None:
                        self._json({"error": "no room details for this university"}, 404)
                    else:
                        self._send(200, body.encode(), "application/json; charset=utf-8", "max-age=3600")
                else:
                    self._send(404, b"not found", "text/plain")
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception:
                log.exception("request failed")
                self._json({"error": "internal error"}, 500)

        def _rooms(self, qs):
            t = datetime.fromisoformat(qs["at"]) if qs.get("at") else now_local()
            conn = store.connect(db_path)
            try:
                return {
                    "at": t.isoformat(timespec="minutes"),
                    "now": now_local().isoformat(timespec="minutes"),
                    "last_fetch": store.get_meta(conn, "last_fetch"),
                    "health": store.health(conn, sources),
                    "rooms": reliability.annotate(with_coords(query(conn, t), geo.coords(conn)),
                                                  reliability.scores(conn)),
                    "buildings": building_hours(conn, t),
                }
            finally:
                conn.close()

        def _health(self):
            conn = store.connect(db_path)
            try:
                return store.health(conn, sources)
            finally:
                conn.close()

        def _places(self):
            conn = store.connect(db_path)
            try:
                return {"now": now_local().isoformat(timespec="minutes"),
                        "health": store.health(conn, sources),
                        "places": with_coords(places.latest(conn), geo.coords(conn))}
            finally:
                conn.close()

        def _buildings(self):
            conn = store.connect(db_path)
            try:
                return [dict(r) for r in conn.execute("SELECT uni, code, lat, lon, name, source FROM buildings ORDER BY uni, code")]
            finally:
                conn.close()

        def _place(self, qs) -> dict | None:
            """One place as in /api/places plus its samples for a Zurich day; its sub-places in `kids`.
            Each sample also gets `local` (naive Zurich time). None for an id that isn't in `places`."""
            pid = qs.get("id")
            if not pid:
                raise ValueError("missing id")
            day = date.fromisoformat(qs["date"]) if qs.get("date") else now_local().date()
            start = datetime(day.year, day.month, day.day, tzinfo=TZ)
            end = start + timedelta(days=1)   # wall-clock midnight; DST days are 23 or 25 h long
            conn = store.connect(db_path)
            try:
                all_places = with_coords(places.latest(conn), geo.coords(conn))
                place = next((p for p in all_places if p["id"] == pid), None)
                if place is None:
                    return None
                kids = [p for p in all_places if p["parent_id"] == pid]
                samples = places.samples_between(conn, [pid, *(k["id"] for k in kids)],
                                                 places.utc_iso(start), places.utc_iso(end))
            finally:
                conn.close()
            for p in (place, *kids):
                p["samples"] = samples[p["id"]]
                for s in p["samples"]:
                    s["local"] = datetime.fromisoformat(s["ts"]).astimezone(TZ).strftime("%Y-%m-%dT%H:%M")
            return {"now": now_local().isoformat(timespec="minutes"), "date": day.isoformat(),
                    "place": place, "kids": kids}

        def _room(self, qs):
            rid = qs.get("id") or ""
            day = datetime.fromisoformat(qs["date"]) if qs.get("date") else now_local()
            d0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
            d1 = d0 + timedelta(days=1)
            conn = store.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT start, end, kind, title FROM intervals WHERE room_id=? AND end>? AND start<? ORDER BY start",
                    (rid, d0.isoformat(), d1.isoformat())).fetchall()
                return {"id": rid, "date": d0.date().isoformat(), "intervals": [dict(r) for r in rows]}
            finally:
                conn.close()

        def _detail(self, qs) -> str | None:
            """The room's details as JSON text; None for a room ETH Rauminfo doesn't know (UZH)."""
            rid = qs.get("id") or ""
            conn = store.connect(db_path)
            try:
                # Only rooms we know: the endpoint must not become an open proxy to ETH.
                room = conn.execute("SELECT uni FROM rooms WHERE id=?", (rid,)).fetchone()
                if not room:
                    raise ValueError("unknown room")
                if room["uni"] != "ETH":
                    return None
                row = conn.execute("SELECT fetched_at, json FROM room_details WHERE room_id=?", (rid,)).fetchone()
                now = datetime.now(timezone.utc)
                if row and now - datetime.fromisoformat(row["fetched_at"]) < DETAIL_TTL:
                    return self._with_reliability(conn, rid, row["json"])
                try:
                    with detail_lock:
                        body = json.dumps(simplify_detail(client.room_detail(rid)), ensure_ascii=False)
                except Exception:
                    if row:   # stale beats nothing
                        log.warning("room detail refresh failed for %s, serving cached copy", rid)
                        return self._with_reliability(conn, rid, row["json"])
                    raise
                conn.execute("INSERT OR REPLACE INTO room_details(room_id, fetched_at, json) VALUES (?,?,?)",
                             (rid, now.isoformat(timespec="seconds"), body))
                conn.commit()
                return self._with_reliability(conn, rid, body)
            finally:
                conn.close()

        @staticmethod
        def _with_reliability(conn, rid: str, body: str) -> str:
            """The cached detail JSON plus our own short-notice flag (not cached: it changes per fetch)."""
            s = reliability.scores(conn).get(rid) or {}
            d = {**json.loads(body), "unreliable": bool(s.get("unreliable")), "unreliable_reason": s.get("reason")}
            return json.dumps(d, ensure_ascii=False)

    return Handler


def refresh_intervals(every: float, unis: tuple[str, ...]) -> dict[str, float]:
    """Seconds between background fetches per university: UZH at most every uzh.REFRESH_MIN."""
    return {u: max(every, uzh.REFRESH_MIN) if u == "UZH" else every for u in unis}


def _last_success_age(conn, uni: str) -> float:
    row = conn.execute("SELECT last_success FROM source_health WHERE source=?", (fetch.SOURCES[uni],)).fetchone()
    return time.time() - datetime.fromisoformat(row[0]).timestamp() if row and row[0] else float("inf")


def refresher(db_path: str, every: float, fetch_kwargs: dict, unis: tuple[str, ...] = ("ETH",),
              uzh_kwargs: dict | None = None) -> None:
    """Background loop: refetch each university when its data is older than its interval (so the
    first run is immediate if the DB is empty or stale). A failed fetch is retried in ~10 min."""
    intervals = refresh_intervals(every, unis)
    retry_at: dict[str, float] = {}
    while True:
        waits = []
        conn = store.connect(db_path)
        try:
            for uni in unis:
                age = _last_success_age(conn, uni)
                if age >= intervals[uni] and time.time() >= retry_at.get(uni, 0):
                    try:
                        fetch.run_uni(conn, uni, eth=fetch_kwargs, uzh=uzh_kwargs)
                        age = 0
                    except Exception:
                        log.exception("background %s fetch failed", uni)
                        retry_at[uni] = time.time() + 600
                        waits.append(600)
                        continue
                waits.append(intervals[uni] - age)
        finally:
            conn.close()
        time.sleep(max(60, min(waits, default=every)))


def serve(db_path: str, host: str, port: int, refresh: float | None, fetch_kwargs: dict,
          live_on: bool = False, unis: tuple[str, ...] = ("ETH",), uzh_kwargs: dict | None = None) -> None:
    live_keys: tuple[str, ...] = ()
    if live_on:
        srcs = live.discover()
        live_keys = tuple(s.key for s in srcs)
        threading.Thread(target=live.loop, args=(db_path, None, srcs), daemon=True).start()
    uzh_kwargs = uzh_kwargs or {}
    if refresh:
        # Data may be up to one interval old by design; only flag it once a refresh is clearly overdue.
        iv = refresh_intervals(refresh, ("ETH", "UZH"))
        fetch_kwargs = {**fetch_kwargs, "max_age": max(fetch_kwargs.get("max_age") or store.DEFAULT_MAX_AGE, 1.5 * iv["ETH"])}
        uzh_kwargs = {**uzh_kwargs, "max_age": max(uzh_kwargs.get("max_age") or store.DEFAULT_MAX_AGE, 1.5 * iv["UZH"])}
        threading.Thread(target=refresher, args=(db_path, refresh, fetch_kwargs, unis, uzh_kwargs), daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, fetch_kwargs.get("client"), live_keys,
                                                           room_sources(unis)))
    log.info("serving on http://%s:%d", host, port)
    httpd.serve_forever()
