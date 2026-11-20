# free-rooms-ethz

Find an empty room to study in at ETH Zürich and the University of Zurich (UZH), right now or later today.

The ETH [Rauminfo](https://ethz.ch/staffnet/de/service/raeume-gebaeude/rauminfo.html) pages load their data from a public JSON endpoint (`https://ethz.ch/bin/ethz/roominfo?path=…`). This tool fetches the schedules of the centrally managed lecture halls, seminar rooms and computer rooms on the Zentrum and Hönggerberg campuses into SQLite (184 rooms, one request each). UZH's teaching rooms (156 on Zentrum, Irchel and Oerlikon) come from UZH's public room calendar, see [UZH rooms](#uzh-rooms). A small web UI then shows which rooms have nothing scheduled and for how long.

- **Rooms view:** free rooms sorted by how long they stay free, with a day timeline. Filter by university (ETH / UZH) and campus (Zentrum, Hönggerberg, Irchel, Oerlikon, …): ETH Zentrum and UZH Zentrum are next door, so they share one “Zentrum” chip, and the university chips narrow it down. Room names are shown the way each university writes them (`HG E 5`, `KOL-F-101`). Tap a room for details: today's bookings, and for ETH rooms seats, windows, sockets, whether food is allowed, a campus map and links to ETH's room details, schedule and location pages; for UZH rooms links to the UZH map with the building's floor plans (`plaene.uzh.ch/?buildingCode=KOL`), the public 3V-Rooms calendar and the building's Uniability page.
- **Buildings view:** how many rooms are free in each building for the chosen time and duration, plus the building's opening hours. Same university and campus filter as the Rooms view.
- **Study spaces view** (`/?view=spaces`, needs `serve --live`): ETH study spaces and ETH/UZH libraries with a traffic light, “~X% free of N”, a trend arrow and today's opening hours (meal times in mensas count as closed for studying). Library floors are listed under their library. You can filter by university, campus and features (quiet, sockets, 24 h, windows, more under “More…”). Places without a live number say “no live data”. A number older than 15 min is greyed out and shows its age. The list refreshes every minute while it's on screen.

The university and campus choice is shared by all three views and remembered (default: Zentrum). UZH's Botanischer Garten library counts as Zentrum. A button at the bottom of the page switches to colour-blind safe colours (blue and orange instead of green and red), also remembered.

- No login, no scraping, **no dependencies**: Python ≥ 3.10 standard library only.
- One fetch takes about 1 min (0.3 s delay between requests; it slows down when the API reports few requests left and honours `Retry-After` on HTTP 429). The web app reads from SQLite. It only calls ETH when someone opens a room's details for the first time, and caches the result for a week.

> “Free” means *nothing is scheduled*. The room may still be locked, and short-notice bookings or cancellations may be missing. This is not an official ETH service.

## Quick start

```sh
python3 -m freerooms fetch            # ETH ~1 min + UZH ~40 s, writes data/freerooms.sqlite
python3 -m freerooms now --min 60     # CLI: rooms free for at least 1h
python3 -m freerooms serve            # UI on http://127.0.0.1:8000
python3 -m freerooms serve --live     # ... plus live study-space occupancy every 5 min
```

`/?at=2026-11-12T14:00` opens the UI at a specific time, `/?view=buildings` opens the building overview, and `#room=HG%20E%201.2` or `#room=KOL-F-101` opens a room's details. The JSON is available at `/api/rooms?at=…` (rooms with `uni` and `campus`, plus opening hours per building) and `/api/roomdetail?id=…` (ETH rooms only; 404 for UZH rooms, ETH has no details for them). `/api/places` lists the study places with their latest occupancy sample. Tapping a study place opens its details (`/?view=spaces#place=eth_studyspaces:HGH30.1`): today's occupancy curve, floors, the week's opening hours, notices, all features and links; `/api/place?id=…&date=YYYY-MM-DD` returns that place and its floors with the day's samples (Zurich date, default today).

`/api/health` reports data freshness per source (`eth_rooms`, `uzh_rooms` when UZH is enabled, and the live sources with `--live`): last attempt, last success, last error, row counts, age and whether it is stale. `status` is `ok` (HTTP 200) or `stale` (HTTP 503, so a plain uptime monitor can alert on it). The UI shows "updated X min ago" and a banner when data is stale. `/healthz` stays a plain liveness check.

Options (flags or env vars):

| Flag | Env | Default | |
| --- | --- | --- | --- |
| `--db` | `FREEROOMS_DB` | `data/freerooms.sqlite` | |
| `--unis` | `FREEROOMS_UNIS` | `ETH,UZH` | whose rooms to fetch (and list in `/api/health`) |
| `--days` | `FREEROOMS_DAYS` | `7` | how far ahead to fetch |
| `--buildings` | `FREEROOMS_BUILDINGS` | all | ETH only, e.g. `HG,CAB,ML` |
| `--delay` | `FREEROOMS_DELAY` | `0.3` | seconds between ETH API requests (UZH: fixed 1 s) |
| `--ratelimit-low` | `FREEROOMS_RATELIMIT_LOW` | `5` | slow down when `x-ratelimit-remaining` is at or below this |
| `--ratelimit-pause` | `FREEROOMS_RATELIMIT_PAUSE` | `1` | seconds between requests while the limit is low |
| `--retry-after-default` | `FREEROOMS_RETRY_AFTER_DEFAULT` | `60` | wait after a `429` without a usable `Retry-After` |
| `--retry-after-max` | `FREEROOMS_RETRY_AFTER_MAX` | `300` | never wait longer than this after a `429` |
| `--live` (serve) | `FREEROOMS_LIVE` | off | also run the live occupancy sources in the background |
| `--refresh` (serve) | `FREEROOMS_REFRESH_HOURS` | `0` | background refetch interval in hours (UZH: at least 6 h) |
| `--stale-hours` | `FREEROOMS_STALE_HOURS` | `4` | mark data stale after N hours (with `--refresh`, at least 1.5× the interval) |
| `--host` / `--port` | `FREEROOMS_HOST` / `_PORT` | `127.0.0.1` / `8000` | |

## Running on a home server

With Docker:

```sh
mkdir -p data && docker compose up -d --build   # serves :8000, refetches every 2h
```

The container runs as uid 1000, so `./data` must be writable by that user. Put it behind your reverse proxy (Caddy/Traefik/nginx) if it should be reachable from outside.

Without Docker, use a systemd service running `python3 -m freerooms serve --host 0.0.0.0 --refresh 2 --live`. Alternatively, run `serve` without `--refresh`/`--live` and use cron: `python3 -m freerooms fetch --unis ETH` for the ETH rooms (e.g. `0 6-21/2 * * *`), `python3 -m freerooms fetch --unis UZH` for UZH a few times a day (e.g. `30 5,11,17 * * *`) and `python3 -m freerooms live --once` for live occupancy (`*/5 * * * *`; each source still only runs when it is due, e.g. the ETH study-space list hourly).

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
   | 13, or `findetStatt: 0` | not held ("Findet dieses Semester nicht statt" in the course catalogue, verified via VVZ; ETH's own allocation page hides these too) | free; kept as *cancelled* and shown struck through in the room timeline |
   | anything else | | busy + warning in the log |

4. At time *t*, a room is busy or closed if an interval contains *t*. Back-to-back intervals are merged, including across midnight (`23:59` → `00:00`). Otherwise the room is free until the next interval starts. Rooms with no entries at all, or with any malformed entry (missing/invalid `date_from`/`date_to`, `date_to` before `date_from`, unexpected shape), are shown as *unknown* rather than free, and a warning is logged. Rooms without closure entries get an "opening hours unknown" note and are listed last.
5. A building's opening hours for a day are the gap between its closure entries (from the first opening to the last closing), taken as the most common value among its rooms.
6. Room details (`GET /v2/rooms/{building floor room}&lang=en`) are fetched on demand and cached in SQLite for 7 days. Only ids that are in the `rooms` table are accepted, so the endpoint can't be used as an open proxy.

### Why only the "Rektoratsräume"

The API also lists rooms managed by individual departments (e.g. HG G 19.1). Those can be booked through other channels, and the bookings are missing from Rauminfo, so the rooms showed up as free while they were in fact reserved. The tool therefore only tracks the rooms that the Rectorate books centrally, as listed in ETH's PDFs ([Zentrum](https://ethz.ch/content/dam/ethz/associates/services/Service/raeume-und-gebaeude/rektoratsraeume/files/rektoratsraeume/rektoratsraeume-zentrum.pdf), [Hönggerberg](https://ethz.ch/content/dam/ethz/associates/services/Service/raeume-und-gebaeude/rektoratsraeume/files/rektoratsraeume/rektoratsraeume-hoenggerberg.pdf), status 04/2025). Meeting rooms are left out. When ETH updates the PDFs, update `freerooms/rooms.txt`. A fetch logs a warning if the list names rooms the API no longer knows.

### Links to ETH pages

The schedule page's "back to room details" link goes to whichever room's details page you last opened on ethz.ch, because ETH stores it in your browser. If you open a schedule straight from this tool, "back" can therefore lead to a different room. The *Room details* link avoids this. The floor plan with the room highlighted (on the *Location* page) needs an ETH login, so the UI shows ETH's public campus map instead.

All times are naive Europe/Zurich local time, as the API returns them.

### UZH rooms

`uzh.py` reads UZH's public room calendar ([3V-Rooms](https://uzh.book.3vrooms.app/default/Anonymous/Calendar/PortalFind), linked from UZH's student study rooms page). It is **undocumented** and marked `noindex`, so we keep it gentle: one run is 10 requests (about 40 s, 1 s apart), and `serve --refresh` runs it at most every 6 hours.

1. **Anonymous session:** the portal page redirects to an OpenID Connect login with `acr_values=anonymous`. The identity provider answers with a form that submits itself (`code`, `state`, `iss`); posting it back gives the session cookies. No credentials are involved. When the session expires (HTTP 401, or the login page instead of JSON), the client logs in once more.
2. **Rooms and campus:** UZH's saved calendar views "Zentrum", "Irchel" and "Oerlikon" list the centrally booked rooms per campus. The first day is fetched per view, which gives the room list and each room's campus; the other days in one request for all rooms. Titles such as `KOL-F-101 (209 Plätze) **` give the id (`KOL-F-101`, UZH's spelling), building, floor, room and seats. Bookable things that aren't rooms to study in (Apérofläche, Lager, Foyer, Lichthof, labs, combinations like `AND-3-02/06` whose parts are listed, …) are left out, and so is KOL-G-201 Aula (only bookable on request, so it would always look busy). "EV" (event) rooms are left out unless the course catalogue showed regular teaching in them (KO2-F-151/152/173, KOL-G-210/212/217, KOL-H-309); [`freerooms/uzh_rooms.txt`](freerooms/uzh_rooms.txt) lists the rooms we keep and why others are missing. A fetch warns about new or vanished rooms.
3. **Blocks** (`RessourceTyp`, undocumented, inferred):

   | type | meaning | treated as |
   | --- | --- | --- |
   | 1 | booking (only says "belegt") | busy |
   | O | outside opening hours (nights, weekends) | closed |
   | S | blocked (*Sperrzeit*): "Buchbar ab 18:00 Uhr.", "Informationen über die Verfügbarkeit auf Anfrage." | busy, with that text; closed if the text says "geschlossen" (e.g. "Semesterferien GLT geschlossen") |
   | F | closure: holidays ("Karfreitag UZH geschlossen", "Weihnachtsferien") and early closing ("Hauptgebäude ab 13:30 geschlossen") | closed, with that text |
   | anything else | | busy + warning in the log |

   Validation is as strict as for ETH: a room with a malformed block is *unknown*, never free, and a room without any block in the whole window is unknown too (every room normally has nightly `O` closures; the three KUM rooms have none, so their opening hours show as unknown). If a day fails to load, the data stops before that day instead of having a hole.
4. **Times:** the API takes the query window in UTC, but its answers are Zurich **wall-clock** time with a misleading `Z` (bookings at `10:15Z` say "10:15 - 12:00" in their comment and stay at `10:15Z` across the switch to summer time). We store them as they are. Multi-day blocks are cut at midnight.

Caveats: undocumented and may change any time; the session is anonymous but still a session; rooms whose `F`/`S` blocks are wrong would show wrong states. Checked against the [UZH course catalogue](https://courses.uzh.ch) for one teaching week: 1,916 of 1,917 course meetings in our rooms are booked in 3V at exactly the same time, see [docs/uzh-validation.md](docs/uzh-validation.md).

### Live occupancy (study spaces and libraries)

Separate from the room schedules, `live.py` runs the sources in [`freerooms/sources/`](freerooms/sources/), each on its own interval (live numbers every 5 min, metadata hourly). Every module there that defines `SOURCES` is picked up automatically. A failing source is logged and shown in `/api/health`; it never blocks the others. Places go into the `places` table and **every sample is kept** in `occupancy` (basis for "popular times" later; about 100 places × 288 samples a day stays small; a sensor that keeps reporting "no data" is stored once, not every 5 minutes). `occupancy.pct` is always the share *occupied*, even for sources that report "% free". `/api/places` returns every place with its latest sample. Sources that only report a number get a shared traffic light (`places.status_for`): green below 70 % occupied, yellow below 90 %, red above. The UI shows exact percentages for sources that count people (ETH, ICBP) and "~" with 5 % steps for UZH, which only reports rounded values.

Sources:

- **ETH study spaces** (`eth_studyspaces`, hourly): the 66 spaces from `ethz.ch/bin/ethz/studyspaces.json` with seats, opening hours (incl. mensa meal times when there's no studying), features and 24-hour access. **ETH live occupancy** (`eth_availability`, every 5 min): `…/studyspaces/availability.json`, with ETH's own traffic light; people counts for about a dozen spaces. Three seminar rooms with a people counter (CHN F 46, HIL B 21, ML H 37.1) are listed too, with a note to check their bookings in the Rooms view.
- **ICBP Infozentrum** (`icbp`, every 5 min): the chemistry/biology library in HCI, as a whole and per floor (G, H, J), from the feed behind [infoz01.ethz.ch](https://infoz01.ethz.ch). Its numbers are attached to ETH's study space HCI G 5.1 (whose own sensor is silent), with the floors nested under it.
- **UZH libraries** (`uzh_ub`): The `uzh_ub` live source reads the 12 occupancy widgets from the [UB overview page](https://www.ub.uzh.ch/de/ub-besuchen/belegung-alle.html) every 5 minutes, one request every 0.5 s. A label like "ca. 30% frei von 120" becomes 70 % occupied of 120 seats. "> 95% frei" is stored as 3 % occupied (like the widget's own bar), and "geschlossen" as closed. Widget 22 ("von 500") is listed as UB Rechtswissenschaft with an *unconfirmed* note: UZH doesn't link it anywhere, but the UB Recht page says "rund 500 Arbeitsplätze". Names, buildings and coordinates are a static table in [`freerooms/sources/uzh_ub.py`](freerooms/sources/uzh_ub.py).

## Development

```sh
python3 -m unittest discover -s tests -t .
```

## License

[MIT](LICENSE). The room data belongs to ETH Zürich and the University of Zurich.
