import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from freerooms import live, store
from freerooms.places import latest
from freerooms.sources import eth_studyspaces as ss

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 19, 20, 43, 27, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeHttp:
    def get_json(self, url):
        return load({ss.STUDYSPACES_URL: "eth_studyspaces.json",
                     ss.AVAILABILITY_URL: "eth_availability.json"}[url])


class Places(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.places = {p.id: p for p in ss.parse_places(load("eth_studyspaces.json"), NOW)}

    def test_library(self):
        p = self.places["eth_studyspaces:HGH30.1"]
        self.assertEqual((p.uni, p.source, p.campus, p.building, p.seats),
                         ("ETH", "eth_studyspaces", "Zürich Zentrum", "HG", 150))
        self.assertEqual(p.name, "ETH-Bibliothek, Lesesaal (HG H 30.1)")
        self.assertEqual(p.url, "https://ethz.ch/en/utils/location.html.html?building=HG&floor=H&room=30.1")
        h = p.hours
        self.assertEqual(h["week"]["mon"], [["08:00", "22:00"]])
        self.assertEqual(h["week"]["sun"], [["08:00", "17:00"]])
        self.assertEqual(h["meals"], {})
        self.assertEqual((h["state"], h["is_open"], h["access_24h"], h["sensor"]), ("open", True, False, True))
        self.assertIn("ETH Day", h["notice"]["en"])
        self.assertEqual(h["next_change"], "2026-11-19T23:00:00+00:00")
        self.assertEqual(h["as_of"], "2026-11-19T20:43:00+00:00")
        self.assertTrue(all(set(f) == {"key", "label"} for f in p.features))

    def test_mensa_with_two_meal_times(self):
        h = self.places["eth_studyspaces:MMB79"].hours
        self.assertEqual(h["meals"]["fri"], [["11:15", "14:00"], ["17:30", "19:30"]])
        self.assertNotIn("sat", h["meals"])
        self.assertEqual(h["week"]["sat"], [])
        self.assertEqual(h["next_open_date"], "2026-11-20")
        self.assertEqual(h["lines"][-1], "Learning environment outside meal times")

    def test_basel_and_single_days(self):
        self.assertEqual(self.places["eth_studyspaces:BSSK14"].campus, "Basel")
        h = self.places["eth_studyspaces:HILE19.1"].hours
        self.assertEqual((h["week"]["sat"], h["week"]["sun"]), ([["08:00", "12:00"]], []))
        self.assertEqual(self.places["eth_studyspaces:HCIF30.0017"].hours["week"]["sun"], [["10:00", "16:00"]])

    def test_24h_space_and_unknown_seats(self):
        p = self.places["eth_studyspaces:CHND8"]
        self.assertTrue(p.hours["access_24h"])
        self.assertIn(ss.FEATURE_24H, p.features)
        self.assertIsNone(p.seats)  # 0 in the feed
        self.assertEqual(p.name, "Grüner Boden (CHN D 8)")
        self.assertEqual(self.places["eth_studyspaces:HILE19.1"].name, "Lernraum HIL E 19.1")
        self.assertIsNone(self.places["eth_studyspaces:MMB79"].hours["notice"])

    def test_parse_hours_edge_cases(self):
        week, meals = ss.parse_hours(["Sat - Sun: Closed", "Mon - Fri: 6.30 - 22.00", "Sun - Mon: 09.00 - 10.00",
                                      "Meal time: Mon - Fri: 11.30 - 13.30", "Opening hours vary", "Tue: soon"])
        self.assertEqual(week["mon"], [["06:30", "22:00"], ["09:00", "10:00"]])
        self.assertEqual(week["sun"], [["09:00", "10:00"]])
        self.assertEqual(week["sat"], [])
        self.assertEqual(meals["wed"], [["11:30", "13:30"]])
        self.assertEqual(set(week), set(ss.DAYS))
        self.assertEqual(ss.parse_hours(None), ({}, {}))


class Samples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = {s.place_id: s for s in ss.parse_samples(load("eth_availability.json"), NOW)}

    def test_sensor_room(self):
        s = self.samples["eth_studyspaces:HGH30.1"]
        self.assertEqual((s.status, s.count, s.max, s.trend, s.closed), ("green", 32, 150, 0, False))
        self.assertEqual(s.pct, 21.3)  # 32/150; ETH itself shows 20 (steps of 20)
        self.assertEqual(s.ts, "2026-11-19T20:40:20+00:00")

    def test_crafted_busy_mensa(self):
        s = self.samples["eth_studyspaces:HPRD70.2"]
        self.assertEqual((s.status, s.pct, s.trend), ("red", 79.2, 1))
        self.assertEqual(s.ts, "2026-11-19T11:05:11+00:00")

    def test_no_data_is_unknown_at_fetch_time(self):
        s = self.samples["eth_studyspaces:CHND8"]
        self.assertEqual((s.status, s.pct, s.count, s.trend), ("unknown", None, None, None))
        self.assertEqual(s.ts, "2026-11-19T20:43:00+00:00")

    def test_hci_g_5_1_is_left_to_icbp(self):
        self.assertNotIn("eth_studyspaces:HCIG5.1", self.samples)

    def test_fallbacks(self):
        raw = {"data": [
            {"id": "X1", "attributes": {"occupancy": 5, "occupancyPercentage": 40, "status": "purple",
                                        "trend": "sideways", "timestamp": "2026-11-19T21:00:00"}},
            {"id": "X2", "attributes": {"roomGlobalIdentifier": "X2", "occupancy": 300, "maxPeople": 200,
                                        "status": "red", "timestamp": "2026-11-19T20:00:00.5Z"}},
            {"id": "X3", "attributes": {"occupancy": 1, "maxPeople": 10, "timestamp": "garbage"}},
            {"attributes": {}}]}
        x1, x2, x3 = ss.parse_samples(raw, NOW)
        self.assertEqual((x1.place_id, x1.pct, x1.max, x1.status, x1.trend), ("eth_studyspaces:X1", 40, None, "unknown", None))
        self.assertEqual(x1.ts, "2026-11-19T20:00:00+00:00")  # naive = Zurich time
        self.assertEqual((x2.pct, x2.ts), (100.0, "2026-11-19T20:00:00+00:00"))
        self.assertEqual(x3.ts, "2026-11-19T20:43:00+00:00")


class EndToEnd(unittest.TestCase):
    def test_both_sources_through_the_live_loop(self):
        srcs = [s for s in live.discover() if s.key.startswith("eth_")]
        self.assertEqual([(s.key, s.every, s.max_age) for s in srcs],
                         [("eth_studyspaces", 3600, 10800), ("eth_availability", 300, 900)])
        conn = store.connect(":memory:")
        res = live.run_due(conn, FakeHttp(), srcs, force=True)
        self.assertEqual(res["eth_studyspaces"]["places"], 7)
        self.assertEqual(res["eth_availability"], {"places": 3, "samples": 8, "new_samples": 8})
        rows = {r["id"]: r for r in latest(conn)}
        self.assertEqual(rows["eth_studyspaces:HGH30.1"]["latest"]["count"], 32)
        chn = rows["eth_studyspaces:CHNF46"]   # seminar room with a sensor: a place with a note
        self.assertEqual((chn["name"], chn["campus"]), ("CHN F 46", "Zürich Zentrum"))
        self.assertIn("Rooms view", chn["note"])
        self.assertIsNotNone(chn["latest"]["pct"])
        self.assertEqual(rows["eth_studyspaces:MMB79"]["hours"]["meals"]["mon"][1], ["17:30", "19:30"])


if __name__ == "__main__":
    unittest.main()
