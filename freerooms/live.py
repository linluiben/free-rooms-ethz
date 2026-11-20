"""Live occupancy loop: runs every source in freerooms/sources/ on its own interval.

Each source is isolated: a failure is logged and recorded in source_health, the others go on.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import places, sources, store
from .http import Http

log = logging.getLogger(__name__)
TICK = 30  # seconds between checks for due sources


@dataclass
class Source:
    key: str                      # source_health key and Place.source, e.g. 'eth_availability'
    every: float                  # seconds between fetches
    max_age: float                # older data counts as stale
    fetch: Callable[[Http], tuple[list[places.Place], list[places.Sample]]]
    order: int = 0                # lower runs first in a tick (metadata before samples)


def discover() -> list[Source]:
    found = []
    for m in pkgutil.iter_modules(sources.__path__):
        mod = importlib.import_module(f"{sources.__name__}.{m.name}")
        found.extend(getattr(mod, "SOURCES", []))
    return sorted(found, key=lambda s: (s.order, s.key))


def due(conn: sqlite3.Connection, src: Source, now: float) -> bool:
    row = conn.execute("SELECT last_attempt FROM source_health WHERE source=?", (src.key,)).fetchone()
    return not row or not row[0] or now - datetime.fromisoformat(row[0]).timestamp() >= src.every - TICK / 2


def run_source(conn: sqlite3.Connection, src: Source, http: Http) -> dict | None:
    """Fetch one source and store the result. Never raises; returns the counts or None on error."""
    try:
        ps, ss = src.fetch(http)
        counts = {"places": places.upsert_places(conn, ps), "samples": len(ss)}
        counts["new_samples"] = places.add_samples(conn, ss)
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.warning("live source %s failed: %s: %s", src.key, type(e).__name__, e)
        store.record_attempt(conn, src.key, error=f"{type(e).__name__}: {e}", max_age=src.max_age)
        return None
    store.record_attempt(conn, src.key, counts=counts, max_age=src.max_age)
    log.debug("live source %s: %s", src.key, counts)
    return counts


def run_due(conn: sqlite3.Connection, http: Http, srcs: list[Source], force: bool = False) -> dict:
    now = time.time()
    return {s.key: run_source(conn, s, http) for s in srcs if force or due(conn, s, now)}


def loop(db_path: str, http: Http | None = None, srcs: list[Source] | None = None) -> None:
    http, srcs = http or Http(), srcs if srcs is not None else discover()
    log.info("live sources: %s", ", ".join(s.key for s in srcs) or "none")
    while True:
        conn = store.connect(db_path)
        try:
            run_due(conn, http, srcs)
        except Exception:
            log.exception("live loop tick failed")
        finally:
            conn.close()
        time.sleep(TICK)
