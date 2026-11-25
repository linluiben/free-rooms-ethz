import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from freerooms import fetch, reliability, store
from freerooms.availability import TZ
from freerooms.web import make_handler

RID = "HG E 1.2"
DAY = date(2026, 11, 23)   # a Monday


def utc(local: str) -> datetime:
    """Zurich wall-clock time -> aware UTC instant."""
    return datetime.fromisoformat(local).replace(tzinfo=TZ).astimezone(timezone.utc)


def busy(s, e, title="Lecture", raw=2):
    return (f"2026-11-{s}", f"2026-11-{e}", "busy", title, raw)


def closed(s, e):
    return (f"2026-11-{s}", f"2026-11-{e}", "closed", None, 8)


NIGHTS = [closed("23T00:00:00", "23T07:00:00"), closed("24T00:00:00", "24T07:00:00")]


class Fetches(unittest.TestCase):
    """Synthetic fetches through record_fetch + the same writes as fetch._save_room."""

    def setUp(self):
        self.conn = store.connect(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def tearDown(self):
        self.conn.close()

    def save(self, rows, at: str, window=(DAY, DAY + timedelta(days=7)), n_raw=None, uni="ETH"):
        """One fetch of RID at Zurich time `at`, covering the days `window`."""
        now = utc(at)
        n = len(rows) if n_raw is None else n_raw
        changes = reliability.record_fetch(self.conn, uni, RID, rows, n, window, now=now)
        self.conn.execute(
            """INSERT INTO rooms(id, building, floor, room, uni, fetched_at, n_entries) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at, n_entries=excluded.n_entries""",
            (RID, "HG", "E", "1.2", uni, now.isoformat(timespec="seconds"), n))
        self.conn.execute("DELETE FROM intervals WHERE room_id=?", (RID,))
        self.conn.executemany("INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES (?,?,?,?,?,?)",
                              [(RID, *r) for r in rows])
        store.set_meta(self.conn, store.window_key(uni), f"{window[0]}/{window[1]}")
        self.conn.commit()
        return changes

    def events(self, change=None):
        q = "SELECT change, start, lead_s, max_lead_s FROM booking_changes"
        return [tuple(r) for r in self.conn.execute(q + (" WHERE change=?" if change else ""), (change,) if change else ())]

    def test_short_notice_booking_is_recorded(self):
        self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00")], "2026-11-23T08:00")
        ch = self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00"), busy("24T09:00:00", "24T11:00:00", "Meeting")],
                       "2026-11-23T10:00")
        self.assertEqual([c for c, _ in ch], ["added"])
        (change, start, lead, max_lead), = self.events()
        self.assertEqual((change, start), ("added", "2026-11-24T09:00:00"))
        self.assertEqual((lead, max_lead), (23 * 3600, 25 * 3600))   # max lead: from the 08:00 fetch
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})   # 25 h: maybe not short notice

    def test_lead_is_measured_from_the_previous_fetch(self):
        self.save(NIGHTS, "2026-11-23T08:00")
        self.save(NIGHTS + [busy("23T15:00:00", "23T16:00:00")], "2026-11-23T10:00")
        (_, _, lead, max_lead), = self.events()
        self.assertEqual((lead, max_lead), (5 * 3600, 7 * 3600))
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00"))[RID]["short_notice"], 1)

    def test_long_gap_between_fetches_does_not_count(self):
        # The last fetch was three days ago: the booking may have been made long before its day.
        self.save([], "2026-11-20T08:00", window=(date(2026, 11, 20), date(2026, 11, 27)), n_raw=5)
        self.save([busy("23T15:00:00", "23T16:00:00")], "2026-11-23T10:00")
        self.assertEqual([c for c, *_ in self.events()], ["added"])
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})

    def test_first_fetch_records_nothing(self):
        self.assertEqual(self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00")], "2026-11-23T08:00"), [])
        self.assertEqual(self.events(), [])
        watch = self.conn.execute("SELECT watch_since FROM rooms").fetchone()[0]
        self.assertIsNone(watch)

    def test_window_moving_forward_is_not_a_booking(self):
        week1 = (DAY, DAY + timedelta(days=7))
        self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00"), busy("30T08:00:00", "30T10:00:00")],
                  "2026-11-23T20:00", window=week1)
        # Next morning: a new last day (Dec 1) and the 30th, which may have been the old window's open end.
        rows = [closed("24T00:00:00", "24T07:00:00"), busy("30T08:00:00", "30T10:00:00"),
                busy("30T13:00:00", "30T15:00:00"), ("2026-12-01T08:00:00", "2026-12-01T10:00:00", "busy", "New day", 2)]
        self.assertEqual(self.save(rows, "2026-11-24T06:00", window=(DAY + timedelta(days=1), DAY + timedelta(days=8))), [])
        # ... and yesterday's lecture dropping out of the window is not a cancellation.
        self.assertEqual(self.events(), [])

    def test_failed_and_empty_fetches(self):
        self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00")], "2026-11-23T08:00")
        # Empty answer (n_raw 0): stored as unknown, nothing recorded ...
        self.assertEqual(self.save([], "2026-11-23T09:00", n_raw=0), [])
        # ... and the next good fetch isn't compared with the unknown state.
        self.assertEqual(self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00"), busy("23T14:00:00", "23T15:00:00")],
                                   "2026-11-23T10:00"), [])
        self.assertEqual(self.events(), [])

    def test_source_dropping_bookings_and_recovering(self):
        rows = NIGHTS + [busy("23T14:00:00", "23T16:00:00"), busy("24T10:00:00", "24T12:00:00")]
        self.save(rows, "2026-11-23T08:00")
        self.save(NIGHTS, "2026-11-23T10:00")   # hiccup: only the closures came back
        self.save(rows, "2026-11-23T12:00")
        self.assertEqual(sorted(c for c, *_ in self.events()), ["removed", "removed", "restored", "restored"])
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T12:00")), {})

    def test_title_and_type_changes_are_not_changes(self):
        self.save(NIGHTS + [busy("23T14:00:00", "23T16:00:00", "Analysis I", 2)], "2026-11-23T08:00")
        self.assertEqual(self.save(NIGHTS + [busy("23T14:00:00", "23T16:00:00", "Analysis I (Übung)", 11)],
                                   "2026-11-23T10:00"), [])

    def test_kind_change_and_moves_dont_count(self):
        cx = ("2026-11-23T14:00:00", "2026-11-23T16:00:00", "cancelled", "Seminar", 13)
        self.save(NIGHTS + [cx, busy("24T10:00:00", "24T12:00:00")], "2026-11-23T08:00")
        ch = self.save(NIGHTS + [busy("23T14:00:00", "23T16:00:00", "Seminar"), busy("24T10:15:00", "24T12:00:00")],
                       "2026-11-23T10:00")
        self.assertEqual(sorted(c for c, _ in ch), ["moved", "removed", "restored"])
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})

    def test_bookings_that_ended_before_the_last_fetch_are_ignored(self):
        self.save(NIGHTS, "2026-11-23T12:00")
        self.assertEqual(self.save(NIGHTS + [busy("23T08:00:00", "23T10:00:00")], "2026-11-23T14:00"), [])
        # One still running when we last looked does count: we showed the room as free.
        ch = self.save(NIGHTS + [busy("23T08:00:00", "23T10:00:00"), busy("23T13:00:00", "23T17:00:00")],
                       "2026-11-23T16:00")
        self.assertEqual([c for c, _ in ch], ["added"])

    def test_room_that_kept_older_data_uses_its_own_window(self):
        # The room's last good fetch was on the 20th (window 20th-27th); later fetches failed for it,
        # but meta already says 23rd-30th. Days 28-29 were never fetched for this room.
        self.save(NIGHTS, "2026-11-20T08:00", window=(date(2026, 11, 20), date(2026, 11, 27)))
        store.set_meta(self.conn, store.window_key("ETH"), "2026-11-23/2026-11-30")
        ch = self.save(NIGHTS + [busy("28T08:00:00", "28T10:00:00"), busy("29T08:00:00", "29T10:00:00")],
                       "2026-11-23T08:00")
        self.assertEqual(ch, [])

    def test_uzh_rooms_are_diffed_too(self):
        self.save(NIGHTS, "2026-11-23T05:30", uni="UZH", window=(DAY, DAY + timedelta(days=6)))
        ch = self.save(NIGHTS + [busy("23T13:15:00", "23T15:00:00", "belegt", "1")], "2026-11-23T11:30",
                       uni="UZH", window=(DAY, DAY + timedelta(days=6)))
        self.assertEqual([c for c, _ in ch], ["added"])

    # Closures

    def test_short_notice_closure_counts(self):
        # "Hauptgebäude ab 13:30 geschlossen", announced the same morning.
        self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00")], "2026-11-23T08:00")
        ch = self.save(NIGHTS + [busy("23T10:00:00", "23T12:00:00"), closed("23T13:30:00", "23T23:59:00")],
                       "2026-11-23T10:00")
        self.assertEqual([c for c, _ in ch], ["closure"])
        (change, start, lead, max_lead), = self.events()
        self.assertEqual((change, start), ("closure", "2026-11-23T13:30:00"))
        self.assertEqual((lead, max_lead), (int(3.5 * 3600), int(5.5 * 3600)))
        s = reliability.scores(self.conn, utc("2026-11-23T10:00"))[RID]
        self.assertEqual((s["short_notice"], s["closures"], s["unreliable"]), (0, 1, False))

    def test_closure_brought_forward_counts_from_the_new_start(self):
        evening = closed("24T20:00:00", "24T23:59:00")
        self.save(NIGHTS + [evening], "2026-11-23T20:00")
        ch = self.save(NIGHTS + [closed("24T13:30:00", "24T23:59:00")], "2026-11-24T06:00")
        self.assertEqual(sorted(c for c, _ in ch), ["closure", "closure_removed"])
        (_, start, _, max_lead), = self.events("closure")
        self.assertEqual(start, "2026-11-24T13:30:00")       # the interval ...
        self.assertEqual(max_lead, int(17.5 * 3600))          # ... and the lead to 13:30, the first newly closed minute

    def test_closure_closing_more_later_counts_from_the_newly_closed_part(self):
        # The night closure extended to noon: what is newly closed starts at 07:00.
        self.save(NIGHTS + [closed("24T00:00:00", "24T07:00:00")], "2026-11-23T08:00")
        self.save(NIGHTS + [closed("24T00:00:00", "24T12:00:00")], "2026-11-23T10:00")
        (_, _, _, max_lead), = self.events("closure")
        self.assertEqual(max_lead, (24 + 7 - 8) * 3600)      # 07:00 on the 24th, from the 08:00 fetch: 23 h

    def test_split_or_shifted_closures_dont_count(self):
        self.save(NIGHTS + [closed("23T18:00:00", "23T23:59:00")], "2026-11-23T08:00")
        # The same evening closure cut in two, and the night closure starting 15 minutes earlier.
        rows = [closed("22T23:45:00", "23T07:00:00"), NIGHTS[1],
                closed("23T18:00:00", "23T21:00:00"), closed("23T21:00:00", "23T23:59:00")]
        ch = self.save(rows, "2026-11-23T10:00")
        self.assertNotIn("closure", [c for c, _ in ch])
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})

    def test_closures_dropped_and_back_dont_count(self):
        rows = NIGHTS + [busy("23T10:00:00", "23T11:00:00"), closed("23T18:00:00", "23T23:59:00")]
        self.save(rows, "2026-11-23T08:00")
        self.save([busy("23T10:00:00", "23T11:00:00")], "2026-11-23T09:00")   # hiccup: only bookings came back
        self.save(rows, "2026-11-23T10:00")
        self.assertNotIn("closure", [c for c, *_ in self.events()])
        self.assertIn("closure_restored", [c for c, *_ in self.events()])
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})

    def test_closure_after_a_long_gap_or_on_a_new_day_doesnt_count(self):
        self.save(NIGHTS, "2026-11-20T08:00", window=(date(2026, 11, 20), date(2026, 11, 27)), n_raw=5)
        self.save(NIGHTS + [closed("23T13:30:00", "23T23:59:00")], "2026-11-23T10:00")
        self.assertEqual(reliability.scores(self.conn, utc("2026-11-23T10:00")), {})   # may be 3 days old
        # A closure on the day entering the window is not a change.
        self.assertEqual(self.save(NIGHTS + [closed("23T13:30:00", "23T23:59:00"), closed("30T13:30:00", "30T23:59:00")],
                                   "2026-11-23T12:00", window=(DAY, DAY + timedelta(days=8))), [])


