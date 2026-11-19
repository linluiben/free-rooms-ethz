import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from freerooms import fetch, store
from freerooms.web import make_handler

ROOM = {"building": "CAB", "floor": "G", "room": "11", "typecode": "3420", "locationName": "Zürich Zentrum"}
ALLOC = {"date_from": "2026-11-11T10:00:00", "date_to": "2026-11-11T12:00:00",
         "belegungsserie": {"belegungstyp": 2, "veranstaltung": {"allocationTitle": "Lecture"}}}


class FakeClient:
    def __init__(self, rooms=None, fail_rooms=False, fail_allocs=False):
        self._rooms, self.fail_rooms, self.fail_allocs = rooms or [ROOM], fail_rooms, fail_allocs

    def rooms(self):
        if self.fail_rooms:
            raise OSError("connection refused")
        return self._rooms

    def allocations(self, rid, start, end):
        if self.fail_allocs:
            raise OSError("timeout")
        return [ALLOC]


def run(conn, client, **kw):
    return fetch.run(conn, client, buildings=("CAB",), **kw)   # buildings: skips the rooms.txt check


def tmp_db():
    return os.path.join(tempfile.mkdtemp(), "t.sqlite")


class Health(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(tmp_db())

    def tearDown(self):
        self.conn.close()

    def test_never_fetched_is_stale(self):
        h = store.health(self.conn, ("eth_rooms",))
        self.assertEqual(h["status"], "stale")
        s = h["sources"]["eth_rooms"]
        self.assertEqual((s["last_success"], s["age_s"], s["stale"], s["max_age_s"]), (None, None, True, 4 * 3600))

    def test_success_records_counts(self):
        stats = run(self.conn, FakeClient(), max_age=3600)
        s = store.health(self.conn, ("eth_rooms",))["sources"]["eth_rooms"]
        self.assertEqual(s["counts"], stats)
        self.assertEqual((s["counts"]["ok"], s["counts"]["intervals"]), (1, 1))
        self.assertEqual((s["last_error"], s["stale"], s["max_age_s"]), (None, False, 3600))
        self.assertEqual(s["last_attempt"], s["last_success"])
        self.assertIsNotNone(store.get_meta(self.conn, "last_fetch"))

    def test_failure_keeps_last_success(self):
        run(self.conn, FakeClient())
        ok = store.health(self.conn)["sources"]["eth_rooms"]
        with self.assertRaises(OSError):
            run(self.conn, FakeClient(fail_rooms=True))
        s = store.health(self.conn)["sources"]["eth_rooms"]
        self.assertEqual((s["last_success"], s["counts"]), (ok["last_success"], ok["counts"]))
        self.assertIn("connection refused", s["last_error"])
        run(self.conn, FakeClient())   # next success clears the error
        self.assertIsNone(store.health(self.conn)["sources"]["eth_rooms"]["last_error"])

    def test_all_rooms_failing_is_a_failure(self):
        with self.assertRaises(RuntimeError), self.assertLogs("freerooms.fetch", "ERROR"):
            run(self.conn, FakeClient(fail_allocs=True))
        s = store.health(self.conn)["sources"]["eth_rooms"]
        self.assertIsNone(s["last_success"])
        self.assertIn("allocation fetches failed", s["last_error"])
        self.assertIsNone(store.get_meta(self.conn, "last_fetch"))

    def test_stale_after_max_age(self):
        store.record_attempt(self.conn, "eth_rooms", counts={"rooms": 1}, max_age=3600)
        now = datetime.now(timezone.utc)
        self.assertEqual(store.health(self.conn, now=now + timedelta(minutes=59))["status"], "ok")
        h = store.health(self.conn, now=now + timedelta(minutes=61))
        self.assertEqual(h["status"], "stale")
        self.assertTrue(h["sources"]["eth_rooms"]["stale"])

    def test_sources_are_independent(self):
        store.record_attempt(self.conn, "eth_rooms", counts={"rooms": 1})
        store.record_attempt(self.conn, "eth_study_spaces", error="HTTP 500", max_age=900)
        h = store.health(self.conn, ("eth_rooms",))
        self.assertEqual(list(h["sources"]), ["eth_rooms", "eth_study_spaces"])
        self.assertFalse(h["sources"]["eth_rooms"]["stale"])
        self.assertTrue(h["sources"]["eth_study_spaces"]["stale"])
        self.assertEqual(h["status"], "stale")

    def test_backfill_from_old_meta(self):
        db = tmp_db()
        conn = store.connect(db)
        conn.execute("DELETE FROM source_health")
        store.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        conn.commit()
        conn.close()
        conn = store.connect(db)
        try:
            self.assertFalse(store.health(conn)["sources"]["eth_rooms"]["stale"])
        finally:
            conn.close()


class Endpoint(unittest.TestCase):
    def test_api_health_and_rooms_payload(self):
        db = tmp_db()
        conn = store.connect(db)
        run(conn, FakeClient())
        conn.close()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, FakeClient()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/health") as r:
                h = json.load(r)
            self.assertEqual(h["status"], "ok")
            self.assertEqual(h["sources"]["eth_rooms"]["counts"]["rooms"], 1)
            with urllib.request.urlopen(base + "/api/rooms") as r:
                self.assertEqual(json.load(r)["health"]["sources"]["eth_rooms"]["stale"], False)
        finally:
            httpd.shutdown()

    def test_api_health_empty_db(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_db(), FakeClient()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/health")
            self.assertEqual(cm.exception.code, 503)
            with cm.exception:
                h = json.load(cm.exception)
            self.assertEqual(h["status"], "stale")
            self.assertIsNone(h["sources"]["eth_rooms"]["last_success"])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
