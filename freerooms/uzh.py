"""UZH teaching rooms from the public 3V-Rooms booking calendar (uzh.book.3vrooms.app).

Undocumented and marked `noindex`; we fetch rarely (one run = 2 login requests + 1 filter list
+ 3 campus requests for the first day + 1 request per further day) with a pause in between.

Session: GET the portal page -> OpenID Connect redirect with `acr_values=anonymous` -> the
identity provider answers with an auto-submitting form (code, state, iss) -> POST it to
/Default -> cookies `3v.rooms`, `3v.roomsC1`, `3v.roomsC2`. No credentials are involved.

Times: the query parameters `from`/`to` are real UTC instants, but every time in the
response is Zurich *wall-clock* time with a misleading `Z` suffix. Evidence (2026-11-20):
`BeginnEffective: "…T10:15:00Z"` comes with `Kommentar: "10:15 - 12:00"` (UZH lectures start
at :15 local), holidays run `00:00Z`–`23:59Z`, a blocked slot titled "Buchbar ab 18:00 Uhr"
ends at `18:00Z`, and the same weekly lecture stays at `08:00Z` before and after the switch to
summer time (2027-03-28). Asking from `00:00Z` (= 01:00 CET) cuts the nightly closure at
`01:00Z`. So we drop the `Z` and store the wall-clock time as it is.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from . import reliability, store
from .api import USER_AGENT, retry_after
from .availability import TZ
from .campus import IRCHEL, OERLIKON, ZENTRUM, canonical

log = logging.getLogger(__name__)

BASE = "https://uzh.book.3vrooms.app"
PORTAL = "/default/Anonymous/Calendar/PortalFind"
API = "/default/api/v1.0"
SESSION_COOKIE = "3v.rooms"
SOURCE = "uzh_rooms"      # key in store.source_health
UNI = "UZH"
CAMPUSES = (ZENTRUM, IRCHEL, OERLIKON)
ROOMS_FILE = Path(__file__).with_name("uzh_rooms.txt")
DELAY = 1.0               # seconds between requests; one day of all rooms is ~0.5 MB
REFRESH_MIN = 6 * 3600    # serve --refresh: UZH at most every 6 h (the data changes slowly)

ROOM_TYPE = "1"           # RessourcetypId of rooms; "2" is equipment (projectors, microphones)
# RessourceTyp of a block -> our kind
KINDS = {
    "1": "busy",          # booking ("belegt", no event names)
    "O": "closed",        # outside opening hours (nights, weekends)
    "S": "busy",          # Sperrzeit: blocked, e.g. "Buchbar ab 18:00 Uhr.", "Einsehbar ab 15. März 2027";
                          # closed instead when its text says "geschlossen" (normalize_block)
    "F": "closed",        # Feiertag: "Karfreitag UZH geschlossen", "Weihnachtsferien UZH geschlossen"
}


class LoginError(RuntimeError):
    pass


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action, self._in_form = a.get("action"), True
        elif tag == "input" and self._in_form and a.get("type", "").lower() == "hidden" and a.get("name"):
            self.fields[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False


def parse_login_form(html: str) -> tuple[str, dict[str, str]]:
    """(action, hidden fields) of the identity provider's auto-submitting form."""
    p = _FormParser()
    p.feed(html)
    if not p.action or "code" not in p.fields or "state" not in p.fields:
        raise LoginError("no login form with code/state in the identity provider's answer")
    return p.action, p.fields


def day_window(d: date) -> tuple[str, str]:
    """`from`/`to` for one Zurich day as UTC instants (23 or 25 h long on DST days)."""
    def utc(x: date) -> str:
        local = datetime(x.year, x.month, x.day, tzinfo=TZ)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return utc(d), utc(d + timedelta(days=1))


