"""Thin client for the (undocumented) ETH Rauminfo JSON API.

Base: https://ethz.ch/bin/ethz/roominfo?path=<path>&<params>
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date

from . import __version__

BASE = "https://ethz.ch/bin/ethz/roominfo?path="
USER_AGENT = f"free-rooms-ethz/{__version__} (+https://github.com/linluiben/free-rooms-ethz)"


class ApiClient:
    def __init__(self, delay: float = 0.3, timeout: float = 20.0, retries: int = 2):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def _get(self, path: str, **params: str) -> object:
        url = BASE + path + "".join(f"&{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(self.retries + 1):
            # be polite: keep a minimum gap between requests
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except Exception:
                if attempt == self.retries:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def rooms(self, lang: str = "de") -> list[dict]:
        return self._get("/v2/rooms", lang=lang)  # type: ignore[return-value]

    def allocations(self, room_id: str, start: date, end: date) -> list[dict]:
        # room_id must be "building floor room" (e.g. "HG F 26.3"), NOT the
        # zero-padded `id` from the room list -- a wrong id silently returns [].
        path = f"/rooms/{urllib.parse.quote(room_id)}/allocations"
        return self._get(path, **{"from": start.isoformat(), "to": end.isoformat()})  # type: ignore[return-value]

    def room_detail(self, room_id: str, lang: str = "en") -> dict:
        # Accepts the same "building floor room" id; returns tabs of attributes
        # (seats, windows, food allowed, sockets, equipment, accessibility, ...).
        return self._get(f"/v2/rooms/{urllib.parse.quote(room_id)}", lang=lang)  # type: ignore[return-value]

    def date_range(self) -> dict:
        return self._get("/min-max-allocation-date")  # type: ignore[return-value]


def query_id(room: dict) -> str:
    """The id the allocations endpoint expects: building + floor + room."""
    return f"{room['building']} {room['floor']} {room['room']}"
