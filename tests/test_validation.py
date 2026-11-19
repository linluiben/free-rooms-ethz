import json
import unittest
from datetime import datetime
from pathlib import Path

from freerooms import fetch, store
from freerooms.availability import query

FIXTURES = Path(__file__).with_name("fixtures")
ROOM = {"building": "HG", "floor": "E", "room": "1.2", "typecode": "3430", "locationName": "Zürich Zentrum"}


def good(**over):
    a = {"date_from": "2026-11-11T10:00:00", "date_to": "2026-11-11T12:00:00",
         "belegungsserie": {"belegungstyp": 2, "veranstaltung": {"allocationTitle": "X", "findetStatt": 1}}}
    a.update(over)
    return a


class FakeClient:
    def __init__(self, allocs):
        self.allocs = allocs

    def rooms(self):
        return [ROOM]

    def allocations(self, rid, start, end):
        return self.allocs


class Validate(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(fetch.validate(good()))
        self.assertIsNone(fetch.validate(good(date_to="2026-11-11T10:00:00")))  # zero length is harmless
        self.assertIsNone(fetch.validate(good(belegungsserie={"belegungstyp": 8, "veranstaltung": None})))

    def test_malformed(self):
        cases = [
            "not a dict",
            None,
            {k: v for k, v in good().items() if k != "date_from"},
            {k: v for k, v in good().items() if k != "date_to"},
            good(date_from=None),
            good(date_from=20261111),
            good(date_to="tomorrow"),
            good(date_to="2026-13-01T10:00:00"),
            good(date_to="2026-11-11T09:00:00"),          # ends before it starts
            good(date_from="2026-11-11T10:00:00+01:00"),  # we expect naive local time
            good(belegungsserie=[2]),
            good(belegungsserie={"belegungstyp": 2, "veranstaltung": "Lecture"}),
        ]
        for a in cases:
            with self.subTest(a=a):
                self.assertIsNotNone(fetch.validate(a))


class FetchMarksUnknown(unittest.TestCase):
    t = datetime.fromisoformat("2026-11-11T11:00")

    def fetch(self, allocs):
        conn = store.connect(":memory:")
        stats = fetch.run(conn, FakeClient(allocs), buildings=("HG",))
        (room,) = query(conn, self.t)
        return conn, stats, room

    def test_good_entries_give_busy(self):
        _, stats, room = self.fetch([good()])
        self.assertEqual((room["state"], stats["malformed"]), ("busy", 0))

    def test_fixture_with_missing_date_to_is_unknown_not_free(self):
        allocs = json.loads((FIXTURES / "allocations_malformed.json").read_text(encoding="utf-8"))
        with self.assertLogs("freerooms.fetch", "WARNING") as logs:
            conn, stats, room = self.fetch(allocs)
        self.assertEqual(room["state"], "unknown")
        self.assertEqual(stats["malformed"], 1)
        self.assertTrue(any("malformed" in m and "date_to" in m for m in logs.output))
        # Valid entries are still kept for the timeline.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM intervals").fetchone()[0], 2)

    def test_reversed_interval_is_unknown(self):
        with self.assertLogs("freerooms.fetch", "WARNING"):
            _, _, room = self.fetch([good(date_to="2026-11-11T09:00:00")])
        self.assertEqual(room["state"], "unknown")

    def test_non_list_response_is_unknown(self):
        with self.assertLogs("freerooms.fetch", "WARNING"):
            _, stats, room = self.fetch({"error": "oops"})
        self.assertEqual((room["state"], stats["malformed"]), ("unknown", 1))


if __name__ == "__main__":
    unittest.main()