class Scoring(unittest.TestCase):
    now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.conn = store.connect(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def tearDown(self):
        self.conn.close()

    def room(self, rid, watched_days):
        since = (self.now - timedelta(days=watched_days)).isoformat(timespec="seconds")
        self.conn.execute("INSERT INTO rooms(id, building, floor, room, watch_since) VALUES (?,?,?,?,?)",
                          (rid, "HG", "E", rid, since))

    def event(self, rid, days_ago, change="added", max_lead_h=5):
        t = (self.now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.conn.execute("""INSERT INTO booking_changes(room_id, detected_at, prev_fetch, start, end, change, lead_s,
                             max_lead_s) VALUES (?,?,?,?,?,?,?,?)""",
                          (rid, t, t, "2026-11-23T10:00:00", "2026-11-23T12:00:00", change, 3600, max_lead_h * 3600))

    def test_threshold(self):
        self.room("A", 60)
        for d in (1, 5, 9, 13):          # 4 in 4 weeks = 1/week
            self.event("A", d)
        self.room("B", 60)
        for d in (1, 5, 40):             # 2 in the window, one too old
            self.event("B", d)
        s = reliability.scores(self.conn, self.now)
        self.assertEqual(s["A"], {"short_notice": 4, "closures": 0, "per_week": 1.0, "unreliable": True,
                                  "reason": "4 short-notice bookings in the last 4 weeks"})
        self.assertFalse(s["B"]["unreliable"])
        self.assertEqual(s["B"]["short_notice"], 2)

    def test_only_short_notice_additions_count(self):
        self.room("A", 60)
        for d in (1, 2, 3):
            self.event("A", d, max_lead_h=30)      # not short notice in the worst case
        for d in (1, 2, 3):
            self.event("A", d, change="restored")
            self.event("A", d, change="removed")
            self.event("A", d, change="moved")
        self.assertEqual(reliability.scores(self.conn, self.now), {})

    def test_short_history_uses_at_least_a_week(self):
        self.room("A", 2)                # watched for 2 days: 3 events are 3 per week, not 10
        for d in (0, 1, 1.5):
            self.event("A", d)
        self.room("B", 2)
        for d in (0, 1):
            self.event("B", d)
        s = reliability.scores(self.conn, self.now)
        self.assertEqual((s["A"]["per_week"], s["A"]["unreliable"]), (3.0, True))
        self.assertEqual(s["A"]["reason"], "3 short-notice bookings in the last week")
        self.assertFalse(s["B"]["unreliable"])   # below FLAG_MIN

    def test_rate_below_threshold(self):
        self.room("A", 60)
        for d in (1, 10, 20):            # 3 in 4 weeks = 0.75/week
            self.event("A", d)
        self.assertFalse(reliability.scores(self.conn, self.now)["A"]["unreliable"])

    def test_closures_weigh_half(self):
        self.room("A", 60)
        for d in (1, 5, 9, 13, 17, 21):  # 6 closures in 4 weeks: 3 points, 0.75/week
            self.event("A", d, change="closure")
        self.room("B", 60)
        for d in range(1, 25, 3):        # 8 closures: 4 points, 1/week
            self.event("B", d, change="closure")
        s = reliability.scores(self.conn, self.now)
        self.assertEqual((s["A"]["closures"], s["A"]["per_week"], s["A"]["unreliable"]), (6, 0.75, False))
        self.assertTrue(s["B"]["unreliable"])
        self.assertEqual(s["B"]["reason"], "8 short-notice closures in the last 4 weeks")

    def test_bookings_and_closures_together(self):
        self.room("A", 2)                # judged on one week
        for d in (0, 1):
            self.event("A", d)
            self.event("A", d + 0.5, change="closure")
        self.room("B", 2)
        self.event("B", 0)
        self.event("B", 1, change="closure")
        self.event("B", 1, change="closure", max_lead_h=30)   # not short notice
        s = reliability.scores(self.conn, self.now)
        self.assertEqual(s["A"]["reason"], "2 short-notice bookings, 2 closures in the last week")
        self.assertEqual((s["B"]["short_notice"], s["B"]["closures"], s["B"]["unreliable"]), (1, 1, False))
        self.assertEqual(reliability.assess(1, 1, 4), (True, "1 short-notice booking, 4 closures in the last week"))
        self.assertEqual(reliability.assess(3, 1, 1), (True, "3 short-notice bookings, 1 closure in the last week"))

    def test_closures_found_in_one_fetch_are_one_closure(self):
        # A building closed Friday 13:30 to Monday: several intervals, one announcement.
        self.room("A", 60)
        for _ in range(3):
            self.event("A", 1, change="closure")
        self.assertEqual(reliability.scores(self.conn, self.now)["A"]["closures"], 1)

    def test_building_wide_closures_dont_flag_the_building_quickly(self):
        # Three short-notice closures of HG in two weeks hit every room once each: no room is flagged.
        for i in range(40):
            self.room(f"HG {i}", 60)
            for d in (2, 6, 11):
                self.event(f"HG {i}", d, change="closure")
        self.assertFalse(any(x["unreliable"] for x in reliability.scores(self.conn, self.now).values()))

    def test_annotate(self):
        rooms = reliability.annotate([{"id": "A"}, {"id": "B"}],
                                     {"A": {"unreliable": True, "reason": "x"}, "B": {"unreliable": False, "reason": None}})
        self.assertEqual([(r["unreliable"], r["unreliable_reason"]) for r in rooms], [(True, "x"), (False, None)])


class Migration(unittest.TestCase):
    def test_old_db_gets_the_column_and_table(self):
        import sqlite3
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        old = sqlite3.connect(db)
        old.execute("""CREATE TABLE rooms (id TEXT PRIMARY KEY, building TEXT NOT NULL, floor TEXT NOT NULL,
                       room TEXT NOT NULL, type TEXT, typecode TEXT, seats INTEGER, workplaces INTEGER, site TEXT,
                       uni TEXT NOT NULL DEFAULT 'ETH', campus TEXT, fetched_at TEXT, n_entries INTEGER)""")
        old.commit()
        old.close()
        conn = store.connect(db)
        self.assertIn("watch_since", {r[1] for r in conn.execute("PRAGMA table_info(rooms)")})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM booking_changes").fetchone()[0], 0)
        conn.close()


class Api(unittest.TestCase):
    def test_rooms_and_detail_carry_the_flag(self):
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        conn = store.connect(db)
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO rooms(id, building, floor, room, n_entries, watch_since) VALUES (?,?,?,?,1,?)",
                     (RID, "HG", "E", "1.2", (now - timedelta(days=60)).isoformat(timespec="seconds")))
        conn.execute("INSERT INTO rooms(id, building, floor, room, n_entries) VALUES ('HG E 3', 'HG', 'E', '3', 1)")
        conn.execute("INSERT INTO room_details(room_id, fetched_at, json) VALUES (?,?,?)",
                     (RID, now.isoformat(timespec="seconds"), json.dumps({"description": None, "info": None, "tabs": []})))
        for d in range(4):
            t = (now - timedelta(days=d)).isoformat(timespec="seconds")
            conn.execute("""INSERT INTO booking_changes(room_id, detected_at, prev_fetch, start, end, change, lead_s,
                            max_lead_s) VALUES (?,?,?,?,?,'added',3600,7200)""", (RID, t, t, "x", "y"))
        conn.commit()
        conn.close()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, object()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/rooms") as r:
                rooms = {x["id"]: x for x in json.load(r)["rooms"]}
            self.assertTrue(rooms[RID]["unreliable"])
            self.assertIn("4 short-notice bookings", rooms[RID]["unreliable_reason"])
            self.assertEqual((rooms["HG E 3"]["unreliable"], rooms["HG E 3"]["unreliable_reason"]), (False, None))
            with urllib.request.urlopen(base + "/api/roomdetail?id=HG%20E%201.2") as r:
                det = json.load(r)
            self.assertTrue(det["unreliable"])
            self.assertEqual(det["tabs"], [])
        finally:
            httpd.shutdown()


class Cli(unittest.TestCase):
    def test_now_marks_flagged_rooms(self):
        from freerooms.__main__ import now_lines
        conn = store.connect(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO rooms(id, building, floor, room, seats, type, n_entries, watch_since) VALUES (?,?,?,?,?,?,1,?)",
                     (RID, "HG", "E", "1.2", 40, "Seminar", (now - timedelta(days=60)).isoformat(timespec="seconds")))
        conn.execute("INSERT INTO rooms(id, building, floor, room, seats, type, n_entries) VALUES ('HG E 3','HG','E','3',20,'Seminar',1)")
        for d in range(4):
            t = (now - timedelta(days=d)).isoformat(timespec="seconds")
            conn.execute("""INSERT INTO booking_changes(room_id, detected_at, prev_fetch, start, end, change, lead_s,
                            max_lead_s) VALUES (?,?,?,?,?,'added',3600,7200)""", (RID, t, t, "x", "y"))
        conn.commit()
        lines = now_lines(conn, datetime(2026, 11, 23, 10))
        self.assertEqual(lines[0], "2 free rooms at Mon 23.11. 10:00")
        flagged = [x for x in lines if "often booked at short notice" in x]
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0].strip().startswith(RID))
        self.assertTrue(flagged[0].endswith("(often booked at short notice)"))
        conn.close()


