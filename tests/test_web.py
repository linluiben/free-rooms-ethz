import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from freerooms import store
from freerooms.web import make_handler, simplify_detail

RAW = {"description": None, "infoText": None, "tabs": [
    {"title": "Overview", "attributes": [
        {"name": "PL.SITZ.SITZPL", "label": "Seats", "value": "172", "isAttachment": False},
        {"name": "x", "label": "Plan", "value": None, "isAttachment": True}]},
    {"title": None, "attributes": None}]}


class FakeClient:
    calls = 0

    def room_detail(self, rid):
        self.calls += 1
        return RAW


class Detail(unittest.TestCase):
    def test_simplify(self):
        self.assertEqual(simplify_detail(RAW)["tabs"],
                         [{"title": "Overview", "items": [{"name": "PL.SITZ.SITZPL", "label": "Seats", "value": "172"}]}])

    def test_endpoint_caches_and_rejects_unknown_rooms(self):
        import tempfile, os
        db = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        conn = store.connect(db)
        conn.execute("INSERT INTO rooms(id, building, floor, room) VALUES ('HG E 1.2', 'HG', 'E', '1.2')")
        conn.commit()
        client = FakeClient()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, client))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}/api/roomdetail?id="
        try:
            for _ in range(2):
                with urllib.request.urlopen(base + "HG%20E%201.2") as r:
                    self.assertEqual(json.load(r)["tabs"][0]["items"][0]["value"], "172")
            self.assertEqual(client.calls, 1)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "..%2Fv2%2Frooms")
            self.assertEqual(e.exception.code, 400)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
