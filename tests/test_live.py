import unittest
from datetime import datetime, timedelta, timezone

from freerooms import live, store
from freerooms.places import Place, Sample, add_samples, latest, upsert_places

P = Place(id="t:1", uni="ETH", name="Lesesaal", source="t_meta", seats=100, features=[{"key": "quiet", "label": "Quiet"}])


def ok_source(key="t_meta", samples=()):
    return live.Source(key=key, every=300, max_age=900, fetch=lambda http: ([P], list(samples)))


def bad_source(key="t_bad"):
    def fetch(http):
        raise OSError("connection refused")
    return live.Source(key=key, every=300, max_age=900, fetch=fetch)


class Places(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        upsert_places(self.conn, [P])

    def test_samples_dedup_and_unknown_place(self):
        s = Sample("t:1", "2026-11-19T10:00:00+00:00", status="green", pct=30, count=30, max=100, trend=1)
        with self.assertLogs("freerooms.places", "WARNING"):
            n = add_samples(self.conn, [s, s, Sample("t:404", "2026-11-19T10:00:00+00:00")])
        self.assertEqual(n, 1)

    def test_latest_picks_newest_sample(self):
        add_samples(self.conn, [Sample("t:1", "2026-11-19T10:00:00+00:00", pct=30),
                                Sample("t:1", "2026-11-19T10:05:00+00:00", pct=40, closed=False)])
        now = datetime(2026, 11, 19, 10, 6, tzinfo=timezone.utc)
        (p,) = latest(self.conn, now)
        self.assertEqual((p["latest"]["pct"], p["latest"]["age_s"]), (40, 60))
        self.assertEqual(p["features"][0]["key"], "quiet")

    def test_place_without_samples(self):
        (p,) = latest(self.conn)
        self.assertIsNone(p["latest"])


class Loop(unittest.TestCase):
    def test_failing_source_does_not_block_others(self):
        conn = store.connect(":memory:")
        s = Sample("t:1", "2026-11-19T10:00:00+00:00", pct=10)
        res = live.run_due(conn, None, [bad_source(), ok_source(samples=[s])])
        self.assertIsNone(res["t_bad"])
        self.assertEqual(res["t_meta"], {"places": 1, "samples": 1, "new_samples": 1})
        h = store.health(conn)["sources"]
        self.assertIn("OSError", h["t_bad"]["last_error"])
        self.assertIsNone(h["t_bad"]["last_success"])
        self.assertFalse(h["t_meta"]["stale"])

    def test_due(self):
        conn = store.connect(":memory:")
        src = ok_source()
        self.assertTrue(live.due(conn, src, datetime.now(timezone.utc).timestamp()))
        live.run_source(conn, src, None)
        now = datetime.now(timezone.utc)
        self.assertFalse(live.due(conn, src, now.timestamp()))
        self.assertTrue(live.due(conn, src, (now + timedelta(seconds=300)).timestamp()))
        self.assertEqual(live.run_due(conn, None, [src]), {})   # not due yet

    def test_discover_finds_only_sources(self):
        self.assertTrue(all(isinstance(s, live.Source) for s in live.discover()))


if __name__ == "__main__":
    unittest.main()
