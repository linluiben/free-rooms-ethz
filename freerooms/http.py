"""Minimal polite HTTP GET for the live sources (the Rauminfo API has its own client in api.py)."""

from __future__ import annotations

import json
import urllib.request

from .api import USER_AGENT


class Http:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def get(self, url: str, accept: str = "*/*") -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def get_json(self, url: str) -> object:
        return json.loads(self.get(url, "application/json"))

    def get_text(self, url: str) -> str:
        return self.get(url, "text/html, text/plain").decode("utf-8", "replace")
