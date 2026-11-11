# free-rooms-ethz

Find an empty room to study in at ETH Zürich, right now or later today.

The ETH [Rauminfo](https://ethz.ch/staffnet/de/service/raeume-gebaeude/rauminfo.html) pages load their data from a public JSON endpoint (`https://ethz.ch/bin/ethz/roominfo?path=…`). This tool fetches the schedules of the centrally managed lecture halls, seminar rooms and computer rooms on the Zentrum and Hönggerberg campuses into SQLite (184 rooms, one request each). A small web UI then shows which rooms have nothing scheduled and for how long.

- **Rooms view:** free rooms sorted by how long they stay free, with a day timeline. Tap a room for details: today's bookings, seats, windows, sockets, whether food is allowed, a campus map, and links to the official room details, schedule and location pages.
- **Buildings view:** how many rooms are free in each building for the chosen time and duration, plus the building's opening hours.

- No login, no scraping, **no dependencies**: Python ≥ 3.10 standard library only.
- One fetch takes about 1 min (0.3 s delay between requests). The web app reads from SQLite. It only calls ETH when someone opens a room's details for the first time, and caches the result for a week.

> “Free” means *nothing is scheduled*. The room may still be locked, and short-notice bookings or cancellations may be missing. This is not an official ETH service.

## Quick start

```sh
python3 -m freerooms fetch            # ~1 min, writes data/freerooms.sqlite
python3 -m freerooms now --min 60     # CLI: rooms free for at least 1h
python3 -m freerooms serve            # UI on http://127.0.0.1:8000
```

`/?at=2026-11-12T14:00` opens the UI at a specific time, `/?view=buildings` opens the building overview, and `#room=HG%20E%201.2` opens a room's details. The JSON is available at `/api/rooms?at=…` (rooms plus opening hours per building) and `/api/roomdetail?id=…`.

Options (flags or env vars):

| Flag | Env | Default | |
| --- | --- | --- | --- |
| `--db` | `FREEROOMS_DB` | `data/freerooms.sqlite` | |
| `--days` | `FREEROOMS_DAYS` | `7` | how far ahead to fetch |
| `--buildings` | `FREEROOMS_BUILDINGS` | all | e.g. `HG,CAB,ML` |
| `--delay` | `FREEROOMS_DELAY` | `0.3` | seconds between API requests |
| `--refresh` (serve) | `FREEROOMS_REFRESH_HOURS` | `0` | background refetch interval in hours |
| `--host` / `--port` | `FREEROOMS_HOST` / `_PORT` | `127.0.0.1` / `8000` | |

## Running on a home server

With Docker:

```sh
mkdir -p data && docker compose up -d --build   # serves :8000, refetches every 2h
```

The container runs as uid 1000, so `./data` must be writable by that user. Put it behind your reverse proxy (Caddy/Traefik/nginx) if it should be reachable from outside.

Without Docker, use a systemd service running `python3 -m freerooms serve --host 0.0.0.0 --refresh 2`. Alternatively, run `serve` without `--refresh` and add a cron job for `python3 -m freerooms fetch` (e.g. `0 6-21/2 * * *`).

## How it works

```
cron / --refresh ──► fetch.py ──► ETH Rauminfo API (rooms list + per-room allocations)
                        │ normalize
                        ▼
                     SQLite  ◄── web.py (/api/rooms, UI)  ◄── browser
```

1. `GET /v2/rooms&lang=de` → filter type codes 3430 (Hörsaal), 3420 (Seminar/Kurs), 3650 (Computer) on Zentrum + Hönggerberg, **and** keep only the rooms listed in [`freerooms/rooms.txt`](freerooms/rooms.txt) (see below).
2. For each room, `GET /rooms/{building floor room}/allocations&from=…&to=…`. The id is **built from the fields** (`HG F 26.3`), not taken from the list's `id` (`HG F 026.3`), which silently returns `[]`.
3. Normalize the entries by `belegungstyp` (undocumented, inferred from observed data):

   | type | meaning | treated as |
   | --- | --- | --- |
   | 2 | lecture / exercise / meeting / event | busy |
   | 5 | construction, repair, technical reservation | busy |
   | 11 | seen on doctoral courses | busy |
   | 8 | building closed (nights, weekends; hours differ per building, e.g. ETF closes 20:00) | closed |
   | 7 | holiday closure | closed |
   | 13, or `findetStatt: 0` | cancelled | dropped (= free) |
   | anything else | | busy + warning in the log |

4. At time *t*, a room is busy or closed if an interval contains *t*. Back-to-back intervals are merged, including across midnight (`23:59` → `00:00`). Otherwise the room is free until the next interval starts. Rooms with no entries at all are shown as *unknown*. Rooms without closure entries get an "opening hours unknown" note and are listed last.
5. A building's opening hours for a day are the gap between its closure entries (from the first opening to the last closing), taken as the most common value among its rooms.
6. Room details (`GET /v2/rooms/{building floor room}&lang=en`) are fetched on demand and cached in SQLite for 7 days. Only ids that are in the `rooms` table are accepted, so the endpoint can't be used as an open proxy.

### Why only the "Rektoratsräume"

The API also lists rooms managed by individual departments (e.g. HG G 19.1). Those can be booked through other channels, and the bookings are missing from Rauminfo, so the rooms showed up as free while they were in fact reserved. The tool therefore only tracks the rooms that the Rectorate books centrally, as listed in ETH's PDFs ([Zentrum](https://ethz.ch/content/dam/ethz/associates/services/Service/raeume-und-gebaeude/rektoratsraeume/files/rektoratsraeume/rektoratsraeume-zentrum.pdf), [Hönggerberg](https://ethz.ch/content/dam/ethz/associates/services/Service/raeume-und-gebaeude/rektoratsraeume/files/rektoratsraeume/rektoratsraeume-hoenggerberg.pdf), status 04/2025). Meeting rooms are left out. When ETH updates the PDFs, update `freerooms/rooms.txt`. A fetch logs a warning if the list names rooms the API no longer knows.

### Links to ETH pages

The schedule page's "back to room details" link goes to whichever room's details page you last opened on ethz.ch, because ETH stores it in your browser. If you open a schedule straight from this tool, "back" can therefore lead to a different room. The *Room details* link avoids this. The floor plan with the room highlighted (on the *Location* page) needs an ETH login, so the UI shows ETH's public campus map instead.

All times are naive Europe/Zurich local time, as the API returns them.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

## License

[MIT](LICENSE). The room data belongs to ETH Zürich.
