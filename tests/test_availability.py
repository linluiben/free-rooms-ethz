import unittest
from datetime import datetime

from freerooms import store
from freerooms.availability import Interval, building_hours, open_hours, query, room_status
from freerooms.fetch import load_allowlist, normalize, select_rooms

D = datetime.fromisoformat


def day(*extra):
    """A normal weekday: closed 00-08 and 22-23:59, plus extra (start, end, kind)."""
    ivs = [Interval(D("2026-11-11T00:00"), D("2026-11-11T08:00"), "closed"),
           Interval(D("2026-11-11T22:00"), D("2026-11-11T23:59"), "closed"),
           Interval(D("2026-11-12T00:00"), D("2026-11-12T08:00"), "closed")]
    return ivs + [Interval(D(s), D(e), k, "event") for s, e, k in extra]


class RoomStatus(unittest.TestCase):
    def test_free_until_next_event(self):
        st = room_status(day(("2026-11-11T10:00", "2026-11-11T12:00", "busy")), D("2026-11-11T09:00"), True)
        self.assertEqual((st.state, st.until), ("free", D("2026-11-11T10:00")))
        self.assertEqual(st.minutes(D("2026-11-11T09:00")), 60)

    def test_free_until_closing(self):
        st = room_status(day(), D("2026-11-11T15:00"), True)
        self.assertEqual((st.state, st.until, st.next_kind), ("free", D("2026-11-11T22:00"), "closed"))

    def test_busy_merges_back_to_back(self):
        ivs = day(("2026-11-11T10:00", "2026-11-11T12:00", "busy"),
                  ("2026-11-11T12:00", "2026-11-11T14:00", "busy"))
        st = room_status(ivs, D("2026-11-11T11:00"), True)
        self.assertEqual((st.state, st.until), ("busy", D("2026-11-11T14:00")))

    def test_closed_spans_midnight(self):
        # 22:00-23:59 then 00:00-08:00 should read as one closure until 08:00.
        st = room_status(day(), D("2026-11-11T23:00"), True)
        self.assertEqual((st.state, st.until), ("closed", D("2026-11-12T08:00")))

    def test_boundaries(self):
        ivs = day(("2026-11-11T10:00", "2026-11-11T12:00", "busy"))
        self.assertEqual(room_status(ivs, D("2026-11-11T10:00"), True).state, "busy")
        self.assertEqual(room_status(ivs, D("2026-11-11T12:00"), True).state, "free")

    def test_no_data_is_unknown(self):
        self.assertEqual(room_status([], D("2026-11-11T10:00"), False).state, "unknown")

    def test_cancelled_never_blocks(self):
        ivs = day(("2026-11-11T14:00", "2026-11-11T18:00", "cancelled"),
                  ("2026-11-11T19:00", "2026-11-11T20:00", "busy"))
        st = room_status(ivs, D("2026-11-11T15:00"), True)
        self.assertEqual((st.state, st.until, st.next_kind), ("free", D("2026-11-11T19:00"), "busy"))
        st = room_status(ivs, D("2026-11-11T12:00"), True)
        self.assertEqual((st.state, st.until), ("free", D("2026-11-11T19:00")))

    def test_outside_window_is_unknown(self):
        st = room_status(day(), D("2026-11-20T10:00"), True, window_end=D("2026-11-19T00:00"))
        self.assertEqual(st.state, "unknown")


class OpeningHours(unittest.TestCase):
    d0 = D("2026-11-11T00:00")

    def closures(self, *pairs):
        return [(D(s), D(e)) for s, e in pairs]

    def test_normal_day(self):
        c = self.closures(("2026-11-11T00:00", "2026-11-11T07:00"), ("2026-11-11T22:00", "2026-11-11T23:59"))
        self.assertEqual(open_hours(c, self.d0), (D("2026-11-11T07:00"), D("2026-11-11T22:00")))

    def test_closure_from_previous_day_is_clipped(self):
        c = self.closures(("2026-11-10T20:00", "2026-11-11T08:00"), ("2026-11-11T20:00", "2026-11-12T08:00"))
        self.assertEqual(open_hours(c, self.d0), (D("2026-11-11T08:00"), D("2026-11-11T20:00")))

    def test_closed_all_day_and_unknown(self):
        self.assertEqual(open_hours(self.closures(("2026-11-11T00:00", "2026-11-11T23:59")), self.d0), "closed")
        self.assertIsNone(open_hours([], self.d0))

    def test_building_majority(self):
        conn = store.connect(":memory:")
        rows = [("A1", "07:00", "22:00"), ("A2", "07:00", "22:00"), ("A3", "06:00", "23:00")]
        for rid, o, c in rows:
            conn.execute("INSERT INTO rooms(id, building, floor, room) VALUES (?, 'A', 'F', ?)", (rid, rid))
            conn.executemany("INSERT INTO intervals(room_id, start, end, kind) VALUES (?,?,?,'closed')",
                             [(rid, "2026-11-11T00:00:00", f"2026-11-11T{o}:00"), (rid, f"2026-11-11T{c}:00", "2026-11-11T23:59:00")])
        conn.execute("INSERT INTO rooms(id, building, floor, room) VALUES ('B1', 'B', 'F', '1')")  # no closures
        self.assertEqual(building_hours(conn, D("2026-11-11T12:00")), {"A": {"open": "07:00", "close": "22:00"}})


