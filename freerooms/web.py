"""Tiny stdlib web server: static UI + JSON API reading from SQLite.

GET /                         -> UI
GET /api/rooms?at=ISO         -> status of every room at `at` (default: now, Zurich time)
GET /api/room?id=..&date=..   -> one room's intervals for a day (for the timeline view)
GET /api/roomdetail?id=..     -> room attributes (seats, windows, food, sockets, ...), cached from ETH
GET /api/places               -> study places with their latest live occupancy sample
GET /api/health               -> data freshness per source (last success, error, row counts, stale)
GET /healthz                  -> ok
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import fetch, live, places, store
from .api import ApiClient
from .availability import building_hours, now_local, query

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
DETAIL_TTL = timedelta(days=7)   # room attributes rarely change
SOURCES = (fetch.SOURCE,)          # always listed in /api/health, even before the first fetch


def simplify_detail(raw: dict) -> dict:
    """Keep only what the UI shows: tabs of (name, label, value), no attachments or empty values."""
    tabs = []
    for tab in raw.get("tabs") or []:
        items = [{"name": a.get("name"), "label": a.get("label"), "value": a.get("value")}
                 for a in tab.get("attributes") or [] if not a.get("isAttachment") and a.get("value")]
        if items and tab.get("title"):
            tabs.append({"title": tab["title"], "items": items})
    return {"description": raw.get("description"), "info": raw.get("infoText"), "tabs": tabs}


def make_handler(db_path: str, client: ApiClient | None = None, live_sources: tuple[str, ...] = ()):
    client = client or ApiClient()
    sources = (*SOURCES, *live_sources)  # listed in health even before their first fetch
    detail_lock = threading.Lock()   # one upstream request at a time
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
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                elif url.path == "/healthz":
                    self._send(200, b"ok", "text/plain")
                elif url.path == "/api/health":
                    h = self._health()  # 503 when stale, so plain uptime monitors notice
                    self._json(h, 503 if h["status"] == "stale" else 200)
                elif url.path == "/api/places":
                    self._json(self._places())
                elif url.path == "/api/rooms":
                    self._json(self._rooms(qs))
                elif url.path == "/api/room":
                    self._json(self._room(qs))
                elif url.path == "/api/roomdetail":
                    self._send(200, self._detail(qs).encode(), "application/json; charset=utf-8", "max-age=3600")
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
                    "rooms": query(conn, t),
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
                        "health": store.health(conn, sources), "places": places.latest(conn)}
            finally:
                conn.close()

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

        def _detail(self, qs) -> str:
            rid = qs.get("id") or ""
            conn = store.connect(db_path)
            try:
                # Only rooms we know: the endpoint must not become an open proxy to ETH.
                if not conn.execute("SELECT 1 FROM rooms WHERE id=?", (rid,)).fetchone():
                    raise ValueError("unknown room")
                row = conn.execute("SELECT fetched_at, json FROM room_details WHERE room_id=?", (rid,)).fetchone()
                now = datetime.now(timezone.utc)
                if row and now - datetime.fromisoformat(row["fetched_at"]) < DETAIL_TTL:
                    return row["json"]
                try:
                    with detail_lock:
                        body = json.dumps(simplify_detail(client.room_detail(rid)), ensure_ascii=False)
                except Exception:
                    if row:   # stale beats nothing
                        log.warning("room detail refresh failed for %s, serving cached copy", rid)
                        return row["json"]
                    raise
                conn.execute("INSERT OR REPLACE INTO room_details(room_id, fetched_at, json) VALUES (?,?,?)",
                             (rid, now.isoformat(timespec="seconds"), body))
                conn.commit()
                return body
            finally:
                conn.close()

    return Handler


def refresher(db_path: str, every: float, fetch_kwargs: dict) -> None:
    """Background loop: refetch every `every` seconds (first run immediately if DB is empty/stale)."""
    while True:
        conn = store.connect(db_path)
        try:
            last = store.get_meta(conn, "last_fetch")
            age = (time.time() - datetime.fromisoformat(last).timestamp()) if last else float("inf")
            if age >= every:
                fetch.run(conn, **fetch_kwargs)
                age = 0
        except Exception:
            log.exception("background fetch failed")
            age = every - 600  # retry in ~10 min
        finally:
            conn.close()
        time.sleep(max(60, every - age))


def serve(db_path: str, host: str, port: int, refresh: float | None, fetch_kwargs: dict,
          live_on: bool = False) -> None:
    live_keys: tuple[str, ...] = ()
    if live_on:
        srcs = live.discover()
        live_keys = tuple(s.key for s in srcs)
        threading.Thread(target=live.loop, args=(db_path, None, srcs), daemon=True).start()
    if refresh:
        # Data may be up to one interval old by design; only flag it once a refresh is clearly overdue.
        fetch_kwargs = {**fetch_kwargs, "max_age": max(fetch_kwargs.get("max_age") or store.DEFAULT_MAX_AGE, 1.5 * refresh)}
        threading.Thread(target=refresher, args=(db_path, refresh, fetch_kwargs), daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, fetch_kwargs.get("client"), live_keys))
    log.info("serving on http://%s:%d", host, port)
    httpd.serve_forever()
