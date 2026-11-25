"""A library made of floors without samples of its own: the floors' sum, trend and usual values."""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from freerooms import popular, store
from freerooms.availability import TZ
from freerooms.places import Place, Sample, add_samples, latest, upsert_places, utc_iso
from freerooms.web import make_handler

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def ts(minute: float) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat(timespec="seconds")


def lib_places(seats=(50, 50), source="t"):
    return [Place(id="t:lib", uni="ETH", name="Library", source="t", seats=sum(seats)),
            *(Place(id=f"t:lib:{i}", uni="ETH", name=f"Floor {i}", source=source, seats=n, parent_id="t:lib")
              for i, n in enumerate(seats))]


class SumFloors(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        upsert_places(self.conn, lib_places())
        self.now = T0 + timedelta(minutes=1)

    def counts(self, pid, counts, every=5, cap=50, end=0):
        """People counts every `every` minutes, the last at minute `end`."""
        n = len(counts)
        add_samples(self.conn, [Sample(pid, ts(end + (i - n + 1) * every), pct=c / cap * 100, count=c, max=cap)
                                for i, c in enumerate(counts)])

    def lib(self, now=None):
        return next(p["latest"] for p in latest(self.conn, now or self.now) if p["id"] == "t:lib")

    def test_sum_and_trend(self):
        self.counts("t:lib:0", [10, 12, 14, 16, 18])
        self.counts("t:lib:1", [20, 21, 22, 23, 24])
        s = self.lib()
        self.assertEqual((s["count"], s["max"], s["pct"], s["status"]), (42, 100, 42.0, "green"))
        self.assertEqual((s["trend"], s["trend_source"]), (1, "computed"))   # 33 -> 42 of 100 in the last 15 min
        self.assertEqual((s["floors"], s["age_s"], s["closed"]), (["t:lib:0", "t:lib:1"], 60, False))

    def test_dead_band_applies_to_the_whole_library(self):
        # Each floor alone moves 8 of 50 (16 %), but one fills while the other empties: steady overall.
        self.counts("t:lib:0", [10, 12, 14, 16, 18])
        self.counts("t:lib:1", [30, 28, 26, 24, 22])
        floors = {p["id"]: p["latest"] for p in latest(self.conn, self.now)}
        self.assertEqual((floors["t:lib:0"]["trend"], floors["t:lib:1"]["trend"]), (1, -1))
        self.assertEqual(floors["t:lib"]["trend"], 0)

    def test_floors_sampled_at_different_times(self):
        # Floor 1 samples two minutes later; each time uses every floor's newest sample up to then.
        self.counts("t:lib:0", [10, 10, 10, 10, 10], end=-2)
        self.counts("t:lib:1", [10, 13, 16, 19, 22])
        s = self.lib()
        self.assertEqual((s["count"], s["trend"], s["age_s"]), (32, 1, 180))

    def test_no_trend_without_history(self):
        self.counts("t:lib:0", [10])
        self.counts("t:lib:1", [20])
        s = self.lib()
        self.assertEqual((s["count"], s["trend"], s["trend_source"]), (30, None, None))

    def test_stale_or_closed_floors_left_out(self):
        self.counts("t:lib:0", [10, 12, 14, 16, 18])
        self.counts("t:lib:1", [20, 20], end=-40)                  # 41 min old: stale
        s = self.lib()
        self.assertEqual((s["count"], s["max"], s["floors"]), (18, 50, ["t:lib:0"]))
        add_samples(self.conn, [Sample("t:lib:0", ts(0.5), closed=True)])
        self.assertIsNone(self.lib())

    def test_parent_with_own_samples_is_left_alone(self):
        self.counts("t:lib:0", [10, 12, 14, 16, 18])
        add_samples(self.conn, [Sample("t:lib", ts(0), pct=80.0, count=80, max=100, trend=-1)])
        s = self.lib()
        self.assertEqual((s["pct"], s["trend_source"]), (80.0, "source"))
        self.assertNotIn("floors", s)

    def test_pct_only_floors_weighted_by_seats(self):
        conn = self.conn = store.connect(":memory:")
        upsert_places(conn, lib_places(seats=(100, 300), source="uzh_ub"))
        for pid, pcts in (("t:lib:0", [10, 10, 10, 20, 20]), ("t:lib:1", [50, 50, 50, 50, 50])):
            add_samples(conn, [Sample(pid, ts((i - 4) * 5), pct=p) for i, p in enumerate(pcts)])
        s = self.lib()
        self.assertEqual((s["count"], s["max"], s["pct"]), (170, 400, 42.5))
        # 40 % -> 42.5 %: inside the dead band, though floor 0 alone moved two 5 % steps.
        self.assertEqual(s["trend"], 0)


class SumUsual(unittest.TestCase):
    def usual(self, weeks, slots):
        return {"weekday": "Wednesday", "slot_min": 30, "window_weeks": 6, "min_weeks": 3, "weeks": weeks,
                "slots": [{"start": a, "pct": p, "weeks": w} for a, p, w in slots], "fills_by": None}

    def test_day_seat_weighted_weeks_minimum(self):
        g = self.usual(5, [("09:00", 40.0, 5), ("09:30", 95.0, 4), ("10:00", 100.0, 5)])
        j = self.usual(3, [("09:30", 70.0, 3), ("10:00", 100.0, 3)])          # no 09:00 slot
        u = popular.sum_usual_day([g, j], [300, 100])
        self.assertEqual(u["slots"], [{"start": "09:30", "pct": 88.8, "weeks": 3},
                                      {"start": "10:00", "pct": 100.0, "weeks": 3}])
        self.assertEqual((u["weeks"], u["weekday"], u["fills_by"]), (3, "Wednesday", "10:00"))

    def test_now(self):
        a, b = {"pct": 20.0, "weeks": 4, "slot": "09:00"}, {"pct": 60.0, "weeks": 3, "slot": "09:00"}
        self.assertEqual(popular.sum_usual_now([a, b], [68, 34]), {"pct": 33.3, "weeks": 3, "slot": "09:00"})
        self.assertIsNone(popular.sum_usual_now([a, None], [68, 34]))
        self.assertIsNone(popular.sum_usual_now([], []))


class Api(unittest.TestCase):
    def test_places_and_place(self):
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        conn = store.connect(db)
        upsert_places(conn, lib_places(seats=(60, 20)))
        wed = date(2026, 11, 18)
        now = datetime(2026, 11, 18, 10, 10, tzinfo=TZ)
        rows = []
        for k in (1, 2, 3):   # three past Wednesdays at 10:00
            t = utc_iso(datetime(*(wed - timedelta(weeks=k)).timetuple()[:3], 10, 0, tzinfo=TZ))
            rows += [Sample("t:lib:0", t, pct=50.0, count=30, max=60), Sample("t:lib:1", t, pct=10.0 * k, count=2 * k, max=20)]
        for m in (-20, -15, -10, -5, 0):   # today: floor 0 filling up
            t = utc_iso(now + timedelta(minutes=m))
            n = 50 + m
            rows += [Sample("t:lib:0", t, pct=n / 60 * 100, count=n, max=60), Sample("t:lib:1", t, pct=20.0, count=4, max=20)]
        add_samples(conn, rows)
        conn.commit()
        conn.close()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        get = lambda path: json.load(urllib.request.urlopen(base + path))
        try:
            with mock.patch("freerooms.places.datetime", wraps=datetime) as pdt, \
                    mock.patch("freerooms.popular.datetime", wraps=datetime) as udt:
                pdt.now.return_value = udt.now.return_value = now.astimezone(timezone.utc)
                ps = {p["id"]: p for p in get("/api/places")["places"]}
                d = get("/api/place?id=t:lib&date=2026-11-18")
            lib = ps["t:lib"]
            self.assertEqual((lib["latest"]["count"], lib["latest"]["max"]), (54, 80))
            self.assertEqual((lib["latest"]["trend"], lib["latest"]["trend_source"]), (1, "computed"))
            # Floor 0 usually 50 % of 60, floor 1 usually 20 % of 20 -> 34 of 80 = 42.5 %.
            self.assertEqual(lib["usual_now"], {"pct": 42.5, "weeks": 3, "slot": "10:00"})
            self.assertEqual(d["place"]["usual"]["slots"], [{"start": "10:00", "pct": 42.5, "weeks": 3}])
            self.assertEqual(d["place"]["usual"]["weeks"], 3)
            self.assertEqual(d["place"]["latest"]["floors"], ["t:lib:0", "t:lib:1"])
            self.assertEqual(d["kids"][1]["usual"]["slots"], [{"start": "10:00", "pct": 20.0, "weeks": 3}])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
