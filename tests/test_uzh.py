import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from freerooms import fetch, store, uzh
from freerooms.availability import building_hours, query

FIXTURES = Path(__file__).with_name("fixtures")
VIEWS = {24885: "uzh_calendar_zentrum.json", 22059: "uzh_calendar_irchel.json", 15325: "uzh_calendar_oerlikon.json"}
TODAY = date(2026, 11, 20)   # the fixtures' day (a Friday); the ids fixture is Saturday 2026-11-21


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def block(**over):
    b = {"Id": 1, "RessourceId": 136, "RessourceTyp": "1", "BeginnEffective": "2026-11-20T10:15:00Z",
         "EndeEffective": "2026-11-20T12:00:00Z", "Titel": "belegt", "Kommentar": "10:15 - 12:00"}
    b.update(over)
    return b


class FakeRooms:
    """Local stand-in for uzh.book.3vrooms.app: OIDC-style login with a cookie, then JSON API."""

    def __init__(self):
        self.logins = 0
        self.token = None          # the valid session cookie value
        self.api_calls = []
        self.fail_days = set()     # 'from' values that answer 500
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cookie(self):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == uzh.SESSION_COOKIE:
                        return v
                return None

            def _send(self, code, body=b"", ctype="text/html", headers=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                qs = urllib.parse.parse_qs(url.query)
                if url.path == uzh.PORTAL:
                    if fake.token and self._cookie() == fake.token:
                        return self._send(200, b"<html>portal</html>")
                    return self._send(302, headers=[("Location", "/idp/connect/authorize?acr_values=anonymous")])
                if url.path == "/idp/connect/authorize":
                    html = fixture("uzh_login_form.html").replace("https://uzh.book.3vrooms.app", fake.base)
                    return self._send(200, html.encode())
                if not url.path.startswith(uzh.API):
                    return self._send(404)
                if not fake.token or self._cookie() != fake.token:
                    return self._send(401, b"", "text/plain")
                fake.api_calls.append(self.path)
                if url.path.endswith("/filtersetting/Kalender"):
                    return self._send(200, fixture("uzh_filtersettings.json").encode(), "application/json; charset=utf-8")
                if qs["from"][0] in fake.fail_days:
                    return self._send(500, b"oops", "text/plain")
                if "filtersettingId" in qs:
                    body = fixture(VIEWS[int(qs["filtersettingId"][0])])
                else:
                    d = json.loads(fixture("uzh_calendar_ids_2026-11-21.json"))
                    ids = {int(i) for i in qs.get("id", [])}
                    d["RessourceList"] = [r for r in d["RessourceList"] if r["Id"] in ids]
                    body = json.dumps(d)
                self._send(200, body.encode(), "application/json; charset=utf-8")

            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                if self.path != "/Default" or form.get("code") != ["hC3h9vqnFaWmCjY5nrPanKlQ6ov0oPYurKpyCXt3HGc"]:
                    return self._send(400)
                fake.logins += 1
                fake.token = f"session{fake.logins}"
                self._send(302, headers=[("Location", uzh.PORTAL), ("Set-Cookie", f"{uzh.SESSION_COOKIE}={fake.token}; Path=/"),
                                         ("Set-Cookie", "3v.roomsC1=x; Path=/"), ("Set-Cookie", "3v.roomsC2=y; Path=/")])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def client(self):
        return uzh.UzhClient(base=self.base, delay=0, sleep=lambda s: None)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class LoginForm(unittest.TestCase):
    def test_real_form(self):
        action, fields = uzh.parse_login_form(fixture("uzh_login_form.html"))
        self.assertEqual(action, "https://uzh.book.3vrooms.app/Default")
        self.assertEqual(set(fields), {"code", "state", "iss"})
        self.assertEqual(fields["iss"], "https://uzh.idp.3vrooms.app/")
        self.assertTrue(fields["state"].startswith("OpenIdConnect.AuthenticationProperties="))

    def test_no_form(self):
        with self.assertRaises(uzh.LoginError):
            uzh.parse_login_form("<html><body>Anmelden</body></html>")


class Session(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRooms()

    def tearDown(self):
        self.fake.close()

    def test_login_then_api(self):
        c = self.fake.client()
        views = c.filtersettings()
        self.assertEqual([v["Description"].strip() for v in views], ["Zentrum", "Irchel", "Oerlikon", "Plakataushang"])
        self.assertEqual((c.logins, self.fake.logins), (1, 1))
        self.assertTrue({"3v.rooms", "3v.roomsC1", "3v.roomsC2"} <= {k.name for k in c.cookies})

    def test_relogin_once_on_401(self):
        c = self.fake.client()
        c.filtersettings()
        self.fake.token = "expired-on-the-server"
        with self.assertLogs("freerooms.uzh", "INFO") as logs:
            c.filtersettings()
        self.assertEqual(c.logins, 2)
        self.assertTrue(any("session expired" in m for m in logs.output))

    def test_gives_up_after_one_relogin(self):
        c = self.fake.client()
        c.filtersettings()
        orig = c.login

        def broken_login():   # the server never accepts our session
            orig()
            self.fake.token = "nope"
        c.login = broken_login
        self.fake.token = "nope"
        with self.assertRaises(Exception) as cm, self.assertLogs("freerooms.uzh", "INFO"):
            c.filtersettings()
        self.assertEqual(getattr(cm.exception, "code", None), 401)
        cm.exception.close()

    def test_day_query_uses_utc_bounds_of_the_zurich_day(self):
        c = self.fake.client()
        c.day(TODAY, filtersetting=24885)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.fake.api_calls[-1]).query)
        self.assertEqual((q["from"], q["to"], q["mode"]), (["2026-11-19T23:00:00Z"], ["2026-11-20T23:00:00Z"], ["Day"]))