class UzhClient:
    """Anonymous 3V-Rooms session. Logs in lazily and once more when the session has expired."""

    def __init__(self, base: str = BASE, delay: float = DELAY, timeout: float = 90.0, retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.base = base.rstrip("/")
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._sleep, self._clock = sleep, clock
        self._last = float("-inf")
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.logins = 0
        self._logged_in = False

    # -- HTTP --

    def _open(self, req: urllib.request.Request):
        req.add_header("User-Agent", USER_AGENT)
        wait = self._last + self.delay - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()
        return self._opener.open(req, timeout=self.timeout)

    def login(self) -> None:
        self.cookies.clear()
        with self._open(urllib.request.Request(self.base + PORTAL, headers={"Accept": "text/html"})) as r:
            html = r.read().decode("utf-8", "replace")
        action, fields = parse_login_form(html)
        # Only ever post the code back to the booking site itself.
        if urllib.parse.urlsplit(action)[:2] != urllib.parse.urlsplit(self.base)[:2]:
            raise LoginError(f"login form posts to an unexpected place: {action}")
        req = urllib.request.Request(action, data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Accept": "text/html",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with self._open(req) as r:
            r.read()
        if not any(c.name == SESSION_COOKIE for c in self.cookies):
            raise LoginError(f"no {SESSION_COOKIE} cookie after the login form")
        self.logins += 1
        self._logged_in = True
        log.debug("UZH 3V-Rooms: anonymous session started")

    def get_json(self, path: str, params: list[tuple[str, str]] | None = None) -> object:
        """GET an API path. Logs in first; on 401 (or a login page instead of JSON) logs in once more."""
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        relogged = False
        attempt = 0
        while True:
            if not self._logged_in:
                self.login()
            try:
                with self._open(urllib.request.Request(url, headers={"Accept": "application/json"})) as r:
                    ctype = r.headers.get("Content-Type", "")
                    body = r.read()
                if "json" not in ctype:   # an expired session ends on the login page, not in a 401
                    raise _SessionExpired(f"got {ctype or 'no content type'} instead of JSON")
                return json.loads(body)
            except (_SessionExpired, urllib.error.HTTPError) as e:
                expired = isinstance(e, _SessionExpired) or e.code == 401
                if isinstance(e, urllib.error.HTTPError) and (expired and not relogged or
                                                              attempt < self.retries and (e.code >= 500 or e.code == 429)):
                    e.close()   # handled below: don't leak the connection
                if expired and not relogged:
                    log.info("UZH 3V-Rooms: session expired (%s), logging in again", e)
                    self._logged_in, relogged = False, True
                    continue
                if expired or not isinstance(e, urllib.error.HTTPError):
                    raise
                if attempt >= self.retries or (400 <= e.code < 500 and e.code != 429):
                    raise
                attempt += 1
                self._sleep(retry_after((e.headers or {}).get("Retry-After")) if e.code == 429 else 2 * attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= self.retries:
                    raise
                attempt += 1
                self._sleep(2 * attempt)

    # -- API --

    def filtersettings(self) -> list[dict]:
        """UZH's saved calendar views: Zentrum, Irchel, Oerlikon (one per campus) and Plakataushang."""
        return self.get_json(API + "/filtersetting/Kalender")  # type: ignore[return-value]

    def calendar(self, start: str, end: str, ids: list[int] | None = None,
                 filtersetting: int | None = None) -> dict:
        """Raw calendar for [start, end) (UTC 'YYYY-MM-DDTHH:MM:SSZ'), for resource ids or a saved view.

        Returns {"RessourceList": [{"Id", "Title", "RessourcetypId", "BlockList": [...]}, ...],
        "StandortList": [...]}; block times are Zurich wall-clock time despite their 'Z'.
        """
        params = [("from", start), ("to", end), ("mode", "Day")]
        params += [("id", str(i)) for i in ids or ()]
        if filtersetting is not None:
            params.append(("filtersettingId", str(filtersetting)))
        return self.get_json(API + "/calendar", params)  # type: ignore[return-value]

    def day(self, d: date, ids: list[int] | None = None, filtersetting: int | None = None) -> dict:
        """Raw calendar for one Zurich day."""
        return self.calendar(*day_window(d), ids=ids, filtersetting=filtersetting)


class _SessionExpired(LoginError):
    """The API answered with something other than JSON (the login page): no valid session."""


# -- room list --

_ID = re.compile(r"^(?P<building>[A-Z][A-Z0-9]{1,3})-(?P<floor>[A-Z0-9]{1,2})-(?P<room>(?:[A-Z]\.)?\d[0-9A-Za-z./]*)(?=\s|$)(?P<rest>.*)$")
_SEATS = (re.compile(r"(\d+)\s*Sitzplätze", re.I), re.compile(r"(\d+)[^()]*?Plätze", re.I))
_PERSONS = re.compile(r"\d+\s*Personen", re.I)
# Bookable spaces in the calendar that are not rooms to study in (checked against the list of 2026-11-20).
_NOT_ROOMS = re.compile(
    r"Apérofläche|Lager|Küche|Lichthof|Foyer|Vorlage|Eingangsbereich|Empfang|Dozierendenzimmer|"
    r"Plakat|Vitrine|Magnetboard|Poster|Labor|Mall|Medizinerhof|\bBar\b", re.I)
# "EV" (event) rooms are dropped unless uzh_rooms.txt lists them: some host regular courses (#14).
_EVENT = re.compile(r"\bEV\b")
EVENT_ROOM = "event room (EV)"
# Rooms left out on purpose although their title looks like a teaching room (no warning for them).
EXCLUDED = {"KOL-G-201": "only bookable on request (standing S block 2026-2030), would always show busy"}
# An S block whose text says the building is closed is a closure, e.g. "Semesterferien GLT geschlossen".
_CLOSED_TEXT = re.compile(r"geschlossen", re.I)


def parse_title(title: str) -> dict | None:
    """'KOL-F-101 (209 Plätze) **' -> {id, building, floor, room, seats, label}; None if it has no room id."""
    m = _ID.match(title.strip())
    if not m:
        return None
    rest = m["rest"]
    seats = None
    for group in re.findall(r"\(([^()]*)\)", rest):
        s = _SEATS[0].search(group) or _SEATS[1].search(group)   # "80 Stehplätze/ 60 Sitzplätze" -> 60
        if s:
            seats = int(s[1])
            rest = rest.replace(f"({group})", " ")
            break
    label = re.sub(r"\s+", " ", re.sub(r"[()*]", " ", rest)).strip() or None
    rid = f"{m['building']}-{m['floor']}-{m['room']}"
    return {"id": rid, "building": m["building"], "floor": m["floor"], "room": m["room"],
            "seats": seats, "label": label}


def drop_reason(title: str, known_ids: set[str] = frozenset()) -> str | None:
    """Why a room resource is not a room to study in, or None. `known_ids`: ids of all room
    resources, to recognise combinations (AND-3-02/06 is AND-3-02 + AND-3-06, which are listed)."""
    p = parse_title(title)
    if p is None:
        return "no building-floor-room id"
    m = _NOT_ROOMS.search(title)
    if m:
        return f"not a room ({m[0]})"
    if _EVENT.search(title):
        return EVENT_ROOM
    if _PERSONS.search(title) and not p["seats"]:
        return "event space (capacity in Personen)"
    parts = combo_parts(p["id"])
    if any(x in known_ids for x in parts):   # e.g. Y22-F-62/70: Y22-F-62 is listed itself
        return "combination of listed rooms"
    return None


def combo_parts(rid: str) -> list[str]:
    """'BIN-0-K.11/12/13' -> ['BIN-0-K.11', 'BIN-0-K.12', 'BIN-0-K.13']; [] for a single room."""
    prefix, _, room = rid.rpartition("-")
    pieces = room.split("/")
    if len(pieces) < 2:
        return []
    first = pieces[0]
    return [f"{prefix}-{first[:max(0, len(first) - len(x))] + x}" for x in pieces]


def load_allowlist(path: Path = ROOMS_FILE) -> frozenset[str]:
    lines = (ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return frozenset(ln for ln in lines if ln)


def _campus_name(view: dict) -> str | None:
    """Campus of a saved view's StandortList (title '\\UZH\\Zentrum')."""
    for s in view.get("StandortList") or []:
        title = s.get("Title") if isinstance(s, dict) else None
        if isinstance(title, str) and title.strip("\\"):
            return title.strip("\\").split("\\")[-1]
    return None


# -- blocks --

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")


def _wall(s: str) -> datetime:
    """'2026-11-20T10:15:00Z' -> naive 2026-11-20 10:15 (Zurich wall clock, see module doc)."""
    return datetime.fromisoformat(s[:-1]).replace(microsecond=0)


def validate_block(b: object) -> str | None:
    """Why a raw block is unusable, or None. A room with an unusable block is stored as unknown."""
    if not isinstance(b, dict):
        return f"not an object: {type(b).__name__}"
    times = []
    for key in ("BeginnEffective", "EndeEffective"):
        v = b.get(key)
        if not isinstance(v, str) or not _TS.match(v):
            return f"invalid {key}: {v!r}"
        try:
            times.append(_wall(v))
        except ValueError:
            return f"invalid {key}: {v!r}"
    if times[1] < times[0]:
        return f"EndeEffective before BeginnEffective: {b['BeginnEffective']} > {b['EndeEffective']}"
    t = b.get("RessourceTyp")
    if not isinstance(t, str) or not t:
        return f"invalid RessourceTyp: {t!r}"
    for key in ("Titel", "Kommentar"):
        if b.get(key) is not None and not isinstance(b[key], str):
            return f"{key} is {type(b[key]).__name__}"
    return None


def normalize_block(b: dict, warned: set | None = None) -> tuple[datetime, datetime, str, str | None, str]:
    """(start, end, kind, title, raw_type) of a valid block. Unknown types count as busy."""
    t = b["RessourceTyp"]
    kind = KINDS.get(t)
    if kind is None:
        kind = "busy"
        if warned is None or t not in warned:
            log.warning("unknown UZH RessourceTyp %r, treating as busy: %s", t, b)
            if warned is not None:
                warned.add(t)
    title = None
    if t in ("S", "F") or t not in KINDS:   # bookings only say "belegt"; closures need no title
        title = (b.get("Titel") or "").strip() or (b.get("Kommentar") or "").strip() or None
    if t == "S" and title and _CLOSED_TEXT.search(title):
        kind = "closed"
    return _wall(b["BeginnEffective"]), _wall(b["EndeEffective"]), kind, title, t


def split_days(start: datetime, end: datetime, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """Clip [start, end) to [lo, hi) and cut it at midnight. A piece that runs to midnight ends
    at 23:59, like ETH's entries, so the UI's day timeline draws it (availability merges the gap)."""
    out = []
    s, e = max(start, lo), min(end, hi)
    while s < e:
        midnight = datetime(s.year, s.month, s.day) + timedelta(days=1)
        piece_end = min(e, midnight)
        out.append((s, piece_end - timedelta(minutes=1) if piece_end == midnight else piece_end))
        s = piece_end
    return out


def _block_key(b: dict) -> tuple:
    # The same block comes once per day it touches, and twice in one day when it crosses
    # midnight (EntryPart 'start' and 'end').
    return (b.get("Id"), b.get("RessourceTyp"), b.get("BeginnEffective"), b.get("EndeEffective"))


# -- fetch --

def run(conn: sqlite3.Connection, client: UzhClient | None = None, days: int = 7,
        max_age: float | None = None, today: date | None = None) -> dict:
    """Fetch the UZH rooms for `days` days from today into `rooms`/`intervals` (uni='UZH')."""
    try:
        stats = _run(conn, client or UzhClient(), days, today or datetime.now(TZ).date())
    except Exception as e:
        store.record_attempt(conn, SOURCE, error=f"{type(e).__name__}: {e}", max_age=max_age)
        raise
    store.record_attempt(conn, SOURCE, counts=stats, max_age=max_age)
    conn.commit()
    return stats


def _resources(resp: object) -> list[dict]:
    if not isinstance(resp, dict) or not isinstance(resp.get("RessourceList"), list):
        raise ValueError(f"unexpected calendar response: {str(resp)[:200]}")
    return [r for r in resp["RessourceList"] if isinstance(r, dict)]


def _run(conn, client: UzhClient, days: int, today: date) -> dict:
    views = client.filtersettings()
    if not isinstance(views, list):
        raise ValueError(f"unexpected filter list: {str(views)[:200]}")
    campus_views = [(v["Id"], canonical(v.get("Description"))) for v in views
                    if isinstance(v, dict) and isinstance(v.get("Id"), int) and canonical(v.get("Description")) in CAMPUSES]
    if not campus_views:
        raise ValueError(f"no campus views (Zentrum/Irchel/Oerlikon) in {views}")
    if len(campus_views) < len(CAMPUSES):
        log.warning("UZH: only campus views %s found", [c for _, c in campus_views])

    # Day 1: one request per campus view. It gives us the room list and each room's campus.
    by_id: dict[int, dict] = {}       # numeric resource id -> resource of day 1
    campus_of: dict[int, tuple[str, str]] = {}
    raw_blocks: dict[int, list] = {}
    for view_id, campus in campus_views:
        resp = client.day(today, filtersetting=view_id)
        site = _campus_name(resp) or campus
        if canonical(site) != campus:
            log.warning("UZH view %s says campus %r, expected %r", view_id, site, campus)
        for r in _resources(resp):
            if r.get("RessourcetypId") == ROOM_TYPE and isinstance(r.get("Id"), int):
                by_id[r["Id"]] = r
                campus_of[r["Id"]] = (campus, site)
                raw_blocks.setdefault(r["Id"], []).append(r.get("BlockList"))

    titles = {i: r.get("Title") for i, r in by_id.items() if isinstance(r.get("Title"), str)}
    parsed = {i: p for i, t in titles.items() if (p := parse_title(t))}
    allow = load_allowlist()
    known = {p["id"] for p in parsed.values()}
    # Both checks: "KOL-F-101 Apérofläche" has the same id as the lecture hall KOL-F-101.
    # An EV room is kept only when the allowlist names it (see uzh_rooms.txt).
    selected = {i: p for i, p in parsed.items()
                if p["id"] in allow and drop_reason(titles[i], known) in (None, EVENT_ROOM)}
    if not selected:
        raise ValueError(f"none of the {len(by_id)} UZH rooms is in uzh_rooms.txt")
    gone = allow - known
    if gone:
        log.warning("uzh_rooms.txt lists rooms the calendar no longer has: %s", ", ".join(sorted(gone)))
    new = sorted(titles[i] for i, p in parsed.items()
                 if p["id"] not in allow and p["id"] not in EXCLUDED and drop_reason(titles[i], known) is None)
    if new:
        log.warning("UZH rooms not in uzh_rooms.txt (add them if they are teaching rooms): %s", "; ".join(new))
    dupes = [rid for rid, n in Counter(p["id"] for p in selected.values()).items() if n > 1]
    if dupes:
        raise ValueError(f"UZH room ids are not unique: {dupes}")
    log.info("UZH room list: %d resources, %d rooms selected", len(by_id), len(selected))

    # Further days: one request for all selected rooms. Stop at the first failing day and
    # store what we have: a missing day must never make rooms look free, so the window shrinks.
    ids = sorted(selected)
    fetched_days = 1
    for k in range(1, days):
        try:
            res = _resources(client.day(today + timedelta(days=k), ids=ids))
        except Exception as e:
            log.error("UZH day %s failed, keeping %d day(s): %s", today + timedelta(days=k), fetched_days, e)
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            break
        seen = set()
        for r in res:
            if r.get("Id") in selected:
                raw_blocks[r["Id"]].append(r.get("BlockList"))
                seen.add(r["Id"])
        for i in set(ids) - seen:
            raw_blocks[i].append(f"missing from the answer for {today + timedelta(days=k)}")
        fetched_days += 1

    lo = datetime(today.year, today.month, today.day)
    hi = lo + timedelta(days=fetched_days)
    keep = [p["id"] for p in selected.values()]
    conn.execute(f"DELETE FROM rooms WHERE uni=? AND id NOT IN ({','.join('?' * len(keep))})", (UNI, *keep))
    conn.commit()

    stats = {"rooms": len(selected), "ok": 0, "empty": 0, "malformed": 0, "intervals": 0,
             "days": fetched_days, "types": {}}
    by_campus: Counter = Counter()
    warned: set = set()
    for i, p in sorted(selected.items(), key=lambda x: x[1]["id"]):
        rid = p["id"]
        blocks, problems = {}, []
        for lst in raw_blocks[i]:
            if not isinstance(lst, list):
                problems.append((lst, "BlockList is not a list" if not isinstance(lst, str) else lst))
                continue
            for b in lst:
                why = validate_block(b)
                if why:
                    problems.append((b, why))
                else:
                    blocks.setdefault(_block_key(b), b)
        rows = []
        for b in blocks.values():
            start, end, kind, title, raw = normalize_block(b, warned)
            stats["types"][raw] = stats["types"].get(raw, 0) + 1
            rows += [(s.isoformat(), e.isoformat(), kind, title, raw) for s, e in split_days(start, end, lo, hi)]
        n_raw = len(blocks)
        if problems:
            stats["malformed"] += 1
            prev = conn.execute("SELECT n_entries FROM rooms WHERE id=?", (rid,)).fetchone()
            if prev and prev[0]:
                log.warning("%s: %d malformed blocks, keeping previous data; first: %s (%r)",
                            rid, len(problems), problems[0][1], problems[0][0])
                stats["ok"] += 1
                continue
            log.warning("%s: %d malformed blocks, marking unknown; first: %s (%r)",
                        rid, len(problems), problems[0][1], problems[0][0])
            n_raw = 0
        elif not blocks:
            # Rooms normally have nightly closures ('O'); nothing at all is suspicious -> unknown.
            log.warning("%s: no blocks at all in %d days, marking unknown", rid, fetched_days)
            stats["empty"] += 1
        campus, site = campus_of[i]
        reliability.record_fetch(conn, UNI, rid, rows, n_raw, (lo.date(), (hi - timedelta(days=1)).date()))
        _save_room(conn, p, rid, campus, site, rows, n_raw)
        by_campus[campus] += 1
        stats["ok"] += 1
        stats["intervals"] += len(rows)

    store.set_meta(conn, store.window_key(UNI), f"{lo.date().isoformat()}/{(hi - timedelta(days=1)).date().isoformat()}")
    conn.commit()
    stats["campus"] = dict(sorted(by_campus.items()))
    log.info("UZH fetch done: %s", stats)
    return stats


def _save_room(conn, p: dict, rid: str, campus: str, site: str, rows, n_raw: int) -> None:
    conn.execute(
        """INSERT INTO rooms(id, building, floor, room, type, typecode, seats, workplaces, site, uni, campus,
                              fetched_at, n_entries)
           VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET building=excluded.building, floor=excluded.floor,
             room=excluded.room, type=excluded.type, typecode=NULL, seats=excluded.seats, workplaces=NULL,
             site=excluded.site, uni=excluded.uni, campus=excluded.campus, fetched_at=excluded.fetched_at,
             n_entries=excluded.n_entries""",
        (rid, p["building"], p["floor"], p["room"], p["label"], p["seats"], site, UNI, canonical(campus),
         datetime.now(timezone.utc).isoformat(timespec="seconds"), n_raw),
    )
    conn.execute("DELETE FROM intervals WHERE room_id=?", (rid,))
    conn.executemany("INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES (?,?,?,?,?,?)",
                     [(rid, *row) for row in rows])
    conn.commit()
