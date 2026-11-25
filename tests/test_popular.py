import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from freerooms import popular, store
from freerooms.availability import TZ
from freerooms.places import Place, Sample, add_samples, upsert_places, utc_iso
from freerooms.web import make_handler

WED = date(2026, 11, 25)                       # a Wednesday
NOW = datetime(2026, 11, 25, 9, 10, tzinfo=TZ)  # Wednesday morning, Zurich


def at(day: date, hh: int, mm: int) -> str:
    """UTC ISO of a Zurich wall-clock time."""
    return utc_iso(datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ))


class Popular(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        upsert_places(self.conn, [Place(id="t:a", uni="ETH", name="A", source="t"),
                                  Place(id="t:b", uni="ETH", name="B", source="t")])

    def add(self, *samples):
        add_samples(self.conn, list(samples))

    def week(self, k: int) -> date:
        return WED - timedelta(weeks=k)

    def test_median_of_weekly_medians(self):
        # Four past Wednesdays, 10:00-10:29: weekly medians 20, 40, 60, 80 -> usual 50.
        # Week 4 has many more samples; it still counts once.
        for k, vals in ((1, [10, 20, 30]), (2, [40]), (3, [60, 60]), (4, [80] * 6)):
            self.add(*(Sample("t:a", at(self.week(k), 10, 5 * i), pct=v) for i, v in enumerate(vals)))
        u = popular.usual_day(self.conn, ["t:a", "t:b"], WED, now=NOW)["t:a"]
        self.assertEqual(u["weekday"], "Wednesday")
        self.assertEqual(u["weeks"], 4)
        self.assertEqual(u["slots"], [{"start": "10:00", "pct": 50.0, "weeks": 4}])
        self.assertEqual(popular.usual_day(self.conn, ["t:b"], WED, now=NOW)["t:b"]["slots"], [])

    def test_closed_and_unknown_samples_are_skipped(self):
        for k in (1, 2, 3):
            self.add(Sample("t:a", at(self.week(k), 14, 0), pct=30.0),
                     Sample("t:a", at(self.week(k), 14, 10), pct=99.0, closed=True),
                     Sample("t:a", at(self.week(k), 14, 20), status="unknown"))
        # A fourth week that was closed all afternoon doesn't count as a week with data.
        self.add(Sample("t:a", at(self.week(4), 14, 0), pct=0.0, closed=True))
        u = popular.usual_day(self.conn, ["t:a"], WED, now=NOW)["t:a"]
        self.assertEqual(u["slots"], [{"start": "14:00", "pct": 30.0, "weeks": 3}])
        self.assertEqual(u["weeks"], 3)

    def test_too_little_history(self):
        for k in (1, 2):
            self.add(Sample("t:a", at(self.week(k), 10, 0), pct=50.0))
        u = popular.usual_day(self.conn, ["t:a"], WED, now=NOW)["t:a"]
        self.assertEqual((u["weeks"], u["slots"], u["min_weeks"]), (2, [], 3))
        # Per slot: three weeks at 10:00 but only one at 11:00 -> only 10:00 is shown.
        self.add(Sample("t:a", at(self.week(3), 10, 0), pct=50.0), Sample("t:a", at(self.week(3), 11, 0), pct=90.0))
        u = popular.usual_day(self.conn, ["t:a"], WED, now=NOW)["t:a"]
        self.assertEqual([s["start"] for s in u["slots"]], ["10:00"])

    def test_window_other_weekdays_and_today_ignored(self):
        for k in (1, 2, 3):
            self.add(Sample("t:a", at(self.week(k), 10, 0), pct=20.0))
        self.add(Sample("t:a", at(self.week(7), 10, 0), pct=100.0),           # older than 6 weeks
                 Sample("t:a", at(self.week(1) + timedelta(days=1), 10, 0), pct=100.0),  # a Thursday
                 Sample("t:a", at(WED, 8, 0), pct=100.0))                      # today is not "usual"
        u = popular.usual_day(self.conn, ["t:a"], WED, now=NOW)["t:a"]
        self.assertEqual(u["slots"], [{"start": "10:00", "pct": 20.0, "weeks": 3}])

    def test_zurich_slots_across_dst(self):
        # Summer time ended on Sunday 2026-10-25. 08:00 Zurich is 06:00 UTC before, 07:00 UTC after;
        # both land in the 08:00 slot of the Mondays around it.
        mon = date(2026, 11, 2)
        for d in (date(2026, 10, 26), date(2026, 10, 19), date(2026, 10, 12)):
            self.add(Sample("t:a", at(d, 8, 0), pct=40.0))
        self.assertEqual(at(date(2026, 10, 19), 8, 0), "2026-10-19T06:00:00+00:00")
        self.assertEqual(at(date(2026, 10, 26), 8, 0), "2026-10-26T07:00:00+00:00")
        u = popular.usual_day(self.conn, ["t:a"], mon, now=datetime(2026, 11, 2, 12, tzinfo=TZ))["t:a"]
        self.assertEqual(u["slots"], [{"start": "08:00", "pct": 40.0, "weeks": 3}])
        # The 25-hour Sunday itself: 23:30 Zurich on 2026-10-25 is 22:30 UTC and still that Sunday.
        sun = date(2026, 11, 15)
        for d in (date(2026, 10, 25), date(2026, 11, 1), date(2026, 11, 8)):
            self.add(Sample("t:a", at(d, 23, 30), pct=10.0), Sample("t:a", at(d, 0, 10), pct=5.0))
        u = popular.usual_day(self.conn, ["t:a"], sun, now=datetime(2026, 11, 15, 12, tzinfo=TZ))["t:a"]
        self.assertEqual(u["slots"], [{"start": "00:00", "pct": 5.0, "weeks": 3},
                                      {"start": "23:30", "pct": 10.0, "weeks": 3}])

    def test_fills_by(self):
        for k in (1, 2, 3):
            self.add(Sample("t:a", at(self.week(k), 9, 0), pct=60.0),
                     Sample("t:a", at(self.week(k), 10, 30), pct=92.0 + k),
                     Sample("t:a", at(self.week(k), 11, 0), pct=97.0))
        self.assertEqual(popular.usual_day(self.conn, ["t:a"], WED, now=NOW)["t:a"]["fills_by"], "10:30")
        self.add(Sample("t:b", at(self.week(1), 9, 0), pct=60.0))
        self.assertIsNone(popular.usual_day(self.conn, ["t:b"], WED, now=NOW)["t:b"]["fills_by"])

    def test_usual_now(self):
        # NOW is 09:10 -> slot 09:00-09:29 of the past Wednesdays only.
        for k, v in ((1, 30.0), (2, 50.0), (3, 70.0)):
            self.add(Sample("t:a", at(self.week(k), 9, 25), pct=v), Sample("t:a", at(self.week(k), 9, 30), pct=100.0))
        self.add(Sample("t:b", at(self.week(1), 9, 0), pct=10.0))
        u = popular.usual_now(self.conn, ["t:a", "t:b"], now=NOW)
        self.assertEqual(u, {"t:a": {"pct": 50.0, "weeks": 3, "slot": "09:00"}})
        self.assertEqual(popular.slot_key(NOW), ("2026-11-25", 18))
        self.assertEqual(popular.slot_key(NOW.astimezone(timezone.utc)), ("2026-11-25", 18))


class Api(unittest.TestCase):
    def test_place_and_places_carry_usual(self):
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        conn = store.connect(db)
        upsert_places(conn, [Place(id="t:lib", uni="ETH", name="Lib", source="t"),
                             Place(id="t:lib:G", uni="ETH", name="Lib G", source="t", parent_id="t:lib")])
        day = date(2026, 11, 18)
        add_samples(conn, [Sample("t:lib", at(day - timedelta(weeks=k), 10, 0), pct=10.0 * k) for k in (1, 2, 3)])
        conn.commit()
        conn.close()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/place?id=t:lib&date=2026-11-18") as r:
                d = json.load(r)
            self.assertEqual(d["place"]["usual"]["slots"], [{"start": "10:00", "pct": 20.0, "weeks": 3}])
            self.assertEqual(d["place"]["usual"]["weeks"], 3)
            self.assertEqual(d["kids"][0]["usual"]["weeks"], 0)
            with urllib.request.urlopen(base + "/api/places") as r:
                self.assertTrue(all("usual_now" in p for p in json.load(r)["places"]))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