ROOM = {"building": "HG", "floor": "E", "room": "1.2", "typecode": "3430", "locationName": "Zürich Zentrum"}


class FakeClient:
    def __init__(self, allocs):
        self.allocs = allocs

    def rooms(self):
        return [ROOM]

    def allocations(self, rid, start, end):
        return self.allocs


def alloc(start: datetime, hours=1, btype=2):
    return {"date_from": start.isoformat(timespec="seconds"),
            "date_to": (start + timedelta(hours=hours)).isoformat(timespec="seconds"),
            "belegungsserie": {"belegungstyp": btype, "veranstaltung": {"allocationTitle": "X", "findetStatt": 1}}}


class FetchHook(unittest.TestCase):
    def test_eth_fetch_records_a_short_notice_booking(self):
        conn = store.connect(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        base = datetime.now(TZ).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        tomorrow = datetime(base.year, base.month, base.day) + timedelta(days=1)
        night = alloc(tomorrow, hours=7, btype=8)
        with self.assertLogs("freerooms.fetch", "INFO"):
            fetch.run(conn, FakeClient([night]), days=7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM booking_changes").fetchone()[0], 0)
            fetch.run(conn, FakeClient([night, alloc(tomorrow + timedelta(hours=10))]), days=7)
        (change, max_lead), = conn.execute("SELECT change, max_lead_s FROM booking_changes").fetchall()
        self.assertEqual(change, "added")
        self.assertLess(max_lead, 2 * 24 * 3600)
        conn.close()


if __name__ == "__main__":
    unittest.main()
