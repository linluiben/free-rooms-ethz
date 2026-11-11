"""CLI: python -m freerooms {fetch,serve,now}"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

from . import fetch, store, web
from .api import ApiClient
from .availability import now_local, query


def _csv(s: str | None) -> tuple[str, ...] | None:
    return tuple(x.strip() for x in s.split(",") if x.strip()) if s else None


def main() -> None:
    p = argparse.ArgumentParser(prog="freerooms", description=__doc__)
    p.add_argument("--db", default=os.environ.get("FREEROOMS_DB", "data/freerooms.sqlite"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def fetch_args(sp):
        sp.add_argument("--days", type=int, default=int(os.environ.get("FREEROOMS_DAYS", 7)))
        sp.add_argument("--buildings", default=os.environ.get("FREEROOMS_BUILDINGS"),
                        help="comma-separated, e.g. HG,CAB (default: all)")
        sp.add_argument("--delay", type=float, default=float(os.environ.get("FREEROOMS_DELAY", 0.3)),
                        help="seconds between API requests")

    fetch_args(sub.add_parser("fetch", help="fetch rooms + allocations into the DB"))

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default=os.environ.get("FREEROOMS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("FREEROOMS_PORT", 8000)))
    s.add_argument("--refresh", type=float, default=float(os.environ.get("FREEROOMS_REFRESH_HOURS", 0)),
                   help="refetch in the background every N hours (0 = off, use cron instead)")
    fetch_args(s)

    n = sub.add_parser("now", help="print free rooms from the DB")
    n.add_argument("--at", help="local time, e.g. 2026-11-12T14:00")
    n.add_argument("--min", type=int, default=0, help="only rooms free for at least N minutes")
    n.add_argument("--building")

    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd in ("fetch", "serve"):
        kw = {"client": ApiClient(delay=a.delay), "days": a.days, "buildings": _csv(a.buildings)}
    if a.cmd == "fetch":
        conn = store.connect(a.db)
        fetch.run(conn, **kw)
    elif a.cmd == "serve":
        web.serve(a.db, a.host, a.port, a.refresh * 3600 if a.refresh else None, kw)
    elif a.cmd == "now":
        t = datetime.fromisoformat(a.at) if a.at else now_local()
        rooms = query(store.connect(a.db), t)
        free = [r for r in rooms if r["state"] == "free" and (10**6 if r["minutes"] is None else r["minutes"]) >= a.min
                and (not a.building or r["building"] == a.building)]
        free.sort(key=lambda r: -(10**6 if r["minutes"] is None else r["minutes"]))
        print(f"{len(free)} free rooms at {t:%a %d.%m. %H:%M}")
        for r in free:
            until = r["until"][11:16] if r["until"] else "end of data"
            print(f"  {r['id']:14} until {until}  ({r['minutes']} min)  {r['seats'] or '?':>4} seats  {r['type']}")
        unknown = sum(r["state"] == "unknown" for r in rooms)
        if unknown:
            print(f"({unknown} rooms with no data shown as unknown)")


if __name__ == "__main__":
    main()
