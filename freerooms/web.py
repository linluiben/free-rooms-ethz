"""Tiny stdlib web server: static UI + JSON API reading from SQLite.

GET /                         -> UI
GET /api/rooms?at=ISO         -> status of every room at `at` (default: now, Zurich time)
GET /api/room?id=..&date=..   -> one room's intervals for a day (for the timeline view)
GET /healthz                  -> ok
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import fetch, store
from .availability import now_local, query

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def make_handler(db_path: str):
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
                elif url.path == "/api/rooms":
                    self._json(self._rooms(qs))
                elif url.path == "/api/room":
                    self._json(self._room(qs))
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
                    "rooms": query(conn, t),
                }
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


def serve(db_path: str, host: str, port: int, refresh: float | None, fetch_kwargs: dict) -> None:
    if refresh:
        threading.Thread(target=refresher, args=(db_path, refresh, fetch_kwargs), daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    log.info("serving on http://%s:%d", host, port)
    httpd.serve_forever()
