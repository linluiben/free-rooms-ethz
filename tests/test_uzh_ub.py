import unittest
from datetime import datetime, timezone
from pathlib import Path

from freerooms import live, store
from freerooms.places import latest
from freerooms.sources import uzh_ub

FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def widget(label):
    """A crafted widget page around the given label, with the decoy value: strings of the real one."""
    return ("<script>var bar = new ProgressBar.Line('#progress', {text: {value: '%s', style: {"
            "transform: {prefix: true, value: 'translate(-50%%, -50%%)'}}}}); bar.animate(0.5);</script>" % label)


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.urls = pages, []   # pages: widget id -> text or Exception

    def get_text(self, url):
        self.urls.append(url)
        page = self.pages.get(int(url.rsplit("=", 1)[1]), OSError("not found"))
        if isinstance(page, Exception):
            raise page
        return page


NOW = datetime(2026, 11, 19, 20, 43, 37, tzinfo=timezone.utc)


class Parse(unittest.TestCase):
    def test_open(self):
        self.assertEqual(uzh_ub.parse_widget(fixture("uzh_ub_open.html")),
                         {"closed": False, "free": 70, "seats": 250, "pct": 30})

    def test_closed(self):
        self.assertEqual(uzh_ub.parse_widget(fixture("uzh_ub_closed.html")),
                         {"closed": True, "free": None, "seats": None, "pct": None})

    def test_more_than_95_free(self):
        p = uzh_ub.parse_widget(fixture("uzh_ub_gt95.html"))
        self.assertEqual((p["pct"], p["seats"], p["closed"]), (uzh_ub.EDGE_OCCUPIED, 120, False))

    def test_crafted_variants(self):
        cases = {
            "ca. 5% frei von 74": (95, 74),
            "40% frei von 120": (60, 120),
            "ca.  25 % frei": (75, None),
            "&lt; 5% frei von 100": (100 - uzh_ub.EDGE_OCCUPIED, 100),
            "> 95% frei von 40": (uzh_ub.EDGE_OCCUPIED, 40),
            "ca. 0% frei von 210": (100, 210),
        }
        for label, (pct, seats) in cases.items():
            with self.subTest(label=label):
                p = uzh_ub.parse_widget(widget(label))
                self.assertEqual((p["pct"], p["seats"]), (pct, seats))
        self.assertTrue(uzh_ub.parse_widget(widget("Geschlossen")).get("closed"))
        self.assertTrue(uzh_ub.parse_widget('text: { value: "geschlossen" }')["closed"])

    def test_garbage_raises(self):
        for text in ("", "<html>Service unavailable</html>", widget("Wartung"), widget("ca. 130% frei von 5")):
            with self.subTest(text=text[:40]), self.assertRaises(ValueError):
                uzh_ub.parse_widget(text)

    def test_status_uses_shared_thresholds(self):
        self.assertEqual(uzh_ub.make_sample(12, uzh_ub.parse_widget("value: 'ca. 30% frei von 250'"), "t").status,
                         "yellow")   # 70 % occupied


class Fetch(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def fetch(self, pages):
        http = FakeHttp(pages)
        res = uzh_ub.fetch(http, now=NOW, sleep=self.sleeps.append)
        return http, res

    def test_all_widgets_sequential_with_delay(self):
        http, (places, samples) = self.fetch({wid: fixture("uzh_ub_open.html") for wid in uzh_ub.LIBRARIES})
        self.assertEqual(len(http.urls), 12)
        self.assertEqual(self.sleeps, [uzh_ub.DELAY] * 11)
        self.assertEqual(len(places), 12)
        self.assertEqual(len(samples), 12)
        s = samples[0]
        self.assertEqual((s.ts, s.pct, s.max, s.status, s.trend, s.closed),
                         ("2026-11-19T20:43:00+00:00", 30, 250, "green", None, False))

    def test_place_metadata(self):
        with self.assertLogs("freerooms.sources.uzh_ub", "WARNING"):   # the other 10 widgets 404
            _, (places, samples) = self.fetch({12: fixture("uzh_ub_open.html"), 11: fixture("uzh_ub_closed.html")})
        by_id = {p.id: p for p in places}
        p = by_id["uzh_ub:12"]
        self.assertEqual((p.uni, p.source, p.name, p.campus, p.building, p.seats),
                         ("UZH", "uzh_ub", "UB Naturwissenschaften", "Irchel", "Y15", 250))
        self.assertTrue(p.url.startswith("https://www.ub.uzh.ch/"))
        self.assertIsNotNone(p.lat)
        closed = by_id["uzh_ub:11"]
        self.assertEqual(closed.seats, 85)              # static fallback, widget shows no count
        (cs,) = [s for s in samples if s.place_id == "uzh_ub:11"]
        self.assertEqual((cs.closed, cs.pct, cs.status, cs.max), (True, None, None, None))

    def test_single_failure_is_skipped(self):
        pages = {wid: fixture("uzh_ub_gt95.html") for wid in uzh_ub.LIBRARIES}
        pages[17] = OSError("timeout")
        pages[18] = "<html>broken</html>"
        with self.assertLogs("freerooms.sources.uzh_ub", "WARNING") as cm:
            _, (places, samples) = self.fetch(pages)
        self.assertEqual(len(cm.output), 2)
        self.assertEqual(len(places), 12)
        self.assertEqual(len(samples), 10)
        self.assertNotIn("uzh_ub:17", {s.place_id for s in samples})

    def test_all_failing_raises_and_is_recorded(self):
        conn = store.connect(":memory:")
        src = live.Source(key="uzh_ub", every=300, max_age=900,
                          fetch=lambda http: uzh_ub.fetch(http, now=NOW, sleep=lambda s: None))
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(live.run_source(conn, src, FakeHttp({})))
        self.assertIn("RuntimeError", store.health(conn)["sources"]["uzh_ub"]["last_error"])

    def test_stored_end_to_end(self):
        conn = store.connect(":memory:")
        src = live.Source(key="uzh_ub", every=300, max_age=900,
                          fetch=lambda http: uzh_ub.fetch(http, now=NOW, sleep=lambda s: None))
        res = live.run_source(conn, src, FakeHttp({wid: fixture("uzh_ub_open.html") for wid in uzh_ub.LIBRARIES}))
        self.assertEqual(res, {"places": 12, "samples": 12, "new_samples": 12})
        rows = latest(conn, NOW)
        self.assertTrue(all(r["latest"]["pct"] == 30 for r in rows))

    def test_registered(self):
        (src,) = [s for s in live.discover() if s.key == "uzh_ub"]
        self.assertEqual((src.every, src.max_age), (300, 900))


if __name__ == "__main__":
    unittest.main()
