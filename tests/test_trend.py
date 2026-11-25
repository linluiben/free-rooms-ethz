"""Our own occupancy trend (places.computed_trend, places.latest) on synthetic sample series."""

import unittest
from datetime import datetime, timedelta, timezone

from freerooms import store
from freerooms.places import Place, Sample, add_samples, computed_trend, latest, upsert_places

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def ts(minute: float) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat(timespec="seconds")


def series(pcts, every=5, closed_at=()):
    """pct values every `every` minutes, the last at minute 0; None = no number."""
    n = len(pcts)
    return [{"ts": ts((i - n + 1) * every), "pct": p, "closed": i in closed_at} for i, p in enumerate(pcts)]


class ComputedTrend(unittest.TestCase):
    def test_rising(self):
        self.assertEqual(computed_trend(series([30, 33, 36, 40, 45])), 1)

    def test_falling(self):
        self.assertEqual(computed_trend(series([60, 55, 52, 48, 44])), -1)

    def test_flat(self):
        self.assertEqual(computed_trend(series([40, 40, 40, 40, 40])), 0)

    def test_noisy_stays_steady(self):
        self.assertEqual(computed_trend(series([40, 44, 37, 43, 38, 42, 41])), 0)
        self.assertEqual(computed_trend(series([40, 41, 42, 43, 44.9])), 0)   # just under the dead band

    def test_compares_with_newest_sample_15_to_30_min_back(self):
        # 15 min back is 40 (-> +10); 25 min back would be 52 (-> -2). The newest in the window wins.
        self.assertEqual(computed_trend(series([52, 52, 40, 45, 48, 50])), 1)

    def test_gap_longer_than_window(self):
        s = [{"ts": ts(-60), "pct": 10, "closed": False}, {"ts": ts(-50), "pct": 10, "closed": False},
             {"ts": ts(0), "pct": 80, "closed": False}]
        self.assertIsNone(computed_trend(s))

    def test_too_short_history(self):
        self.assertIsNone(computed_trend(series([10, 20, 30])))          # 10 min only
        self.assertIsNone(computed_trend(series([50])))
        self.assertIsNone(computed_trend([]))

    def test_closed_in_between(self):
        self.assertIsNone(computed_trend(series([0, 0, 0, 30, 50], closed_at={2})))
        # Closed long enough ago (before the reference sample): fine again.
        self.assertEqual(computed_trend(series([0, 10, 20, 30, 40, 50], closed_at={0})), 1)

    def test_latest_closed_or_without_number(self):
        self.assertIsNone(computed_trend(series([10, 20, 30, 40, 50], closed_at={4})))
        self.assertIsNone(computed_trend(series([10, 20, 30, 40, None])))

    def test_no_data_in_between(self):
        self.assertIsNone(computed_trend(series([10, 20, None, 40, 50])))

    def test_stepped_source_needs_one_step_seen_twice(self):
        # UZH: 5 % steps. One step, held for two samples -> a trend.
        self.assertEqual(computed_trend(series([30, 30, 30, 35, 35]), step=5), 1)
        self.assertEqual(computed_trend(series([30, 30, 30, 25, 25]), step=5), -1)
        # One step in the latest sample only -> not confirmed yet, steady.
        self.assertEqual(computed_trend(series([30, 30, 30, 30, 35]), step=5), 0)
        # Flickering between two steps -> steady.
        self.assertEqual(computed_trend(series([30, 35, 30, 35, 30, 35]), step=5), 0)
        # Samples 15 min apart: nothing between to confirm with -> steady.
        self.assertEqual(computed_trend(series([30, 40], every=15), step=5), 0)
        self.assertEqual(computed_trend(series([30, 40], every=15)), 1)


class Latest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        upsert_places(self.conn, [Place(id="uzh_ub:1", uni="UZH", name="UB A", source="uzh_ub", seats=100),
                                  Place(id="t:eth", uni="ETH", name="Lesesaal", source="t", seats=100),
                                  Place(id="t:quiet", uni="ETH", name="Silent", source="t", seats=100)])
        self.now = T0 + timedelta(minutes=1)

    def add(self, pid, pcts, **kw):
        add_samples(self.conn, [Sample(pid, s["ts"], pct=s["pct"], closed=s["closed"], **kw) for s in series(pcts)])

    def test_computed_and_source_trend(self):
        self.add("uzh_ub:1", [30, 30, 30, 20, 20, 15])                # emptying, no source trend
        self.add("t:eth", [30, 35, 40, 45, 50], trend=0)               # the source says steady: it wins
        rows = {p["id"]: p["latest"] for p in latest(self.conn, self.now)}
        self.assertEqual((rows["uzh_ub:1"]["trend"], rows["uzh_ub:1"]["trend_source"]), (-1, "computed"))
        self.assertEqual((rows["uzh_ub:1"]["pct"], rows["uzh_ub:1"]["age_s"]), (15, 60))
        self.assertEqual((rows["t:eth"]["trend"], rows["t:eth"]["trend_source"]), (0, "source"))
        self.assertIsNone(rows["t:quiet"])

    def test_no_trend_without_history(self):
        self.add("t:eth", [50])
        (row,) = [p["latest"] for p in latest(self.conn, self.now) if p["id"] == "t:eth"]
        self.assertEqual((row["trend"], row["trend_source"]), (None, None))

    def test_old_samples_outside_the_window_are_ignored(self):
        add_samples(self.conn, [Sample("t:eth", ts(-120), pct=0), Sample("t:eth", ts(-20), pct=50),
                                Sample("t:eth", ts(0), pct=50)])
        (row,) = [p["latest"] for p in latest(self.conn, self.now) if p["id"] == "t:eth"]
        self.assertEqual((row["trend"], row["trend_source"]), (0, "computed"))

    def test_one_query_for_all_places(self):
        for pid in ("uzh_ub:1", "t:eth", "t:quiet"):
            self.add(pid, [10, 20, 30, 40, 50])
        queries = []
        self.conn.set_trace_callback(queries.append)
        rows = latest(self.conn, self.now)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(queries), 1)
        self.assertEqual([p["latest"]["trend"] for p in rows], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