class Titles(unittest.TestCase):
    def test_parse(self):
        cases = {
            "KOL-F-101 (209 Plätze) **": ("KOL-F-101", "KOL", "F", "101", 209, None),
            "BIN-1-B.01 (170 Plätze)": ("BIN-1-B.01", "BIN", "1", "B.01", 170, None),
            "Y35-F-08a (14 Plätze)": ("Y35-F-08a", "Y35", "F", "08a", 14, None),
            "KOL-G-201 Aula (280 / mit Emporen 359 Plätze ) **": ("KOL-G-201", "KOL", "G", "201", 280, "Aula"),
            "HAH-E-12 Studienplätze (80 Stehplätze/ 60 Sitzplätze)": ("HAH-E-12", "HAH", "E", "12", 60, "Studienplätze"),
            "KUM-E-1 (30 Sitzplätze - ohne Tische!)": ("KUM-E-1", "KUM", "E", "1", 30, None),
            "KAB-E-05 (Stadt Zürich) (36 Plätze)": ("KAB-E-05", "KAB", "E", "05", 36, "Stadt Zürich"),
            "AND-3-02/06 (88 Plätze)": ("AND-3-02/06", "AND", "3", "02/06", 88, None),
            "KOL-E-12": ("KOL-E-12", "KOL", "E", "12", None, None),
            "Y24-G-55 Mikroskopierhörsaal (152 Plätze)": ("Y24-G-55", "Y24", "G", "55", 152, "Mikroskopierhörsaal"),
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                p = uzh.parse_title(title)
                self.assertEqual((p["id"], p["building"], p["floor"], p["room"], p["seats"], p["label"]), want)

    def test_not_parseable(self):
        for title in ("Lichthof Nord (450 Personen)", "Y-Lichthof (800 Personen)", "KOL-K-Küche (100 Personen)",
                      "SOE-E Eingangsbereich", "Irchel Bar", "Orellisaal", "BIN-0-Mall (120 Personen)",
                      "L Foyer West (KOL-D-43a)", "Vorlage Zentrum", "KOL, Plakat 1 (Haupteingang)"):
            with self.subTest(title=title):
                self.assertIsNone(uzh.parse_title(title))
                self.assertIsNotNone(uzh.drop_reason(title))

    def test_s_block_saying_closed_is_a_closure(self):
        b = {"RessourceTyp": "S", "BeginnEffective": "2026-12-25T00:00:00Z", "EndeEffective": "2027-02-21T23:59:00Z",
             "Titel": "Semesterferien GLT geschlossen"}
        self.assertEqual(uzh.normalize_block(b)[2:4], ("closed", "Semesterferien GLT geschlossen"))
        b["Titel"] = "Informationen über die Verfügbarkeit auf Anfrage."
        self.assertEqual(uzh.normalize_block(b)[2], "busy")

    def test_ev_rooms_only_when_listed(self):
        self.assertEqual(uzh.drop_reason("KOL-G-217 EV (129 Plätze) **"), uzh.EVENT_ROOM)
        allow = uzh.load_allowlist()
        self.assertIn("KOL-G-217", allow)        # regular courses there (docs/uzh-validation.md)
        self.assertNotIn("KOL-E-13", allow)      # Senatszimmer stays out
        self.assertNotIn("KOL-G-201", allow)

    def test_drop_reasons(self):
        known = {"AND-3-02", "AND-3-06", "BIN-0-K.11", "BIN-0-K.12", "BIN-0-K.13", "Y22-F-62"}
        dropped = ["KOL-F-101 Apérofläche (209 Personen)", "KOL-G-202 (Lager)", "KOL-G-217 EV (129 Plätze) **",
                   "KOL-E-13 EV Senatszimmer (40 Plätze)", "Y14-F-41 Labor-Praktikumsraum (92 Plätze)",
                   "AFL-E-070 Olivenhalle (150 Personen)", "KOL-E-21 Empfang (rechts)", "KOL-E-11 Dozierendenzimmer",
                   "Y23-G-100 Postersession", "AND-3-02/06 (88 Plätze)", "BIN-0-K.11/12/13 (54 Plätze)",
                   "Y22-F-62/70 (80 Plätze)"]
        kept = ["KOL-F-101 (209 Plätze) **", "Y13-L-11/13 (30 Plätze)", "KOL-E-21 (78 Plätze)",
                "Y21-F-65 Theatersaal (150 Plätze)", "HAH-E-12 Studienplätze (80 Stehplätze/ 60 Sitzplätze)"]
        for t in dropped:
            with self.subTest(t=t):
                self.assertIsNotNone(uzh.drop_reason(t, known))
        for t in kept:
            with self.subTest(t=t):
                self.assertIsNone(uzh.drop_reason(t, known))

    def test_combo_parts(self):
        self.assertEqual(uzh.combo_parts("BIN-0-K.11/12/13"), ["BIN-0-K.11", "BIN-0-K.12", "BIN-0-K.13"])
        self.assertEqual(uzh.combo_parts("AND-3-02/06"), ["AND-3-02", "AND-3-06"])
        self.assertEqual(uzh.combo_parts("KOL-F-101"), [])

    def test_allowlist(self):
        allow = uzh.load_allowlist()
        self.assertIn("KOL-F-101", allow)
        self.assertNotIn("AND-3-02/06", allow)
        self.assertTrue(all(" " not in r for r in allow))   # never collides with ETH's "HG E 1.2"
        self.assertGreater(len(allow), 100)


class Blocks(unittest.TestCase):
    def test_kinds_and_titles(self):
        self.assertEqual(uzh.normalize_block(block())[2:], ("busy", None, "1"))
        self.assertEqual(uzh.normalize_block(block(RessourceTyp="O", Titel=""))[2:], ("closed", None, "O"))
        self.assertEqual(uzh.normalize_block(block(RessourceTyp="S", Titel="Buchbar ab 18:00 Uhr."))[2:],
                         ("busy", "Buchbar ab 18:00 Uhr.", "S"))
        self.assertEqual(uzh.normalize_block(block(RessourceTyp="S", Titel="", Kommentar="06:00 - 18:00"))[3],
                         "06:00 - 18:00")
        self.assertEqual(uzh.normalize_block(block(RessourceTyp="F", Titel="Karfreitag UZH geschlossen"))[2:],
                         ("closed", "Karfreitag UZH geschlossen", "F"))
        with self.assertLogs("freerooms.uzh", "WARNING"):
            self.assertEqual(uzh.normalize_block(block(RessourceTyp="X"))[2], "busy")

    def test_validate(self):
        self.assertIsNone(uzh.validate_block(block()))
        bad = [None, "x", block(BeginnEffective=None), block(EndeEffective="2026-11-20T12:00:00"),
               block(EndeEffective="2026-11-20T12:00:00+01:00"), block(BeginnEffective="2026-13-20T10:00:00Z"),
               block(EndeEffective="2026-11-20T09:00:00Z"), block(RessourceTyp=None), block(RessourceTyp=1),
               block(Titel=["belegt"])]
        for b in bad:
            with self.subTest(b=b):
                self.assertIsNotNone(uzh.validate_block(b))

    def test_times_are_wall_clock_across_dst(self):
        # Real data around the switch to summer time (Sunday 2027-03-28): the Z times stay
        # at the same wall-clock time on both sides, and the 'Kommentar' agrees with them.
        d = json.loads(fixture("uzh_calendar_easter_2027.json"))
        rooms = {r["Id"]: r for r in d["RessourceList"]}
        apero = [b for b in rooms[142]["BlockList"] if b["RessourceTyp"] == "S" and b["Titel"].startswith("Buchbar")]
        starts = {uzh.normalize_block(b)[0].date(): uzh.normalize_block(b)[:2] for b in apero}
        self.assertEqual(starts[date(2027, 3, 26)], (datetime(2027, 3, 26, 6), datetime(2027, 3, 26, 18)))
        self.assertEqual(starts[date(2027, 3, 30)], (datetime(2027, 3, 30, 6), datetime(2027, 3, 30, 18)))
        for b in rooms[136]["BlockList"]:
            s, e = uzh.normalize_block(b)[:2]
            self.assertEqual(b["Kommentar"][:13], f"{s:%H:%M} - {e:%H:%M}")
        holidays = {uzh.normalize_block(b)[3] for b in rooms[136]["BlockList"] if b["RessourceTyp"] == "F"}
        self.assertIn("Karfreitag UZH geschlossen", holidays)

    def test_day_window_dst(self):
        self.assertEqual(uzh.day_window(date(2026, 11, 20)), ("2026-11-19T23:00:00Z", "2026-11-20T23:00:00Z"))
        self.assertEqual(uzh.day_window(date(2027, 3, 28)), ("2027-03-27T23:00:00Z", "2027-03-28T22:00:00Z"))  # 23 h
        self.assertEqual(uzh.day_window(date(2026, 10, 25)), ("2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"))  # 25 h
        self.assertEqual(uzh.day_window(date(2027, 3, 29)), ("2027-03-28T22:00:00Z", "2027-03-29T22:00:00Z"))

    def test_split_days(self):
        lo, hi = datetime(2026, 11, 20), datetime(2026, 11, 22)
        # A Sperrzeit from September to Christmas becomes one piece per fetched day.
        self.assertEqual(uzh.split_days(datetime(2026, 9, 21), datetime(2026, 12, 24, 23, 59), lo, hi),
                         [(datetime(2026, 11, 20), datetime(2026, 11, 20, 23, 59)),
                          (datetime(2026, 11, 21), datetime(2026, 11, 21, 23, 59))])
        # The nightly closure 19:00-00:00 ends at 23:59 like ETH's entries.
        self.assertEqual(uzh.split_days(datetime(2026, 11, 20, 19), datetime(2026, 11, 21), lo, hi),
                         [(datetime(2026, 11, 20, 19), datetime(2026, 11, 20, 23, 59))])
        self.assertEqual(uzh.split_days(datetime(2026, 11, 21), datetime(2026, 11, 21), lo, hi), [])


class FetchRun(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRooms()
        self.db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.fake.close()

    def run_fetch(self, days=2):
        with self.assertLogs("freerooms.uzh", "INFO") as logs:
            stats = uzh.run(self.conn, self.fake.client(), days=days, today=TODAY)
        return stats, logs.output

    def rooms(self, t):
        return {r["id"]: r for r in query(self.conn, datetime.fromisoformat(t))}

    def test_fetch_into_sqlite(self):
        stats, _ = self.run_fetch()
        # 1 filter list + 3 campus views for day 1 + 1 id request for day 2
        self.assertEqual(len(self.fake.api_calls), 5)
        ids = {r["id"] for r in self.conn.execute("SELECT id FROM rooms WHERE uni='UZH'")}
        self.assertEqual(ids, {"KOL-F-101", "KOL-G-217", "KAB-E-05", "KUM-E-1", "KOH-B-10", "RAI-F-041", "KOL-E-12",
                               "KOL-E-18", "Y15-G-20", "Y24-G-45", "Y35-F-08a", "AFL-E-009", "AND-3-02",
                               "AND-3-06", "BIN-1-B.01"})
        self.assertEqual(stats["campus"], {"Irchel": 3, "Oerlikon": 4, "Zentrum": 8})
        self.assertEqual((stats["rooms"], stats["ok"], stats["malformed"], stats["days"]), (15, 15, 0, 2))
        self.assertEqual(set(stats["types"]), {"1", "O", "F"})   # the fixture's only S block is the Aula's
        r = self.conn.execute("SELECT * FROM rooms WHERE id='KOL-F-101'").fetchone()
        self.assertEqual((r["uni"], r["campus"], r["site"], r["building"], r["floor"], r["room"], r["seats"]),
                         ("UZH", "Zentrum", "Zentrum", "KOL", "F", "101", 209))
        self.assertEqual(self.conn.execute("SELECT campus FROM rooms WHERE id='Y15-G-20'").fetchone()[0], "Irchel")
        self.assertEqual(store.get_meta(self.conn, store.window_key("UZH")), "2026-11-20/2026-11-21")
        h = store.health(self.conn, ("uzh_rooms",))["sources"]["uzh_rooms"]
        self.assertEqual((h["stale"], h["counts"]["rooms"]), (False, 15))

    def test_availability_works_unchanged(self):
        self.run_fetch()
        rooms = self.rooms("2026-11-20T11:00")
        kol = rooms["KOL-F-101"]   # real booking 10:15-12:00 that day
        self.assertEqual((kol["state"], kol["until"], kol["uni"], kol["campus"]), ("busy", "2026-11-20T12:00", "UZH", "Zentrum"))
        self.assertEqual(kol["timeline"][0][:2], ["00:00", "07:00"])
        self.assertTrue(kol["hours_known"])
        free = self.rooms("2026-11-20T12:30")["KOL-F-101"]
        self.assertEqual((free["state"], free["until"]), ("free", "2026-11-20T14:00"))
        night = self.rooms("2026-11-20T23:00")["KOL-F-101"]   # closed through the night into Saturday
        self.assertEqual(night["state"], "closed")
        self.assertEqual(night["until"], "2026-11-21T07:00")
        # Saturday: real 'F' block "Hauptgebäude ab 13:30 geschlossen" (F is not only holidays).
        sat = self.rooms("2026-11-21T14:00")["KOL-F-101"]
        self.assertEqual((sat["state"], sat["reason"]), ("closed", "Hauptgebäude ab 13:30 geschlossen"))
        # KOL-G-201 Aula (standing 'S' block "auf Anfrage", 2026-2030) is left out of uzh_rooms.txt.
        self.assertNotIn("KOL-G-201", self.rooms("2026-11-20T13:00"))
        self.assertEqual(self.rooms("2026-11-22T10:00")["KOL-F-101"]["state"], "unknown")   # past the window
        self.assertEqual(building_hours(self.conn, datetime(2026, 11, 20, 12))["KOL"], {"open": "07:00", "close": "22:00"})

    def test_kum_without_closures_has_unknown_hours(self):
        self.run_fetch(days=1)
        kum = self.rooms("2026-11-20T10:00")["KUM-E-1"]
        self.assertEqual((kum["state"], kum["hours_known"]), ("free", False))

    def test_prunes_only_uzh_rooms(self):
        self.conn.execute("INSERT INTO rooms(id, building, floor, room, uni, n_entries) VALUES "
                          "('HG E 1.2','HG','E','1.2','ETH',3), ('KOL-X-1','KOL','X','1','UZH',3)")
        self.conn.commit()
        self.run_fetch(days=1)
        ids = {r[0] for r in self.conn.execute("SELECT id FROM rooms")}
        self.assertIn("HG E 1.2", ids)
        self.assertNotIn("KOL-X-1", ids)

    def test_failed_day_shrinks_the_window(self):
        self.fake.fail_days.add("2026-11-20T23:00:00Z")
        stats, logs = self.run_fetch(days=3)
        self.assertEqual(stats["days"], 1)
        self.assertEqual(store.get_meta(self.conn, store.window_key("UZH")), "2026-11-20/2026-11-20")
        self.assertTrue(any("keeping 1 day" in m for m in logs))
        self.assertEqual(self.rooms("2026-11-21T10:00")["KOL-F-101"]["state"], "unknown")

    def test_malformed_block_makes_room_unknown(self):
        good = self.fake.client()
        orig = good.day

        def day(d, ids=None, filtersetting=None):
            resp = orig(d, ids=ids, filtersetting=filtersetting)
            for r in resp["RessourceList"]:
                if r["Title"].startswith("KOL-F-101"):
                    r["BlockList"].append(block(EndeEffective=None))
            return resp
        good.day = day
        with self.assertLogs("freerooms.uzh", "WARNING") as logs:
            stats = uzh.run(self.conn, good, days=1, today=TODAY)
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(self.rooms("2026-11-20T12:30")["KOL-F-101"]["state"], "unknown")
        self.assertTrue(any("KOL-F-101" in m and "marking unknown" in m for m in logs.output))

    def test_failure_is_recorded_and_eth_keeps_going(self):
        self.fake.close()   # nothing listens any more
        eth = {"client": EthClient(), "buildings": ("CAB",)}
        with self.assertLogs("freerooms.fetch", "ERROR"):
            res = fetch.run_all(self.conn, ("UZH", "ETH"), eth=eth,
                                uzh={"client": uzh.UzhClient(base=self.fake.base, delay=0, retries=0), "today": TODAY})
        self.assertIsNone(res["UZH"])
        self.assertEqual(res["ETH"]["ok"], 1)
        h = store.health(self.conn)["sources"]
        self.assertIsNotNone(h["uzh_rooms"]["last_error"])
        self.assertFalse(h["eth_rooms"]["stale"])
        self.fake = FakeRooms()   # for tearDown


class EthClient:
    def rooms(self):
        return [{"building": "CAB", "floor": "G", "room": "11", "typecode": "3420", "locationName": "Zürich Zentrum"}]

    def allocations(self, rid, start, end):
        return [{"date_from": "2026-11-11T10:00:00", "date_to": "2026-11-11T12:00:00",
                 "belegungsserie": {"belegungstyp": 2, "veranstaltung": {"allocationTitle": "Lecture"}}}]


class Unis(unittest.TestCase):
    def test_parse_unis(self):
        self.assertEqual(fetch.parse_unis(None), ("ETH", "UZH"))
        self.assertEqual(fetch.parse_unis("uzh"), ("UZH",))
        self.assertEqual(fetch.parse_unis("ETH, UZH,ETH"), ("ETH", "UZH"))
        with self.assertRaises(ValueError):
            fetch.parse_unis("EPFL")


if __name__ == "__main__":
    unittest.main()
