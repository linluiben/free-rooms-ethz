import sqlite3
import tempfile
import unittest
from pathlib import Path

from freerooms import store
from freerooms.campus import canonical


class CanonicalTest(unittest.TestCase):
    def test_both_universities_share_zentrum(self):
        for name in ("Zürich Zentrum", "Zentrum", "ZZ", " zentrum "):
            self.assertEqual(canonical(name), "Zentrum")
        self.assertEqual(canonical("Zürich Hönggerberg"), "Hönggerberg")
        self.assertEqual(canonical("AC"), "Basel")

    def test_unknown_and_empty(self):
        self.assertEqual(canonical("Wädenswil"), "Wädenswil")
        self.assertEqual(canonical("Botanischer Garten"), "Zentrum")
        self.assertIsNone(canonical(None))
        self.assertIsNone(canonical(" "))


class MigrationTest(unittest.TestCase):
    def test_old_db_gets_uni_campus_and_per_uni_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE rooms (id TEXT PRIMARY KEY, building TEXT NOT NULL, floor TEXT NOT NULL,
                    room TEXT NOT NULL, type TEXT, typecode TEXT, seats INTEGER, site TEXT,
                    fetched_at TEXT, n_entries INTEGER);
                INSERT INTO rooms(id, building, floor, room, site) VALUES ('HG E 1.2', 'HG', 'E', '1.2', 'Zürich Zentrum');
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta VALUES ('window', '2026-11-20/2026-11-27');
            """)
            old.commit()
            old.close()
            conn = store.connect(path)
            self.assertEqual(tuple(conn.execute("SELECT uni, campus FROM rooms").fetchone()), ("ETH", "Zentrum"))
            self.assertEqual(store.get_meta(conn, store.window_key("ETH")), "2026-11-20/2026-11-27")
            self.assertIsNone(store.get_meta(conn, "window"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
