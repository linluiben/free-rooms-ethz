"""Thin client for the (undocumented) ETH Rauminfo JSON API.

Base: https://ethz.ch/bin/ethz/roominfo?path=<path>&<params>
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from . import __version__

BASE = "https://ethz.ch/bin/ethz/roominfo?path="
USER_AGENT = f"free-rooms-ethz/{__version__} (+https://github.com/linluiben/free-rooms-ethz)"

# The API sends `x-ratelimit-remaining` (a bucket of ~20 that refills within
# ~1 s). When it runs low, pause this long before the next request.
# Defaults; each can be overridden per ApiClient (CLI flags / env vars).
LOW_REMAINING = 5
LOW_PAUSE = 1.0
RETRY_AFTER_DEFAULT = 60.0  # 429 without a usable Retry-After
RETRY_AFTER_MAX = 300.0     # never sleep longer than this on a server's word


def retry_after(value: str | None, now: datetime | None = None,
                default: float = RETRY_AFTER_DEFAULT, cap: float = RETRY_AFTER_MAX) -> float:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return default
    value = value.strip()
    try:
        secs = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        secs = (when - (now or datetime.now(timezone.utc))).total_seconds()
    if secs != secs:  # NaN
        return default
    return min(max(secs, 0.0), cap)


class ApiClient:
    def __init__(self, delay: float = 0.3, timeout: float = 20.0, retries: int = 2,
                 opener: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 low_remaining: int = LOW_REMAINING, low_pause: float = LOW_PAUSE,
                 retry_after_default: float = RETRY_AFTER_DEFAULT, retry_after_max: float = RETRY_AFTER_MAX):
        self.delay = delay
        self.low_remaining, self.low_pause = low_remaining, low_pause
        self.retry_after_default, self.retry_after_max = retry_after_default, retry_after_max
        self.timeout = timeout
        self.retries = retries
        self._open, self._sleep, self._clock = opener, sleep, clock
        self._last = 0.0
        self._gap = delay  # min gap before the next request; grows when the bucket is low

    def _get(self, path: str, **params: str) -> object:
        url = BASE + path + "".join(f"&{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(self.retries + 1):
            # be polite: keep a minimum gap between requests
            wait = self._last + self._gap - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    self._note_remaining(resp.headers.get("x-ratelimit-remaining"))
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                # 4xx other than 429 won't fix itself by retrying (e.g. 404)
                if attempt == self.retries or (400 <= e.code < 500 and e.code != 429):
                    raise
                self._sleep(retry_after((e.headers or {}).get("Retry-After"), default=self.retry_after_default,
                                        cap=self.retry_after_max) if e.code == 429
                            else 2 * (attempt + 1))
            except Exception:
                if attempt == self.retries:
                    raise
                self._sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def _note_remaining(self, value: str | None) -> None:
        try:
            remaining = int(value) if value is not None else None
        except ValueError:
            remaining = None
        self._gap = (max(self.delay, self.low_pause) if remaining is not None and remaining <= self.low_remaining
                     else self.delay)

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
