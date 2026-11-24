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

    def get_text(self, url):
        return self.get_json(url)


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

    def test_osm_code_from_name(self):
        # Real OSM data: ETH outlines carry the code in `name`, not `ref` (HCI's ref is a building number).
        raw = json.loads((FIX / "osm_eth_names.json").read_text())
        bs = {b.code: b for b in geo.parse_osm(raw, ["HG", "HCI", "BSS", "NO", "CAB"])}
        self.assertEqual(sorted(bs), ["BSS", "HCI", "HG", "NO"])   # CAB only in shop/venue names
        self.assertEqual((bs["HG"].lat, bs["HG"].lon), (47.376427, 8.54781))
        self.assertEqual((bs["NO"].lat, bs["NO"].lon), (47.37852, 8.5473))   # not the loading dock
        self.assertEqual(geo.code_from_tags({"name": "ETH HIT"}, {"HIT"}), "HIT")
        self.assertIsNone(geo.code_from_tags({"name": "ETH HIT"}, {"HG"}))

    def test_mapsearch(self):
        html = (FIX / "eth_mapsearch_hit.html").read_text()
        b = geo.parse_mapsearch(html, "HIT")
        self.assertEqual((b.uni, b.code, b.lat, b.lon, b.source), ("ETH", "HIT", 47.410381, 8.508163, "eth_mapsearch"))
        self.assertEqual(b.name, "Wolfgang-Pauli-Strasse 27, 8093 Zürich")
        # Unknown code: ETH's page has no map; a point outside every campus box is not trusted.
        self.assertIsNone(geo.parse_mapsearch("<h2>Gebäude</h2>", "XYZ"))
        self.assertIsNone(geo.parse_mapsearch(html.replace("47.410381", "46.9"), "HIT"))

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
        http = FakeHttp({geo.UZH_URL: uzh, geo.OVERPASS_URLS[0]: OSError("reset"), geo.OVERPASS_URLS[1]: osm,
                         geo.MAPSEARCH_URL.split("{")[0]: OSError("down")})
        with self.assertLogs("freerooms.geo", "WARNING"):
            res = geo.refresh(http, ("ETH", "UZH"), self.conn, self.path, pause=0)
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
            res = geo.refresh(http, ("ETH",), self.conn, self.path, pause=0)
        self.assertTrue(res["ETH"].startswith("FAILED"))
        self.assertEqual(len(json.loads(self.path.read_text())["buildings"]), 7)

    def test_eth_gaps_filled_from_mapsearch(self):
        osm = json.loads((FIX / "osm_eth.json").read_text())
        hit = (FIX / "eth_mapsearch_hit.html").read_text()
        ms = geo.MAPSEARCH_URL.split("{")[0]
        http = FakeHttp({geo.OVERPASS_URLS[0]: osm, ms + "HIT&": hit, ms: "<h2>Gebäude</h2>"})
        with self.assertLogs("freerooms.geo", "WARNING"):
            bs = {b.code: b for b in geo.fetch_eth(http, ["HG", "HIT", "ML"], pause=0)}
        self.assertEqual(sorted(bs), ["HG", "HIT"])            # ML: neither source knows it
        self.assertEqual((bs["HG"].source, bs["HIT"].source), ("osm", "eth_mapsearch"))
        # Only the codes OSM lacks are looked up on ETH's map pages.
        self.assertEqual(sorted(u[len(ms):].split("&")[0] for u in http.urls if u.startswith(ms)), ["HIT", "ML"])

    def test_mapsearch_gives_up_when_down(self):
        ms = geo.MAPSEARCH_URL.split("{")[0]
        http = FakeHttp({geo.OVERPASS_URLS[0]: {"elements": []}, ms: OSError("timeout")})
        with self.assertLogs("freerooms.geo", "WARNING"):
            self.assertEqual(geo.fetch_eth(http, [f"A{i}" for i in range(10)], pause=0), [])
        self.assertEqual(sum(u.startswith(ms) for u in http.urls), 3)

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