class Query(unittest.TestCase):
    def test_cancelled_shown_in_timeline_but_room_free(self):
        conn = store.connect(":memory:")
        conn.execute("INSERT INTO rooms(id, building, floor, room, n_entries) VALUES ('HCI F 8', 'HCI', 'F', '8', 3)")
        conn.executemany("INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES ('HCI F 8',?,?,?,?,?)", [
            ("2026-11-19T00:00:00", "2026-11-19T08:00:00", "closed", None, 8),
            ("2026-11-19T14:00:00", "2026-11-19T18:00:00", "cancelled", "Unveiling Habitats", 13),
            ("2026-11-19T22:00:00", "2026-11-19T23:59:00", "closed", None, 8)])
        conn.execute("INSERT INTO rooms(id, building, floor, room, n_entries) VALUES ('X 1', 'X', 'F', '1', 1)")
        conn.execute("INSERT INTO intervals(room_id, start, end, kind, title, raw_type) VALUES "
                     "('X 1', '2026-11-19T14:00:00', '2026-11-19T18:00:00', 'cancelled', 'Gone', 13)")
        r, x = query(conn, D("2026-11-19T15:00"))
        self.assertEqual((r["state"], r["until"], r["reason"], r["hours_known"]), ("free", "2026-11-19T22:00", None, True))
        self.assertIn(["14:00", "18:00", "Unveiling Habitats", "cancelled"], r["timeline"])
        # Only cancelled entries: free with no end, and no closure data -> opening hours unknown.
        self.assertEqual((x["state"], x["until"], x["hours_known"]), ("free", None, False))
        self.assertEqual(building_hours(conn, D("2026-11-19T15:00")), {"HCI": {"open": "08:00", "close": "22:00"}})


def raw(btype, findet=1, title="X"):
    v = None if btype in (7, 8) else {"allocationTitle": title, "findetStatt": findet}
    return {"date_from": "2026-11-11T10:00:00", "date_to": "2026-11-11T12:00:00",
            "belegungsserie": {"belegungstyp": btype, "veranstaltung": v}}


class Normalize(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(normalize(raw(2))[2], "busy")
        self.assertEqual(normalize(raw(5))[2], "busy")
        self.assertEqual(normalize(raw(11))[2], "busy")
        self.assertEqual(normalize(raw(8))[2], "closed")
        self.assertEqual(normalize(raw(7))[2], "closed")

    def test_cancelled_kept_as_cancelled(self):
        self.assertEqual(normalize(raw(13, findet=0, title="Unveiling Habitats"))[2:],
                         ("cancelled", "Unveiling Habitats", 13))
        self.assertEqual(normalize(raw(2, findet=0))[2], "cancelled")
        self.assertEqual(normalize(raw(13))[2], "cancelled")

    def test_unknown_type_is_busy(self):
        with self.assertLogs("freerooms.fetch", "WARNING"):
            self.assertEqual(normalize(raw(99))[2], "busy")


class Allowlist(unittest.TestCase):
    def test_only_centrally_managed_rooms(self):
        allow = load_allowlist()
        self.assertIn("HG E 1.2", allow)
        self.assertNotIn("HG G 19.1", allow)   # department room, bookings not public
        rooms = [{"building": "HG", "floor": "E", "room": "1.2", "typecode": "3430", "locationName": "Zürich Zentrum"},
                 {"building": "HG", "floor": "G", "room": "19.1", "typecode": "3420", "locationName": "Zürich Zentrum"}]
        self.assertEqual([r["room"] for r in select_rooms(rooms, allowlist=allow)], ["1.2"])


if __name__ == "__main__":
    unittest.main()
