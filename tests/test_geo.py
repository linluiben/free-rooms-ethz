import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from freerooms import geo, store
from freerooms.web import make_handler, with_coords

FIX = Path(__file__).parent / "fixtures"


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def get_json(self, url):
        self.urls.append(url)
        for prefix, ans in self.answers.items():
            if url.startswith(prefix):
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise AssertionError(url)


class Distance(unittest.TestCase):
    def test_haversine(self):
        # One minute of latitude is one nautical mile.
        self.assertAlmostEqual(geo.distance_m(47.0, 8.5, 47.0 + 1 / 60, 8.5), 1853, delta=3)
        self.assertEqual(geo.distance_m(47.3, 8.5, 47.3, 8.5), 0)

    def test_walk_minutes(self):
        self.assertEqual(geo.walk_min(0), 1)
        self.assertEqual(geo.walk_min(64), 1)      # 64 m * 1.25 = 80 m
        self.assertEqual(geo.walk_min(65), 2)
        self.assertEqual(geo.walk_min(1000), 16)


class Parse(unittest.TestCase):
    def test_uzh(self):
        bs = geo.parse_uzh(json.loads((FIX / "uzh_buildings.json").read_text()))
        self.assertEqual(sorted(b.code for b in bs), ["AFL", "HIT", "KOL", "Y63"])
        afl = next(b for b in bs if b.code == "AFL")
        self.assertEqual((afl.uni, afl.lat, afl.lon, afl.name), ("UZH", 47.410822, 8.540082, "Cityport"))

    def test_osm_prefers_outlines_and_averages(self):
        with self.assertLogs("freerooms.geo", "WARNING") as logs:
            bs = {b.code: b for b in geo.parse_osm(json.loads((FIX / "osm_eth.json").read_text()), ["HG", "HCI", "CAB", "ML"])}
        self.assertEqual(sorted(bs), ["CAB", "HCI", "HG"])       # XYZ not wanted, ML not found
        # Two ETH outlines averaged; the entrance node (no building tag) is ignored.
        self.assertEqual((bs["HG"].lat, bs["HG"].lon), (47.3763, 8.548))
        self.assertEqual(bs["HG"].uni, "ETH")
        text = "\n".join(logs.output)
        self.assertIn("ML", text)                                # missing
        self.assertIn("ref=CAB", text)                           # two outlines ~700 m apart

    def test_query_lists_codes_once_per_campus(self):
        q = geo.overpass_query(["HG", "CAB", "HG"])
        self.assertEqual(q.count('"^(CAB|HG)$"'), len(geo.ETH_BBOXES))
        self.assertTrue(q.endswith("out center tags;"))


class Refresh(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.path = Path(d) / "buildings.json"
        self.conn = store.connect(os.path.join(d, "t.sqlite"))

    def test_refresh_merges_and_loads(self):
        uzh = json.loads((FIX / "uzh_buildings.json").read_text())
        osm = json.loads((FIX / "osm_eth.json").read_text())
        http = FakeHttp({geo.UZH_URL: uzh, geo.OVERPASS_URLS[0]: OSError("reset"), geo.OVERPASS_URLS[1]: osm})
        with self.assertLogs("freerooms.geo", "WARNING"):
            res = geo.refresh(http, ("ETH", "UZH"), self.conn, self.path)
        self.assertEqual(res, {"ETH": 3, "UZH": 4})           # second Overpass instance answered
        where = geo.coords(self.conn)
        self.assertNotIn(("ETH", "HIT"), where)               # not in the OSM answer
        self.assertIn(("UZH", "HIT"), where)                  # both universities have a HIT
        self.assertIn(("ETH", "HG"), where)
        # The file is valid JSON with one building per line.
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data["buildings"]), 7)
        self.assertEqual(set(data["sources"]), {"ETH", "UZH"})

        # A failed ETH refresh keeps the ETH buildings we had.
        http = FakeHttp({u: OSError("down") for u in geo.OVERPASS_URLS})
        with self.assertLogs("freerooms.geo", "WARNING"):
            res = geo.refresh(http, ("ETH",), self.conn, self.path)
        self.assertTrue(res["ETH"].startswith("FAILED"))
        self.assertEqual(len(json.loads(self.path.read_text())["buildings"]), 7)

    def test_sync_only_when_file_changes(self):
        geo.write_file({"generated": "a", "buildings": [
            {"uni": "ETH", "code": "HG", "lat": 47.3763, "lon": 8.548, "source": "osm"}]}, self.path)
        self.assertEqual(geo.sync(self.conn, self.path), 1)
        self.assertEqual(geo.sync(self.conn, self.path), 0)
        geo.write_file({"generated": "b", "buildings": []}, self.path)
        self.assertEqual(geo.sync(self.conn, self.path), 0)
        self.assertEqual(geo.coords(self.conn), {})

    def test_eth_codes_include_database_buildings(self):
        self.conn.execute("INSERT INTO rooms(id, building, floor, room) VALUES ('ZZZ A 1', 'ZZZ', 'A', '1')")
        self.assertIn("ZZZ", geo.eth_codes(self.conn))
        self.assertIn("HG", geo.eth_codes(None))


class Api(unittest.TestCase):
    def test_with_coords(self):
        where = {("ETH", "HG"): (47.3763, 8.548), ("UZH", "HIT"): (47.1, 8.1)}
        items = [{"uni": "ETH", "building": "HG"}, {"uni": "ETH", "building": "HIT"},
                 {"uni": "UZH", "building": "X", "lat": 47.2, "lon": 8.2}]
        with_coords(items, where)
        self.assertEqual([(i["lat"], i["lon"]) for i in items], [(47.3763, 8.548), (None, None), (47.2, 8.2)])

    def test_rooms_and_buildings_endpoints_carry_coordinates(self):
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        conn = store.connect(db)
        conn.execute("INSERT INTO rooms(id, building, floor, room, uni, campus, n_entries) "
                     "VALUES ('KOL-F-101', 'KOL', 'F', '101', 'UZH', 'Zentrum', 1)")
        conn.commit()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))   # loads the shipped buildings.json
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/rooms") as r:
                room = json.load(r)["rooms"][0]
            self.assertIsInstance(room["lat"], float)
            self.assertLess(geo.distance_m(room["lat"], room["lon"], 47.3745, 8.5486), 300)   # KOL, Rämistrasse
            with urllib.request.urlopen(base + "/api/buildings") as r:
                bs = json.load(r)
            self.assertTrue(any(b["uni"] == "UZH" and b["code"] == "KOL" for b in bs))
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
