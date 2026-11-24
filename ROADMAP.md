# Roadmap and data-source research

Research session of 2026-11-18. Every endpoint below was called from outside the ETH network (home connection, no login) on that day, unless marked otherwise.

## TL;DR

1. **UZH has a usable room API.** UZH's public booking calendar (3V-Rooms, `uzh.book.3vrooms.app`) returns JSON for about 200 teaching rooms on all three campuses (Zentrum, Irchel, Oerlikon), with bookings and opening hours. It needs an *anonymous* login handshake first, which can be scripted without credentials.
2. **UZH library occupancy is public.** There are 12 sensor widgets, readable as tiny HTML pages ("ca. 30% frei von 120").
3. **ETH has its own study-space feed.** `ethz.ch/bin/ethz/studyspaces.json` lists 66 study spaces (about 4,200 seats, including the ETH Library reading room), with hours, sockets, noise level and 24-hour access. `…/studyspaces/availability.json` gives live occupancy, estimated from Wi-Fi data, for 13 of them. A separate feed for the chemistry/biology library ICBP Infozentrum (HCI) gives occupancy **per floor**.
4. **Door displays (ROOMZ e-paper, 800+ rooms) are not public.** They show Outlook/Exchange room calendars, and the myRoomz app needs an ETH login. Raw Wi-Fi data per access point is not public either: only the aggregated study-space numbers above.
5. ⚠️ **Someone already built most of this.** [bquozon/ETH-room-finder](https://github.com/bquozon/ETH-room-finder), live at [rooms.polyuni.ch](https://rooms.polyuni.ch/), combines ETH and UZH rooms, ETH and UZH live occupancy, forecasts, a 3D map and "near me". It is actively developed (last commit 2026-11-16) and has no license. **We should decide how to position against it before building Phase 2+** (see [Positioning](#positioning-decision-needed)).

## Findings

### UZH rooms: 3V-Rooms booking calendar ✅

UZH's "Room Calendar" link on the [student study rooms page](https://www.campuskultur.uzh.ch/en/campusnutzung-und-bewilligungen/raeume/weitere-raeume/studentische-lernraeume.html) opens `https://uzh.book.3vrooms.app/default/Anonymous/Calendar/PortalFind`.

**Session.** The first GET redirects to an OpenID Connect flow with `acr_values=anonymous`. The identity provider immediately returns an auto-submitting form (`code`, `state`, `iss`). POST that form to `https://uzh.book.3vrooms.app/Default` and keep the cookies (`3v.rooms`, `3v.roomsC1`, `3v.roomsC2`). No credentials are involved. Without the cookies the API returns `401`. This works with stdlib `urllib` + `http.cookiejar`.

**Calendar endpoint.**

```
GET /default/api/v1.0/calendar?from=2026-11-17T23:00:00Z&to=2026-11-18T23:00:00Z&mode=Day&id=1&id=2&…&id=399
```

- `id=` repeated selects resources by numeric id. The ids are sequential: 1–399 returned 270 resources, 203 rooms (`RessourcetypId: "1"`) and 67 pieces of equipment (`"2"`, e.g. projectors). Without `id` you only get the session's default location (KOL/KO2/KOH, 55 rooms). `standortId=` alone returns no rooms.
- Each resource has a `Title` such as `KOL-F-101 (209 Plätze) **` (building-floor-room plus seat count) and a `BlockList`.
- Each block has `BeginnEffective`/`EndeEffective` in **UTC** (`Z`, unlike ETH), plus `RessourceTyp`, `Titel` (`"belegt"`, no event names) and `Kommentar` (e.g. `"10:15 - 12:00 > TN: 350"`, i.e. participants).
- `RessourceTyp` values:

  | value | meaning (inferred) | count over one week |
  | --- | --- | --- |
  | `1` | booking | 2632 |
  | `O` | outside opening hours (e.g. 00:00–07:00) | 2409 |
  | `S` | blocked (*Sperrzeit*), e.g. "Buchbar ab 18:00 Uhr" | 63 |
  | `F` | probably a holiday (*Feiertag*) — unverified | 61 |

- One week for ids 1–239 was 8.9 MB and took 21 s. One day is about 0.5 MB, so fetch day by day or week by week. **One request covers every UZH room**, versus one request per room at ETH.
- The location tree (`standortId` 1–270) goes UZH → Zentrum/Irchel/Oerlikon → building → floor. It also contains non-rooms ("Info-Aktionen", poster spots, "Apérofläche" and "Lager" (storage) resources), which need filtering by title.
- Coverage: KOL, KO2, KOH, RAA, RAI, RAK, SOC, SOD, SOE, SOF, PLD, KAB, FRE, GLT, HAH, KUM (Zentrum); Y03–Y44 (Irchel); AND, BIN, AFL (Oerlikon). These are the centrally booked teaching rooms, the UZH equivalent of our Rektoratsräume.
- Still to verify: that lectures really appear as `belegt` (compare a few rooms with the [UZH course catalogue](https://courses.uzh.ch)), and what `F` means.
- Caveats: `x-robots-tag: noindex`, undocumented, and the anonymous session presumably expires. Fetch rarely (2×/day is plenty) and cache.

### UZH libraries: live occupancy ✅

The [occupancy overview page](https://www.ub.uzh.ch/de/ub-besuchen/belegung-alle.html) embeds `https://hbzwwws04.ub.uzh.ch/occusense/occusense_progressbar_150.php?id=N`. Each widget is a tiny HTML page containing `value: 'ca. 30% frei von 120'` and `bar.animate(0.3)`. Other values are `'geschlossen'` (closed) and `'&gt; 95% frei von …'`. The numbers are rounded to 5%.

| id | location | | id | location |
| --- | --- | --- | --- | --- |
| 10 | UB Betriebswirtschaft – Lernraum | | 16 | UB Medizin Careum Lernraum |
| 15 | Digital Library Space | | 12 | UB Naturwissenschaften |
| 19 | UB Sozialwissenschaften | | 21 | UB Pflanzenwissenschaften |
| 2 | UB Lernzentrum Irchel West | | 17 | UB Psychologie |
| 23 | UB Lernzentrum Irchel Ost/Süd | | 20 | UB Veterinärmedizin |
| 11 | UB Medizin Careum Erdgeschoss | | 18 | UB Volkswirtschaft |

These mappings were checked against the individual location pages (betriebswirtschaft → 10, lernzentrum_irchel → 2 + 23, medizin_careum → 11, careum_lernraum → 16, naturwissenschaften → 12, psychologie → 17). Ids 5, 7–9, 13, 14, 22 and 24–26 also respond but appear on no page. 22 reports "> 95% frei von 500", possibly UB Recht.

The Zentralbibliothek (ZB, about 600 seats) has **no** occupancy display.

**UZH floor-plan API** (`https://ziplaene-api.uzh.ch/v1`, used by [plaene.uzh.ch](https://plaene.uzh.ch)):
- `/map/buildings` lists 231 buildings with lat/lon and POI types (`LERNRAUM`, `SELBSTSTUDIUM`, `UNIVERSITAETSBIBLIOTHEK`, `HOERSAAL`, …). This is where to get **building coordinates** for the location feature.
- `/map/buildings/{code}` returns study places (`arbeitsplaetze`), libraries (`bibliotheken`) and POIs with room numbers.
- Also: `/map/groups`, `/map/poiAussen`, `/map/search?query=`.
- There are `belegung` and `floorRoomAvailabilities` fields, but they are **empty for all buildings today**. They may get filled later, so check again occasionally.

UZH is measuring room usage (Wi-Fi + CO2) in Irchel lecture halls Dec 2025 – Oct 2026 ([UZH News](https://news.uzh.ch/en/articles/news/2025/measuring-space-utilization.html)). This is for planning; nothing says the data will be published.

### ETH study spaces and library occupancy ✅

| Endpoint | Content |
| --- | --- |
| `https://ethz.ch/bin/ethz/studyspaces.json` | 66 study spaces (46 Zentrum, 16 Hönggerberg, 4 Basel/BSS) with seats, hours (incl. meal times when a mensa is not a study space), 15 with 24-hour card access, features (sockets many/few, quiet/noisy, windows, wheelchair), image, location URL. Includes ETH Library HG H 30.1 (150 seats), Baubibliothek, GESS, Physik, Grüne Bibliothek, mensas outside meal times. |
| `https://ethz.ch/bin/ethz/studyspaces/availability.json` | Per space: `status` (`green`/`yellow`/`red`/`unknown`), and for 13 of 112 entries `occupancy`, `maxPeople`, `occupancyPercentage`, `trend`, `timestamp`, `elionaAssetId` (data from the Eliona IoT platform). |
| `https://iz-websrv01.ethz.ch:3000/api/visitors` | ICBP Infozentrum (HCI G 5.1): `{"overall":118.67,"overall_max":170,"G":50.24,"G_max":68,"H":41.59,…,"trend":-1,"library_is_closed":false}`. Per floor, from Wi-Fi plus an entrance counter. Used by [infoz01.ethz.ch](https://infoz01.ethz.ch). |

How ETH computes the numbers ([data source page](https://ethz.ch/students/en/campus/workspaces-for-students/data-source.html)): aggregated network usage calibrated against reference values, shown in 20% steps, updated every 5 minutes. ETH plans to cover more spaces over time. The [EduApp → Study Environments](https://eduapp.ethz.ch/workspaces) page needs an ETH login, but it is very likely built on the same JSON.

### ETH Wi-Fi, sensors and the door displays ❌ (not public)

- **ROOMZ e-paper door signs** ([ETH page](https://ethz.ch/en/the-eth-zurich/organisation/departments/engineering-and-systems/raumbelegung/digital-room-signs.html)): more than 800 displays, run by ETH Engineering & Systems (E&S). They show **Outlook/Exchange room calendars**, updated hourly. The **myRoomz** app shows free/occupied rooms on floor plans (15-minute delay), but requires an ETH login and only shows rooms you have access to. So the "data for all rooms" you suspected exists, but it lives in Exchange.
- **Sensors** ([space utilisation page](https://ethz.ch/en/the-eth-zurich/organisation/departments/engineering-and-systems/raumbelegung/raumnutzung.html)):
  - Wi-Fi connection counts per access point (not mappable to rooms).
  - Motion sensors in meeting rooms (OCT building pilot).
  - People-flow counters (e.g. Audimax).
  - The 7 large HG lecture halls are colour-coded by utilisation internally.
  - There is an "interface for ETH researchers" (living-lab projects). **That is the realistic official route:** ask E&S.
- **Wi-Fi from the client:** browsers cannot read the BSSID. ETH-room-finder tried IP/gateway fingerprinting (`site/wifi-test.js`, `scripts/wifi_probe.sh`), with no usable result so far. Only a native Android app could read BSSIDs, and iOS needs special entitlements.
- **Department rooms via Exchange:** an ETH user could query room mailboxes' free/busy times with Microsoft Graph (`getSchedule`), using their own login. This would cover the department rooms we currently drop. But it needs an app registration that the ETH tenant may block (admin consent), and it is a grey zone. Only do this with E&S's blessing.

### Other projects

| Project | What it does | Notes |
| --- | --- | --- |
| [bquozon/ETH-room-finder](https://github.com/bquozon/ETH-room-finder) → [rooms.polyuni.ch](https://rooms.polyuni.ch/) / [eth-rooms.bquozon.ch](https://eth-rooms.bquozon.ch/) | ETH + UZH free rooms (7 days), ETH + UZH live study-space occupancy, popular-times forecast, 3D MapLibre map, "near me" via browser geolocation (computed client-side), privacy-friendly stats | Static site + cron (bash/python/jq), VPS + Caddy. Uses only Rektorat rooms (orgunit 09188, room group 114), like us, plus a hand-picked `extra_rooms.txt` (CAB H 56). Treats type 13 / `findetStatt: 0` as booked. No ICBP per-floor feed. **No license** → we cannot reuse code. Very active, 3 stars. |
| [damcha02/open-room-ethz](https://github.com/damcha02/open-room-ethz) | ETH free-room PWA, favourites, filters, offline | Cloudflare Worker + Durable Object, daily request budget, honours 429/Retry-After (so ETH's Rauminfo API has a rate-limit header!). 129-room seed list. No license. |
| [nielstron/freiraumETHZ](https://github.com/nielstron/freiraumETHZ) (2021) | Closest free room, Telegram bot, geocoding | Old HTML scraping; idea source only. |
| *roominfo_checker* / "Sick Room Finder" by Oliver Saravanja | ETH Rauminfo scraper (MIT) | Credited by ETH-room-finder; couldn't find the repo. |
| EPFL: [occupancy-epfl](https://github.com/antoninfaure/occupancy-epfl), repfl, NotEnoughRoomBot | Free rooms from edu.epfl.ch schedules, Telegram bot | Same pattern: scrape the schedule → "not booked". |
| [UNSW freerooms](https://github.com/devsoc-unsw/freerooms), [KIT Seatfinder](https://seatfinder.bibliothek.kit.edu/), UvA crowding indicators, UCL | Mature student/official tools | KIT/UvA use Wi-Fi/sensor counts with trend arrows, the same model as ETH/UZH here. |
| ZHAW project *Lernumgebung Zürich* | Cross-university platform to make Zurich learning rooms visible (ZHAW, UZH, ZHdK, …) | Mentioned in search results; project page 404s now. Worth watching or contacting. |

### Lessons from the other projects

- **Type 13 / `findetStatt: 0` really means "not held".** On 2026-11-19, HCI F 8 had two weekly series of type 13, all semester: 052-0585-26 *Unveiling Habitats* (14:00–18:00) and 052-0845-26 *Spezialfragen Architekturgeschichte* (14:00–16:00). The course catalogue says "Findet dieses Semester nicht statt" for both. ETH's own allocation page drops these entries too (`roominformation.js`: `addAllocation` keeps entries only if `veranstaltung == null || findetStatt === 1`). So we are right to show the room as free. rooms.polyuni.ch and rooms.saravanja.ch show it as reserved, and open-room-ethz as "blocked". **Idea:** show cancelled entries greyed out in the room timeline ("cancelled: Unveiling Habitats"), so users understand why we disagree with other tools.
- **open-room-ethz:**
  - *Strict parsing*: one malformed entry fails the whole response, so a skipped booking can never make a room look free.
  - *Rate limiting*: honours `x-ratelimit-remaining` (slows down at ≤ 5) and `429` + `Retry-After` (60 s default cooldown).
  - *Honest freshness*: every response carries the age of the original ETH answer, and the UI marks stale data.
  - *No guessing*: room existence is checked via room details, not assumed.
- **ETH-room-finder:**
  - Visitors only ever read cached static JSON, so ETH sees about 2 requests a minute no matter how many users there are.
  - It alerts (ntfy) when live data is more than 15 minutes old.
  - It stores occupancy history for "popular times".
  - It keeps a `special_days.txt` for holidays.
- **Sick Room Finder** ([ollie-sara/roominfo_checker](https://github.com/ollie-sara/roominfo_checker), MIT):
  - Rektorat rooms only, like us.
  - Its scraper runs in **GitHub Actions twice a day** and commits `docs/data/data.json`, which a **GitHub Pages** site renders. This is proof that our idea works on Pages for the schedule part.

### Hosting on GitHub Pages?

It's possible, but only as a static site:
- A scheduled Action runs `fetch`, exports JSON, and deploys to Pages.
- The UI then reads static JSON instead of `/api/*`.
- Room details are pre-fetched for all ~190 rooms, weekly.

Limits:
- Scheduled workflows can be delayed by 10–30+ minutes, which makes 5-minute live occupancy unreliable.
- Schedules are disabled after 60 days without repo activity.
- Each run uses Actions minutes (free for public repos).
- Requests come from GitHub's shared IPs.

**Fine for room schedules (every 1–2 h); poor for live occupancy.** The browser can't fetch the live sources directly: none of them send CORS headers (checked 2026-11-19 for ETH availability, ICBP and the UZH widgets). So live occupancy needs a server: the home server, or a small free worker/proxy like open-room-ethz's Cloudflare Worker. Possible split: Pages for the static app + schedule JSON, plus a tiny proxy for live data.

## Positioning (decision needed)

ETH-room-finder already covers most of the feature list. Options:

1. **Differentiate (recommended).** Our edge:
   - *Correctness*: only rooms whose bookings are complete (Rektoratsräume + UZH central rooms), with explicit "unknown" states, and cancelled (type 13) entries shown as free. (Correction 2026-11-19: they also use only Rektorat rooms, so the department-room point does not apply to them.)
   - *Zero-dependency, self-hostable*: one Python process, SQLite, Docker.
   - *A clean JSON API* other tools (bots, widgets) can build on.
   - Features they lack: ICBP per-floor occupancy, filters on room attributes (food allowed, windows, sockets, both universities), "free from X for Y hours" planning, notifications/bot.
2. **Collaborate.** Open an issue there about type 13 and the ICBP feed. That needs them to add a license before any code flows either way.
3. **Keep it as a personal/learning project** and not worry about overlap.

Either way, it's worth telling them about type 13, since it affects their users (issue #5). Note that department rooms are not simply missing from Rauminfo: HG G 19.1 has bookings there, but they are incomplete, because departments also book through other channels.

## Roadmap

### Phase 1: Study places with live occupancy (ETH + UZH libraries) — small, high value

- New concept **place** (study space / library) next to **room**: `places(id, uni, building, name, seats, hours_json, features_json, lat, lon, source, url)` and `occupancy(place_id, ts, pct, count, max, trend, closed)`.
- Fetchers, separate from the 2-hourly room fetch, each run about every 5 minutes:
  - ETH: `studyspaces.json` hourly; `availability.json` every 5 min.
  - ICBP: `/api/visitors`, per floor.
  - UZH UB: 12 widgets; parse `value:` with a regex, treat `geschlossen` as closed.
- UI: a **"Study spaces" tab** with a traffic light, "% free of N", trend arrow, opening hours today, and the features as filter chips (quiet, sockets, 24h).
- Keep **every sample** in SQLite (about 100 places × 288/day ≈ tiny). This is the basis for Phase 4.
- Tests: parsers for each source against saved fixtures, in the existing `tests/` style.

### Phase 2: UZH rooms (3V-Rooms) — medium

- `uzh.py`:
  - Anonymous OIDC handshake (GET → parse hidden form → POST → cookies).
  - Re-login on 401.
  - One request per day for the next 7 days.
- Room list: resources with `RessourcetypId == "1"`. Parse `BLDG-FLOOR-ROOM (N Plätze)`. Drop `Apérofläche`, `Lager`, `Küche`, `Lichthof`, `Foyer`, `Vorlage` and "EV" (events) after checking. Keep a `uzh_rooms.txt` allowlist like `rooms.txt`.
- Normalise the block types: `1` → busy, `O` → closed, `S` → busy (blocked), `F` → closed (verify). Convert UTC → Europe/Zurich.
- Reuse `availability.py` unchanged; add a `uni` column and campus filter (ETH Zentrum / ETH Hönggerberg / UZH Zentrum / Irchel / Oerlikon).
- Validation: compare 5–10 rooms against the UZH course catalogue for one week before shipping.

### Phase 3: Location, "nearest free place" — medium

**Status (2026-11-23):** built. Buildings table + `python -m freerooms buildings` (UZH floor-plan API, OSM/Overpass for ETH), the **Near** tab (one-tap answer), "Nearest first" in the other tabs, auto campus within 500 m, location only in the browser. ETH coordinates fetched 2026-11-24: 8 buildings from OSM names (no ETH outline carries its code as `ref`), the other 21 from ETH's building map (mapsearch.ethz.ch), which agrees with OSM within 5–35 m. Open: entrances, floor preference and the .ics mode are still "later".

- Building coordinates:
  - UZH: `ziplaene-api.uzh.ch/v1/map/buildings` (lat/lon).
  - ETH: OSM building outlines (`ref=*` on ETH buildings) or the Google Maps links on `ethz.ch/en/utils/location.html`.
  - Store them in a `buildings` table, refreshed manually or monthly.
- Client-side `navigator.geolocation`, so the location never leaves the device. Haversine plus a walking-speed estimate. Auto-select the campus when the user is within ~500 m of it.
- **One-tap answer:** "Nearest room or study space that is free for ≥ 60 min", combining rooms (schedule) and places (live occupancy < 80%).
- Later: entrance points per building (HG has many), floor preference, and a "same building as my next lecture" mode (import from myStudies .ics).

### Phase 4: History, forecasts, reliability — medium

- "Usually busy at this time" per place (median of the same weekday and time slot over the last N weeks), shown as Google-Maps-style popular times.
- For rooms: flag rooms that often get short-notice bookings (diff successive fetches), and rooms that are often locked (from user feedback, Phase 5).
- Prediction: "the Lesesaal usually fills up by 10:00 in the exam session".

### Phase 5: Long-term

- **Feedback button**: "room was locked / occupied / great". Store anonymously and use it to down-rank rooms.
- **Telegram bot / push**: "tell me when a seat in HG H 30.1 frees up" or "a room near me at 14:00".
- **PWA** (installable, offline last-known data), **i18n** (de/en).
- **Official data access** (send emails):
  - ETH E&S for Wi-Fi/sensor and room-calendar access (the researcher interface).
  - UZH Hörsaaldisposition (`hoersaaldispo@del.uzh.ch`) for API permission.
  - The ZB, about whether it has any occupancy data.
- **Department rooms** via Exchange free/busy, only with E&S approval.
- More institutions: ZHAW, ZHdK, PHZH, or Lernumgebung Zürich if that platform launches.

## Housekeeping and risks

- Every source is **undocumented** and can change without notice. Add a health check per source (last success, row count) and show "data stale" in the UI.
- Be polite: cache, use a low frequency and an identifiable User-Agent. ETH's Rauminfo API sends `x-ratelimit-remaining: 20` (a token bucket that refills within about a second). Our `api.py` only retries generically after 2 s and 4 s. **Small fix:** read that header and slow down when it gets low, and honour `429` + `Retry-After`.
- The project is MIT. Don't copy code from the unlicensed projects above; ideas and endpoints are fine.
- Open questions from the [first research note](Freie%20Räume%20ETH%20–%20API-Recherche.md) that are still open: ask ETH whether the API may officially be used.
