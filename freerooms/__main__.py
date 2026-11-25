"""CLI: python -m freerooms {fetch,serve,live,now,buildings}"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from . import api, fetch, geo, live, reliability, store, web
from .api import ApiClient
from .http import Http
from .availability import now_local, query


def _csv(s: str | None) -> tuple[str, ...] | None:
    return tuple(x.strip() for x in s.split(",") if x.strip()) if s else None


def now_lines(conn, t: datetime, min_minutes: int = 0, building: str | None = None) -> list[str]:
    """The output of `now`: free rooms at local time t, longest free first. Rooms often booked at
    short notice (reliability.py) are marked."""
    rooms = reliability.annotate(query(conn, t), reliability.scores(conn))
    free = [r for r in rooms if r["state"] == "free" and (10**6 if r["minutes"] is None else r["minutes"]) >= min_minutes
            and (not building or r["building"] == building)]
    free.sort(key=lambda r: -(10**6 if r["minutes"] is None else r["minutes"]))
    out = [f"{len(free)} free rooms at {t:%a %d.%m. %H:%M}"]
    for r in free:
        until = r["until"][11:16] if r["until"] else "end of data"
        flag = "  (often booked at short notice)" if r["unreliable"] else ""
        out.append(f"  {r['id']:14} until {until}  ({r['minutes']} min)  {r['seats'] or '?':>4} seats  {r['type']}{flag}")
    unknown = sum(r["state"] == "unknown" for r in rooms)
    if unknown:
        out.append(f"({unknown} rooms with no data shown as unknown)")
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="freerooms", description=__doc__)
    p.add_argument("--db", default=os.environ.get("FREEROOMS_DB", "data/freerooms.sqlite"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def fetch_args(sp):
        sp.add_argument("--unis", default=os.environ.get("FREEROOMS_UNIS"),
                        help="comma-separated universities whose rooms to fetch: ETH,UZH (default: both)")
        sp.add_argument("--days", type=int, default=int(os.environ.get("FREEROOMS_DAYS", 7)))
        sp.add_argument("--buildings", default=os.environ.get("FREEROOMS_BUILDINGS"),
                        help="ETH only: comma-separated, e.g. HG,CAB (default: all)")
        sp.add_argument("--delay", type=float, default=float(os.environ.get("FREEROOMS_DELAY", 0.3)),
                        help="seconds between ETH API requests (UZH: fixed 1 s)")
        sp.add_argument("--ratelimit-low", type=int, default=int(os.environ.get("FREEROOMS_RATELIMIT_LOW", api.LOW_REMAINING)),
                        help="slow down when x-ratelimit-remaining is at or below this")
        sp.add_argument("--ratelimit-pause", type=float,
                        default=float(os.environ.get("FREEROOMS_RATELIMIT_PAUSE", api.LOW_PAUSE)),
                        help="seconds between requests while the rate limit is low")
        sp.add_argument("--retry-after-default", type=float,
                        default=float(os.environ.get("FREEROOMS_RETRY_AFTER_DEFAULT", api.RETRY_AFTER_DEFAULT)),
                        help="seconds to wait after a 429 without a usable Retry-After")
        sp.add_argument("--retry-after-max", type=float,
                        default=float(os.environ.get("FREEROOMS_RETRY_AFTER_MAX", api.RETRY_AFTER_MAX)),
                        help="never wait longer than this after a 429")
        sp.add_argument("--stale-hours", type=float, default=float(os.environ.get("FREEROOMS_STALE_HOURS", 4)),
                        help="flag data as stale in the UI after N hours (serve --refresh raises it to 1.5x the interval)")

    fetch_args(sub.add_parser("fetch", help="fetch rooms + allocations into the DB"))

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default=os.environ.get("FREEROOMS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("FREEROOMS_PORT", 8000)))
    s.add_argument("--refresh", type=float, default=float(os.environ.get("FREEROOMS_REFRESH_HOURS", 0)),
                   help="refetch in the background every N hours (0 = off, use cron instead)")
    fetch_args(s)
    s.add_argument("--live", action="store_true", default=os.environ.get("FREEROOMS_LIVE", "") not in ("", "0"),
                   help="also run the live occupancy sources (study spaces, libraries) in the background")

    lv = sub.add_parser("live", help="fetch live occupancy (study spaces, libraries)")
    lv.add_argument("--once", action="store_true", help="run the sources that are due once and exit (for cron)")
    lv.add_argument("--force", action="store_true", help="with --once: run every source, due or not")

    n = sub.add_parser("now", help="print free rooms from the DB")
    n.add_argument("--at", help="local time, e.g. 2026-11-12T14:00")
    n.add_argument("--min", type=int, default=0, help="only rooms free for at least N minutes")
    n.add_argument("--building")

    bl = sub.add_parser("buildings", help="refresh building coordinates (freerooms/buildings.json) from UZH, OpenStreetMap and ETH's building map")
    bl.add_argument("--unis", default="ETH,UZH", help="comma-separated: ETH (OpenStreetMap), UZH (UZH floor-plan API)")
    bl.add_argument("--out", default=str(geo.BUNDLED), help="the buildings file to update (default: the one shipped with the code)")

    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd in ("fetch", "serve"):
        try:
            unis = fetch.parse_unis(a.unis)
        except ValueError as e:
            p.error(str(e))
        client = ApiClient(delay=a.delay, low_remaining=a.ratelimit_low, low_pause=a.ratelimit_pause,
                           retry_after_default=a.retry_after_default, retry_after_max=a.retry_after_max)
        kw = {"client": client, "days": a.days, "buildings": _csv(a.buildings),
              "max_age": a.stale_hours * 3600}
        uzh_kw = {"days": a.days, "max_age": a.stale_hours * 3600}
    if a.cmd == "fetch":
        conn = store.connect(a.db)
        res = fetch.run_all(conn, unis, eth=kw, uzh=uzh_kw)
        for uni, stats in res.items():
            print(f"{uni}: {stats if stats is not None else 'FAILED (see log)'}")
        raise SystemExit(1 if any(s is None for s in res.values()) else 0)
    elif a.cmd == "serve":
        web.serve(a.db, a.host, a.port, a.refresh * 3600 if a.refresh else None, kw, live_on=a.live,
                  unis=unis, uzh_kwargs=uzh_kw)
    elif a.cmd == "live":
        if a.once:
            res = live.run_due(store.connect(a.db), live.Http(), live.discover(), force=a.force)
            for key, counts in res.items():
                print(f"{key}: {counts if counts is not None else 'FAILED (see log)'}")
            raise SystemExit(1 if any(c is None for c in res.values()) else 0)
        live.loop(a.db)
    elif a.cmd == "buildings":
        try:
            unis = fetch.parse_unis(a.unis)
        except ValueError as e:
            p.error(str(e))
        res = geo.refresh(Http(timeout=90), unis, store.connect(a.db), Path(a.out))
        for uni, n in res.items():
            print(f"{uni}: {n if isinstance(n, str) else f'{n} buildings'}")
        print(f"written to {a.out}" if any(isinstance(n, int) for n in res.values()) else "nothing written")
        raise SystemExit(1 if any(isinstance(n, str) for n in res.values()) else 0)
    elif a.cmd == "now":
        t = datetime.fromisoformat(a.at) if a.at else now_local()
        print("\n".join(now_lines(store.connect(a.db), t, a.min, a.building)))


if __name__ == "__main__":
    main()
