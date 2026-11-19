import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from freerooms import live, store
from freerooms.places import latest
from freerooms.sources import icbp

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def by_id(samples):
    return {s.place_id: s for s in samples}


class Places(unittest.TestCase):
    def test_library_and_floors(self):
        ps = icbp.parse_places(load("icbp_closed.json"))
        self.assertEqual([p.id for p in ps], ["icbp:HCI G 5.1", "icbp:G", "icbp:H", "icbp:J"])
        lib = ps[0]
        self.assertEqual((lib.uni, lib.campus, lib.building, lib.seats, lib.parent_id), ("ETH", "Hönggerberg", "HCI", 170, None))
        self.assertTrue(all(p.parent_id == lib.id and p.source == "icbp" for p in ps[1:]))
        self.assertEqual([p.seats for p in ps[1:]], [68, 68, 34])
        self.assertEqual(ps[3].name, "ICBP Infozentrum – floor J")

    def test_floors_are_discovered(self):
        raw = {"overall_max": 10, "overall": 4, "K": 4, "K_max": 10, "trend": 0, "junk_max": "x"}
        self.assertEqual(icbp.floors(raw), ["K"])
        self.assertEqual([p.id for p in icbp.parse_places(raw)], ["icbp:HCI G 5.1", "icbp:K"])

    def test_rejects_garbage(self):
        for bad in ([], {"error": "x"}, {"overall_max": None}):
            with self.assertRaises(ValueError):
                icbp.parse_places(bad)
            with self.assertRaises(ValueError):
                icbp.parse_samples(bad)


class Samples(unittest.TestCase):
    def test_closed(self):
        ss = by_id(icbp.parse_samples(load("icbp_closed.json")))
        lib = ss["icbp:HCI G 5.1"]
        self.assertEqual(lib.ts, "2026-11-19T20:39:29+00:00")
        self.assertEqual((lib.count, lib.max, lib.pct, lib.trend, lib.closed, lib.status), (0, 170, 0, 0, True, "green"))
        self.assertTrue(all(s.closed and s.ts == lib.ts for s in ss.values()))

    def test_open(self):
        ss = by_id(icbp.parse_samples(load("icbp_open.json")))
        lib, g, j = ss["icbp:HCI G 5.1"], ss["icbp:G"], ss["icbp:J"]
        self.assertEqual(lib.ts, "2026-11-19T13:05:00+00:00")
        self.assertEqual((lib.count, lib.pct, lib.trend, lib.closed, lib.status), (118.67, 69.8, -1, False, "yellow"))
        self.assertEqual((g.count, g.max, g.pct, g.status, g.trend), (50.24, 68, 73.9, "yellow", None))
        self.assertEqual((j.pct, j.status), (95.6, "red"))
        self.assertEqual(ss["icbp:H"].status, "yellow")

    def test_no_data(self):
        for s in icbp.parse_samples(load("icbp_no_data.json")):
            self.assertEqual((s.status, s.pct, s.count, s.trend), ("unknown", None, None, None))
            self.assertIsNotNone(s.max)

    def test_edge_values(self):
        raw = {"overall_max": 0, "overall": 5, "G": 80, "G_max": 68, "H": -1, "H_max": 68, "trend": 3}
        ss = by_id(icbp.parse_samples(raw, now=datetime(2026, 11, 19, 12, tzinfo=timezone.utc)))
        lib = ss["icbp:HCI G 5.1"]
        self.assertEqual((lib.pct, lib.status, lib.trend, lib.ts), (None, "unknown", 1, "2026-11-19T12:00:00+00:00"))
        self.assertEqual((ss["icbp:G"].pct, ss["icbp:G"].status), (100, "red"))
        self.assertEqual(ss["icbp:H"].pct, 0)

    def test_overall_falls_back_to_floor_sum(self):
        raw = {"overall_max": 170, "G": 10, "G_max": 68, "H": 7.5, "H_max": 68, "trend": -0.2}
        lib = by_id(icbp.parse_samples(raw))["icbp:HCI G 5.1"]
        self.assertEqual((lib.count, lib.trend), (17.5, -1))

    def test_thresholds(self):
        self.assertEqual([icbp.status_for(p) for p in (None, 0, 59.9, 60, 84.9, 85, 100)],
                         ["unknown", "green", "green", "yellow", "yellow", "red", "red"])


class Fake:
    def get_json(self, url):
        assert url == icbp.URL
        return load("icbp_open.json")


class Source(unittest.TestCase):
    def test_registered_and_stores(self):
        (src,) = [s for s in live.discover() if s.key == "icbp"]
        self.assertEqual((src.every, src.max_age), (300, 900))
        conn = store.connect(":memory:")
        self.assertEqual(live.run_source(conn, src, Fake()), {"places": 4, "samples": 4, "new_samples": 4})
        rows = {p["id"]: p for p in latest(conn)}
        self.assertEqual(rows["icbp:G"]["parent_id"], "icbp:HCI G 5.1")
        self.assertEqual(rows["icbp:HCI G 5.1"]["hours"], icbp.HOURS)
        self.assertEqual(rows["icbp:HCI G 5.1"]["latest"]["count"], 118.67)


if __name__ == "__main__":
    unittest.main()
