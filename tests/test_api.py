import io
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message

from freerooms import api


def headers(**kv) -> Message:
    m = Message()
    for k, v in kv.items():
        m[k.replace("_", "-")] = str(v)
    return m


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes = b"{}", **hdrs):
        super().__init__(body)
        self.headers = headers(**hdrs)


def http_error(code: int, **hdrs) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", headers(**hdrs), io.BytesIO(b""))


class Fake:
    """Scripted opener plus a fake clock/sleep that never actually sleeps."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.calls = 0

    def open(self, req, timeout):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def client(self, **kw) -> api.ApiClient:
        return api.ApiClient(opener=self.open, sleep=self.sleep, clock=lambda: self.now, **kw)


class RateLimit(unittest.TestCase):
    def test_plenty_remaining_keeps_normal_gap(self):
        f = Fake(FakeResp(x_ratelimit_remaining=19), FakeResp(x_ratelimit_remaining=18))
        c = f.client(delay=0.3)
        c.date_range(); c.date_range()
        self.assertEqual([round(s, 3) for s in f.sleeps], [0.3])

    def test_low_remaining_slows_down(self):
        f = Fake(FakeResp(x_ratelimit_remaining=3), FakeResp(x_ratelimit_remaining=19), FakeResp())
        c = f.client(delay=0.3)
        c.date_range(); c.date_range(); c.date_range()
        self.assertEqual([round(s, 3) for s in f.sleeps], [api.LOW_PAUSE, 0.3])

    def test_429_waits_retry_after(self):
        f = Fake(http_error(429, Retry_After=7), FakeResp(b'{"ok": 1}'))
        self.assertEqual(f.client(delay=0).date_range(), {"ok": 1})
        self.assertEqual(f.sleeps, [7.0])

    def test_429_without_header_uses_default(self):
        f = Fake(http_error(429), FakeResp())
        f.client(delay=0).date_range()
        self.assertEqual(f.sleeps, [api.RETRY_AFTER_DEFAULT])

    def test_knobs_are_configurable(self):
        f = Fake(FakeResp(x_ratelimit_remaining=8), FakeResp(), http_error(429), FakeResp())
        c = f.client(delay=0.3, low_remaining=10, low_pause=2.5, retry_after_default=9)
        c.date_range(); c.date_range(); c.date_range()
        self.assertEqual([round(s, 3) for s in f.sleeps], [2.5, 0.3, 9.0])

    def test_404_not_retried(self):
        f = Fake(http_error(404), FakeResp())
        with self.assertRaises(urllib.error.HTTPError):
            f.client(delay=0).date_range()
        self.assertEqual((f.calls, f.sleeps), (1, []))

    def test_5xx_and_network_errors_back_off(self):
        f = Fake(http_error(503), urllib.error.URLError("down"), FakeResp())
        f.client(delay=0).date_range()
        self.assertEqual(f.sleeps, [2, 4])

    def test_gives_up_after_retries(self):
        f = Fake(*(http_error(429, Retry_After=1) for _ in range(3)))
        with self.assertRaises(urllib.error.HTTPError):
            f.client(delay=0, retries=2).date_range()
        self.assertEqual(f.calls, 3)


class RetryAfter(unittest.TestCase):
    NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)

    def test_seconds(self):
        self.assertEqual(api.retry_after("30"), 30.0)

    def test_http_date(self):
        self.assertEqual(api.retry_after("Thu, 19 Nov 2026 12:00:45 GMT", self.NOW), 45.0)

    def test_past_date_is_zero(self):
        self.assertEqual(api.retry_after("Thu, 19 Nov 2026 11:00:00 GMT", self.NOW), 0.0)

    def test_capped_and_garbage(self):
        self.assertEqual(api.retry_after("86400"), api.RETRY_AFTER_MAX)
        self.assertEqual(api.retry_after("-5"), 0.0)
        self.assertEqual(api.retry_after("soon"), api.RETRY_AFTER_DEFAULT)
        self.assertEqual(api.retry_after(None), api.RETRY_AFTER_DEFAULT)
        self.assertEqual(api.retry_after(None, default=5), 5)
        self.assertEqual(api.retry_after("86400", cap=10), 10)


if __name__ == "__main__":
    unittest.main()
