"""The shared HTTP transport under newshound (2026-10-12, WP-TRANSPORT phase 3).

judges.py posts through transport.send: a date-form Retry-After no longer crashes it (float() raised ValueError on
"Wed, 21 Oct 2026 07:28:00 GMT"), a 403 is not retried, and the bearer key never reaches an error. Feed reads
stay on feedparser, whose conditional GET (If-None-Match / If-Modified-Since, a 304 as "unchanged") is pinned here
against a loopback server, so a change to how feeds are fetched cannot silently drop the validators. The CourtListener
docket-link search (newshound.resolve_bare_link) is covered in test_newshound.BareLinks. Nothing here leaves loopback.
"""
import contextlib
import http.server
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import newshound
from triage import judges
from tests.test_newshound import Base

judges._transport()
import transport  # noqa: E402

KEY = "jev-secret-key-123456"
DATE_FORM = "Wed, 21 Oct 2099 07:28:00 GMT"
ANSWERS = {"answers": {"q": {"type": "noul", "noul": 0.5}}, "model": "m", "usage": {}}


class Judges(unittest.TestCase):
    def setUp(self):
        self.sent, self.script = [], []
        self.slept = []
        self.patches = [patch("triage.judges.time.sleep", self.slept.append)]
        for p in self.patches:
            p.start()
        transport.install(self.fake)

    def tearDown(self):
        transport.uninstall()
        for p in self.patches:
            p.stop()

    def fake(self, method, url, *, headers, data, json, timeout, allow_redirects):
        self.sent.append((method, url, dict(headers), json))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def post(self, **kw):
        return judges._post("https://api.typesafe.ai/v1/systemone", {"state": {}}, {"Authorization": f"Bearer {KEY}"}, **kw)

    def test_a_date_form_retry_after_waits_it_out_instead_of_crashing(self):
        # old code: float("Wed, 21 Oct 2099 ...") raised ValueError out of _post, losing the item
        self.script = [(429, {"Retry-After": DATE_FORM}, b"slow"), (200, {}, b'{"answers": {}}')]
        self.assertEqual(self.post(), {"answers": {}})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.slept, [30], "a date years away is capped at the 30 s ceiling")

    def test_a_delta_seconds_retry_after_is_still_honoured(self):
        self.script = [(503, {"Retry-After": "7"}, b""), (200, {}, b"{}")]
        self.post()
        self.assertEqual(self.slept, [7.0])

    def test_a_403_is_not_retried(self):
        self.script = [(403, {}, b"forbidden")]
        with self.assertRaises(RuntimeError) as cm:
            self.post()
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertEqual(len(self.sent), 1, "a 4xx other than 429 is never retried")
        self.assertEqual(self.slept, [])

    def test_a_5xx_retries_with_backoff_then_gives_up(self):
        self.script = [(502, {}, b"")] * 3
        with self.assertRaises(RuntimeError):
            self.post(tries=3)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.slept, [2, 4])

    def test_a_network_failure_retries_and_the_key_never_reaches_the_error(self):
        self.script = [OSError(f"connect failed for Bearer {KEY}")] * 2
        with self.assertRaises(transport.TransportError) as cm:
            self.post(tries=2)
        self.assertNotIn(KEY, str(cm.exception) + cm.exception.url)
        self.assertEqual(len(self.sent), 2)

    def test_jev_sends_the_bearer_header_and_the_body_as_json(self):
        self.script = [(200, {}, b'{"answers": {"q": {"type": "noul", "noul": 0.25}}, "model": "m"}')]
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY}):
            out, meta = judges.Jev().judge({"title": "x"})
        self.assertEqual(out, {"q": {"p": 0.25}})
        method, url, headers, body = self.sent[0]
        self.assertEqual((method, headers["Authorization"]), ("POST", f"Bearer {KEY}"))
        self.assertEqual(body["state"], {"title": "x"})


class _Feed(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            return
        body = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>Bitcoin crypto bank '
                b'charged</title><link>http://127.0.0.1/p/1</link><guid>http://127.0.0.1/p/1</guid>'
                b'<description>crypto</description></item></channel></rss>')
        self.send_response(200)
        self.send_header("ETag", '"v1"')
        self.send_header("Last-Modified", "Wed, 07 Oct 2026 16:00:00 GMT")
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ConditionalGet(Base):
    def setUp(self):
        super().setUp()
        _Feed.seen = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Feed)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/feed.xml"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def run_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return newshound.run(self.conn, [self.url])

    def test_the_stored_etag_goes_out_as_if_none_match_and_a_304_changes_nothing(self):
        self.seen(self.url)
        self.assertEqual(self.run_once(), [])
        self.assertEqual(len(self.posted()), 1, "the first, unconditional read alerts on the item")
        self.assertNotIn("if-none-match", _Feed.seen[0])
        before = self.conn.execute("SELECT count(*) FROM sent_keys").fetchone()
        self.assertEqual(self.run_once(), [], "a 304 is not a failure")
        self.assertEqual(_Feed.seen[1].get("if-none-match"), '"v1"')
        self.assertEqual(_Feed.seen[1].get("if-modified-since"), "Wed, 07 Oct 2026 16:00:00 GMT")
        self.assertEqual(len(self.posted()), 1, "an unchanged feed alerts nothing twice")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sent_keys").fetchone(), before)

    def test_parse_feed_reports_the_304_with_no_entries(self):
        got = newshound.parse_feed(self.url, etag='"v1"', modified="Wed, 07 Oct 2026 16:00:00 GMT")
        self.assertEqual((got.status, list(got.entries)), (304, []))
        self.assertEqual(_Feed.seen[0]["if-none-match"], '"v1"')


if __name__ == "__main__":
    unittest.main()
