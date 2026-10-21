"""newshound's alerting state: what gets sent, once, and what never floods.

Each case runs check_and_alert against a real temporary SQLite database with the
feed and Slack mocked, so the tests exercise the same SQL the service runs.
The two tests this file replaced called check_and_alert with a signature it no
longer had and had been failing since before the dedupe change.
"""
import json
import socket
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import newshound

SEARCH = "https://www.courtlistener.com/feed/search/?q=yuchen+sun&type=r&order_by=dateFiled+desc&"
SEARCH_RENAMED = "https://www.courtlistener.com/feed/search/?q=yuchen+sun&type=r&order_by=score+desc&"
DOCKET = "https://www.courtlistener.com/docket/73224507/feed/"
SEC = "http://www.sec.gov/rss/litigation/admin.xml"


def feed(*entries):
    return types.SimpleNamespace(entries=[dict(e) for e in entries], status=200)


NEWCASE = "https://www.courtlistener.com/feed/search/?type=r&q=%28coinbase%29+AND+dateFiled%3A%5Bnow-14d+TO+%2A%5D"


def doc(n, att=None, title="Sun v. World Liberty Financial LLC", docket=73224507):
    path = f"/docket/{docket}/{n}/" + (f"{att}/" if att else "") + "slug/"
    url = "https://www.courtlistener.com" + path
    return {"id": url, "link": url, "title": title, "summary": ""}


def docket_entry(n):
    url = f"https://www.courtlistener.com/docket/73224507/sun-v-world-liberty-financial-llc/?order_by=desc#entry-{n}"
    return {"id": url, "link": url, "title": f"Entry #{n} in Sun v. World Liberty Financial LLC", "summary": "x"}


def minute_entry(n, summary="x"):
    """A docket feed's entry with no document number: #minute-entry-<id>, not #entry-<n>."""
    url = f"https://www.courtlistener.com/docket/73224507/sun-v-world-liberty-financial-llc/?order_by=desc#minute-entry-{n}"
    return {"id": url, "link": url, "title": "Minute entry from 2026-09-29 in Sun v. World Liberty Financial LLC",
            "summary": summary}


def bare(title, published):
    return {"id": "https://www.courtlistener.com", "link": "https://www.courtlistener.com",
            "title": title, "published": published, "summary": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        newshound.DB_PATH = os.path.join(self.tmp.name, "state.db")
        self.conn = newshound.initialize_db()
        self.slack = MagicMock()
        import clpace
        # Never the real state: the entry log the morning gather reads, the shared CourtListener pace window, or a
        # real token (a test that reached the API spent 21s waiting on the live window, 2026-10-08). A test that
        # needs a token patches its own.
        self.patches = [patch("newshound.get_slack_client", return_value=self.slack),
                        patch("newshound.time.sleep"),
                        patch("newshound.get_keywords", return_value=["crypto", "bitcoin"]),
                        patch.dict(os.environ, {"LOCAL_TESTING": "False"}),
                        patch("newshound.ENTRY_LOG", os.path.join(self.tmp.name, "entries.jsonl")),
                        patch("newshound.RUN_LOG", os.path.join(self.tmp.name, "runs.jsonl")),
                        patch("newshound._logged", None),
                        patch.object(clpace, "pace", lambda *a, **k: 0.0),
                        patch("newshound.courtlistener_token", return_value="")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        self.tmp.cleanup()

    def seen(self, url):
        self.conn.execute("INSERT OR IGNORE INTO feeds_seen VALUES (?)", (url,))
        self.conn.commit()

    def run_feed(self, url, parsed):
        with patch("newshound.parse_feed", return_value=parsed):
            newshound.check_and_alert(url, self.conn)

    def posted(self):
        return [c.kwargs["text"] for c in self.slack.chat_postMessage.call_args_list]


class CanonicalKey(unittest.TestCase):
    def test_search_and_docket_feed_spellings_of_one_entry_agree(self):
        a = newshound.canonical_key("x", doc(62)["link"])
        b = newshound.canonical_key("x", docket_entry(62)["link"])
        self.assertEqual(a, "cl:73224507:62")
        self.assertEqual(a, b)

    def test_attachments_stay_distinct(self):
        self.assertEqual(newshound.canonical_key("x", doc(59, att=2)["link"]), "cl:73224507:59:2")

    def test_other_links_lose_query_and_fragment(self):
        self.assertEqual(newshound.canonical_key("g", "https://x.com/a/status/1?s=20#top"), "https://x.com/a/status/1")

    def test_bare_link_falls_back_to_the_item_id(self):
        self.assertEqual(newshound.canonical_key("abc", "https://www.courtlistener.com"), "id:abc")

    def test_minute_entries_on_one_docket_stay_distinct(self):
        # Keyed on the docket page they all link to, every minute entry of a docket
        # was one document: the first sent, the rest skipped as already sent.
        a = newshound.canonical_key("x", minute_entry(480563541)["link"])
        b = newshound.canonical_key("x", minute_entry(480734762)["link"])
        self.assertEqual(a, "cl:73224507:minute:480563541")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, newshound.canonical_key("x", docket_entry(480563541)["link"]))


class DocketLink(unittest.TestCase):
    """CourtListener 404s /docket/<id>/; the docket page needs a slug after the id."""
    def test_document_and_attachment_links_keep_the_case_slug(self):
        for link in (doc(1, docket=74800001)["link"], doc(1, att=2, docket=74800001)["link"]):
            self.assertEqual(newshound.docket_link(link), "https://www.courtlistener.com/docket/74800001/slug/")

    def test_docket_urls_keep_their_slug_and_lose_query_and_fragment(self):
        self.assertEqual(newshound.docket_link(docket_entry(62)["link"]),
                         "https://www.courtlistener.com/docket/73224507/sun-v-world-liberty-financial-llc/")

    def test_a_slugless_docket_url_gets_one_and_a_search_is_not_a_docket(self):
        self.assertEqual(newshound.docket_link("https://www.courtlistener.com/docket/74800001/"),
                         "https://www.courtlistener.com/docket/74800001/docket/")
        self.assertIsNone(newshound.docket_link("https://www.courtlistener.com/?q=caseName%3A%28%22A%22%29&type=r"))


class BareLinkIds(unittest.TestCase):
    def test_entries_without_a_document_get_their_own_ids(self):
        a = newshound.item_identifier(bare("United States v. 225,364,961 USDT", "2026-09-22"))
        b = newshound.item_identifier(bare("Coinbase, Inc. v. Honscha", "2026-09-22"))
        self.assertNotEqual(a, b)
        self.assertNotIn(a, newshound.BARE_COURTLISTENER)


class Dedupe(Base):
    def test_one_document_through_two_feeds_is_sent_once(self):
        self.seen(SEARCH); self.seen(DOCKET)
        self.run_feed(SEARCH, feed(doc(62)))
        self.run_feed(DOCKET, feed(docket_entry(62)))
        self.assertEqual(len(self.posted()), 1)

    def test_same_document_twice_in_one_feed_is_sent_once(self):
        self.seen(SEARCH)
        self.run_feed(SEARCH, feed(doc(62), doc(62)))
        self.assertEqual(len(self.posted()), 1)

    def test_bare_entries_are_no_longer_swallowed_after_the_first(self):
        self.seen(SEARCH)
        self.run_feed(SEARCH, feed(bare("A v. B", "2026-09-21")))
        self.run_feed(SEARCH, feed(bare("A v. B", "2026-09-21"), bare("C v. D", "2026-09-22")))
        self.assertEqual(len(self.posted()), 2)

    def test_minute_entries_are_not_swallowed_after_the_first(self):
        # Sun v. World Liberty Financial, 2026-09-29: the stay came through the
        # docket feed as a minute entry and was never posted, because an earlier
        # minute entry had already sent the key every minute entry shared.
        self.seen(DOCKET)
        self.run_feed(DOCKET, feed(minute_entry(1, "Notice Regarding Pro Hac Vice Motion")))
        self.run_feed(DOCKET, feed(minute_entry(1, "Notice Regarding Pro Hac Vice Motion"),
                                   minute_entry(2, "Order AND ~Util - Case Stayed"),
                                   minute_entry(3, "Order AND ~Util - Terminate Motions")))
        self.assertEqual(len(self.posted()), 3)


class NewFeeds(Base):
    def test_a_new_feed_url_is_recorded_without_alerting(self):
        self.run_feed(SEARCH_RENAMED, feed(doc(1), doc(2), doc(3)))
        self.assertEqual(self.posted(), [])
        self.run_feed(SEARCH_RENAMED, feed(doc(1), doc(2), doc(3), doc(4)))
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("/4/", self.posted()[0])

    def test_renaming_a_feed_does_not_resend_what_the_old_url_sent(self):
        self.seen(SEARCH)
        self.run_feed(SEARCH, feed(doc(1), doc(2)))
        self.seen(SEARCH_RENAMED)          # even without the first-sight pass
        self.run_feed(SEARCH_RENAMED, feed(doc(1), doc(2)))
        self.assertEqual(len(self.posted()), 2)

    def test_a_known_feed_that_never_matched_still_alerts_its_first_match(self):
        self.seen(SEC)
        self.run_feed(SEC, feed({"id": "g1", "link": "https://www.sec.gov/a", "title": "Bitcoin fraud charges", "summary": ""}))
        self.assertEqual(len(self.posted()), 1)

    def test_keywords_still_gate_non_courtlistener_feeds(self):
        self.seen(SEC)
        self.run_feed(SEC, feed({"id": "g2", "link": "https://www.sec.gov/b", "title": "Municipal bonds", "summary": ""}))
        self.assertEqual(self.posted(), [])


class Durability(Base):
    def test_each_item_is_recorded_as_soon_as_it_is_sent(self):
        self.seen(SEARCH)
        self.slack.chat_postMessage.side_effect = [None, RuntimeError("stopped mid-feed")]
        with self.assertRaises(RuntimeError):
            self.run_feed(SEARCH, feed(doc(1), doc(2)))
        self.slack.chat_postMessage.side_effect = None
        self.run_feed(SEARCH, feed(doc(1), doc(2)))
        self.assertEqual(sum("/1/" in t for t in self.posted()), 1, "doc 1 was posted again")


class BareLinks(Base):
    def setUp(self):
        super().setUp()
        newshound._docket_links.clear()
        # the API call reserves a slot in claude_knowledge's shared cl-pace window first (clpace.py): stubbed, so a
        # test neither spends a real slot nor fails when another process has the window busy
        import clpace
        self.paced = []
        pacer = patch.object(clpace, "pace", side_effect=lambda *a, **k: self.paced.append(1))
        pacer.start()
        self.addCleanup(pacer.stop)

    def over_transport(self, fake):
        """The shared transport's one test seam: send() still builds its headers and redacts around `fake`, and
        nothing reaches the network."""
        sys.path.append(str(newshound.TRANSPORT_DIR))
        import transport
        return transport.installed(fake)

    def test_without_the_api_a_bare_entry_links_to_a_search_for_its_caption(self):
        self.seen(SEARCH)
        with patch("newshound.courtlistener_token", return_value=""):
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22")))
        text = self.posted()[0]
        self.assertNotIn("Link: https://www.courtlistener.com\n", text + "\n")
        self.assertIn("caseName", text)
        self.assertIn("Honscha", text)

    def test_with_the_api_a_bare_entry_links_to_its_docket(self):
        self.seen(SEARCH)
        body = json.dumps({"results": [{"caseName": "Coinbase, Inc. v. Honscha",
                                        "docket_absolute_url": "/docket/74811111/coinbase-inc-v-honscha/"}]}).encode()
        sent = []

        def fake(method, url, *, headers, data, json, timeout, allow_redirects):
            sent.append((method, url, dict(headers), timeout))
            return 200, {"content-type": "application/json"}, body
        with patch("newshound.courtlistener_token", return_value="t-secret-token"), self.over_transport(fake):
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22"),
                                       bare("Coinbase, Inc. v. Honscha", "2026-09-23")))
        self.assertTrue(all("/docket/74811111/" in t for t in self.posted()), self.posted())
        self.assertEqual(len(sent), 1, "one API call per feed, not per entry")
        self.assertEqual(sent[0][0], "GET")
        self.assertEqual(sent[0][2]["Authorization"], "Token t-secret-token", "the token goes in the header")
        self.assertEqual(sent[0][2]["User-Agent"], "newshound (Protos)")
        self.assertNotIn("t-secret-token", sent[0][1], "the token never goes in the URL")
        self.assertEqual(len(self.paced), 1, "the API call is paced through the shared window")

    def test_a_busy_shared_window_falls_back_to_a_caption_search_without_calling_the_api(self):
        import clpace
        sent = []
        self.seen(SEARCH)
        with patch.object(clpace, "pace", side_effect=clpace.PaceWait("cl-pace: next slot in 900s")), \
             patch("newshound.courtlistener_token", return_value="t-secret-token"), \
             self.over_transport(lambda *a, **k: sent.append(1)):
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22")))
        self.assertEqual(sent, [], "a refused slot never becomes an unpaced call")
        self.assertIn("caseName", self.posted()[0])

    def test_an_api_failure_still_sends_a_usable_link(self):
        self.seen(SEARCH)
        def down(*a, **k):
            raise OSError("connection refused")
        with patch("newshound.courtlistener_token", return_value="t-secret-token"), self.over_transport(down):
            self.run_feed(SEARCH, feed(bare("Yuyuan Gao v. Coinbase, Inc.", "2026-09-22")))
        self.assertIn("caseName", self.posted()[0])

    def test_a_429_is_recorded_for_every_process_and_not_retried(self):
        import clpace
        calls, noted = [], []

        def throttled(method, url, *, headers, data, json, timeout, allow_redirects):
            calls.append(url)
            return 429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, b"slow down"
        cl = types.SimpleNamespace(note_throttle=lambda ra, why="": noted.append((ra, why)))
        self.seen(SEARCH)
        with patch("newshound.courtlistener_token", return_value="t-secret-token"), self.over_transport(throttled), \
             patch.object(clpace, "_cl_match", return_value=cl):
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22")))
        self.assertEqual(len(calls), 1, "a 429 is never retried")
        self.assertEqual(len(noted), 1, "CourtListener's Retry-After is recorded for the other processes")
        self.assertEqual(noted[0][0], 86400.0, "the date-form Retry-After parsed and capped at a day (as cl_match: "
                                               "a day-cap 429 blocks every process for the day), not a crash")
        self.assertIn("caseName", self.posted()[0])

    def test_a_403_is_not_retried_and_the_token_never_reaches_the_log(self):
        import contextlib, io
        calls = []

        def refused(method, url, *, headers, data, json, timeout, allow_redirects):
            calls.append(url)
            return 403, {}, b"Token t-secret-token refused"
        out = io.StringIO()
        self.seen(SEARCH)
        with patch("newshound.courtlistener_token", return_value="t-secret-token"), self.over_transport(refused), \
             contextlib.redirect_stdout(out):
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22")))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("t-secret-token", out.getvalue() + "".join(self.posted()))
        self.assertIn("caseName", self.posted()[0])


class NewCases(Base):
    A = dict(title="Doe v. Coinbase, Inc.", docket=74800001)
    B = dict(title="Roe v. Payward, Inc.", docket=74800002)

    def setUp(self):
        super().setUp()
        newshound._docket_links.clear()
        self.token = patch("newshound.courtlistener_token", return_value="")
        self.token.start()

    def tearDown(self):
        self.token.stop()
        super().tearDown()

    def test_the_filter_marks_a_new_case_feed(self):
        self.assertTrue(newshound.new_case_feed(NEWCASE))
        self.assertFalse(newshound.new_case_feed(SEARCH))
        self.assertFalse(newshound.new_case_feed(DOCKET))

    def test_a_new_case_filing_several_documents_alerts_once(self):
        self.seen(NEWCASE)
        self.run_feed(NEWCASE, feed(doc(1, **self.A), doc(1, att=1, **self.A),
                                    bare(self.A["title"], "2026-09-22"), doc(1, **self.B)))
        posts = self.posted()
        self.assertEqual(len(posts), 2, posts)
        self.assertTrue(all(p.startswith("🚨 NEW CASE!") for p in posts))
        self.assertIn("Link: https://www.courtlistener.com/docket/74800001/slug/\n", posts[0] + "\n")

    def test_later_filings_in_an_announced_case_stay_quiet(self):
        self.seen(NEWCASE)
        self.run_feed(NEWCASE, feed(doc(1, **self.A)))
        self.run_feed(NEWCASE, feed(doc(1, **self.A), doc(2, **self.A)))
        self.assertEqual(len(self.posted()), 1)

    def test_every_filing_feeds_still_alert_each_document(self):
        self.seen(SEARCH)
        self.run_feed(SEARCH, feed(doc(1), doc(2)))
        self.assertEqual(len(self.posted()), 2)

    def test_a_case_already_surfaced_elsewhere_is_not_announced_as_new(self):
        self.seen(SEARCH); self.seen(NEWCASE)
        self.run_feed(SEARCH, feed(doc(1, **self.A)))
        self.run_feed(NEWCASE, feed(doc(1, **self.A), doc(2, **self.A)))
        self.assertEqual(len(self.posted()), 1, "only the every-filing alert")

    def test_the_quiet_first_pass_records_cases_already_in_the_window(self):
        self.run_feed(NEWCASE, feed(doc(1, **self.A)))
        self.run_feed(NEWCASE, feed(doc(1, **self.A), doc(2, **self.A), doc(1, **self.B)))
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("Roe v. Payward", self.posted()[0])

    def test_an_empty_new_feed_still_alerts_its_first_case(self):
        self.run_feed(NEWCASE, feed())
        self.run_feed(NEWCASE, feed(doc(1, **self.A)))
        self.assertEqual(len(self.posted()), 1, "the first case was swallowed by the quiet pass")

    def test_a_recycled_caption_alerts_again_once_the_old_case_is_forgotten(self):
        self.seen(NEWCASE)
        self.conn.execute("INSERT INTO cases_seen VALUES (?, ?, datetime('now', '-40 days'))",
                          ("case:doe v. coinbase, inc.", NEWCASE))
        self.conn.commit()
        self.run_feed(NEWCASE, feed(doc(1, **self.A)))
        self.assertEqual(len(self.posted()), 1)

    def test_a_recent_case_with_the_same_caption_stays_quiet(self):
        self.seen(NEWCASE)
        self.conn.execute("INSERT INTO cases_seen VALUES (?, ?, datetime('now', '-3 days'))",
                          ("case:doe v. coinbase, inc.", NEWCASE))
        self.conn.commit()
        self.run_feed(NEWCASE, feed(doc(1, **self.A)))
        self.assertEqual(self.posted(), [])

    def test_the_quiet_pass_does_not_block_other_feeds(self):
        self.seen(SEARCH)
        self.run_feed(SEARCH_RENAMED, feed(doc(9)))      # first sight: quiet
        self.run_feed(SEARCH, feed(doc(9)))              # an established feed still sends it
        self.assertEqual(len(self.posted()), 1)


class Migration(unittest.TestCase):
    def test_existing_history_seeds_sent_keys_and_feeds_seen(self):
        with tempfile.TemporaryDirectory() as d:
            import sqlite3
            path = os.path.join(d, "old.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE processed_items (feed_source TEXT, item_id TEXT, PRIMARY KEY (feed_source, item_id))")
            c.executemany("INSERT INTO processed_items VALUES (?, ?)",
                          [(SEARCH, doc(62)["link"]), (SEARCH, "https://www.courtlistener.com")])
            c.commit(); c.close()
            newshound.DB_PATH = path
            conn = newshound.initialize_db()
            self.assertEqual(newshound.get_sent_keys(conn), {"cl:73224507:62"})
            self.assertEqual({r[0] for r in conn.execute("SELECT feed_source FROM feeds_seen")}, {SEARCH})
            conn.close()


def dated(entry, ts):
    """entry as feedparser serves it, published at epoch ts (UTC)."""
    import time as _t
    return dict(entry, published_parsed=_t.gmtime(ts))


class CourtListenerSchedule(Base):
    """CourtListener feeds are read on a per-feed schedule; every other feed on every run (2026-10-08)."""
    DAY = 86400

    def run_all(self, urls, parsed_for):
        """One newshound run over urls; parsed_for(url) is what the feed answers. Returns the urls fetched."""
        fetched = []

        def parse(url):
            fetched.append(url)
            result = parsed_for(url)
            if isinstance(result, Exception):
                raise result
            return result
        with patch("newshound.parse_feed", side_effect=parse):
            self.failures = newshound.run(self.conn, urls)
        return fetched

    def row(self, url):
        return self.conn.execute("SELECT next_due, interval, newest_ts FROM feed_schedule WHERE feed_source = ?",
                                 (url,)).fetchone()

    def due_at(self, url, when, newest=None):
        self.conn.execute("INSERT OR REPLACE INTO feed_schedule VALUES (?, ?, ?, ?)",
                          (url, when, newshound.CL_STALE_INTERVAL, newest))
        self.conn.commit()

    def read_once(self, url, parsed):
        """Read url (seen, due now) once and return (seconds from the read to its next_due, stored interval)."""
        import time as _t
        self.seen(url)
        self.due_at(url, 0)
        t0 = _t.time()
        self.assertEqual(self.run_all([url], lambda u: parsed), [url])
        next_due, interval, _ = self.row(url)
        return next_due - t0, interval

    def assertNear(self, got, want):
        self.assertTrue(want - 1 <= got <= want + 5, f"{got} is not ~{want}")

    def test_the_intervals(self):
        import time as _t
        now = _t.time()
        cases = [(DOCKET, feed(dated(docket_entry(1), now - 1 * self.DAY)), newshound.CL_ACTIVE_INTERVAL),
                 (DOCKET, feed(dated(docket_entry(1), now - 5 * self.DAY)), newshound.CL_RECENT_INTERVAL),
                 (DOCKET, feed(dated(docket_entry(1), now - 40 * self.DAY)), newshound.CL_STALE_INTERVAL),
                 (DOCKET, feed(), newshound.CL_STALE_INTERVAL),
                 (SEARCH, feed(dated(doc(1), now - 40 * self.DAY)), newshound.CL_SEARCH_INTERVAL)]
        for url, parsed, want in cases:
            with self.subTest(url=url, want=want):
                self.conn.execute("DELETE FROM feed_schedule"); self.conn.commit()
                wait, interval = self.read_once(url, parsed)
                self.assertEqual(interval, want)
                self.assertNear(wait, want - newshound.CL_RUN_SLACK)
        self.assertEqual((newshound.CL_ACTIVE_INTERVAL, newshound.CL_RECENT_INTERVAL, newshound.CL_STALE_INTERVAL,
                          newshound.CL_SEARCH_INTERVAL), (1800, 7200, 21600, 1800))

    def test_a_docket_with_no_dated_entry_is_judged_by_its_last_modified(self):
        import email.utils, time as _t
        parsed = feed(docket_entry(1))
        parsed.headers = {"last-modified": email.utils.formatdate(_t.time() - 3600, usegmt=True)}
        self.assertEqual(self.read_once(DOCKET, parsed)[1], newshound.CL_ACTIVE_INTERVAL)

    def test_a_30_minute_feed_is_read_on_the_run_30_minutes_later_not_40(self):
        # launchd runs every 600 s; a feed is reached a little earlier or later in each run than in the last.
        import time as _t
        t0 = _t.time()
        self.read_once(SEARCH, feed())
        runs = [t0 + 600 * k + jitter for k, jitter in ((1, 20), (2, 20), (3, -20))]
        due = [newshound.cl_due(self.conn, SEARCH, t) for t in runs]
        self.assertEqual(due, [False, False, True])

    def test_a_feed_not_yet_due_is_not_fetched_but_other_feeds_are_every_run(self):
        import time as _t
        self.seen(DOCKET); self.seen(SEC)
        self.due_at(DOCKET, _t.time() + 3600)
        for _ in range(2):
            self.assertEqual(self.run_all([DOCKET, SEC], lambda u: feed()), [SEC])
        self.assertIsNone(self.row(SEC), "a non-CourtListener feed has no schedule")

    def test_a_due_feed_is_fetched_and_the_run_says_how_many_were(self):
        import contextlib, io, time as _t
        self.seen(DOCKET); self.seen(SEARCH)
        self.due_at(DOCKET, _t.time() - 1)
        self.due_at(SEARCH, _t.time() + 3600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fetched = self.run_all([DOCKET, SEARCH, SEC], lambda u: feed())
        self.assertEqual(fetched, [DOCKET, SEC])
        self.assertIn("CourtListener: read 1 of 2 feeds due (1 not due)", out.getvalue())

    def test_a_feed_never_fetched_is_read_at_once(self):
        self.assertEqual(self.run_all([DOCKET], lambda u: feed(docket_entry(1))), [DOCKET])
        self.assertEqual(self.posted(), [], "its first sight is still the quiet baseline")
        self.assertIsNotNone(self.row(DOCKET))

    def test_a_known_feed_with_no_schedule_is_staggered_within_its_window(self):
        import time as _t
        self.seen(DOCKET)
        offset = newshound.cl_stagger(DOCKET, newshound.cl_first_window(DOCKET))
        self.assertGreater(offset, 10, "fixture: DOCKET must not land at the very start of its window")
        t0 = _t.time()
        self.assertEqual(self.run_all([DOCKET], lambda u: feed()), [], "read before its staggered turn")
        next_due, interval, newest = self.row(DOCKET)
        self.assertNear(next_due - t0, offset)
        self.assertEqual(interval, newshound.CL_ACTIVE_INTERVAL)
        self.assertIsNone(newest)

    def test_stagger_offsets_differ_and_lie_in_the_window(self):
        urls = [l.strip() for l in open(os.path.join(os.path.dirname(newshound.__file__), "rss.md"),
                                        encoding="utf-8-sig") if newshound.is_courtlistener(l.strip())]
        self.assertGreater(len(urls), 1)
        offsets = [newshound.cl_stagger(u, newshound.cl_first_window(u)) for u in urls]
        for u, o in zip(urls, offsets):
            self.assertTrue(0 <= o < newshound.cl_first_window(u), (u, o))
        self.assertGreater(len(set(offsets)), len(urls) // 2, "the feeds would come due together")
        self.assertEqual(offsets, [newshound.cl_stagger(u, newshound.cl_first_window(u)) for u in urls], "not stable")

    def test_a_failed_read_is_tried_again_in_30_minutes_keeping_what_was_known(self):
        import time as _t
        for answer in (TimeoutError("stalled"), types.SimpleNamespace(entries=[], status=503)):
            with self.subTest(answer=answer):
                self.seen(DOCKET)
                self.due_at(DOCKET, 0, newest=_t.time() - 40 * self.DAY)
                t0 = _t.time()
                fetched = self.run_all([DOCKET], lambda u: answer)
                self.assertNear(self.row(DOCKET)[0] - t0, newshound.CL_RETRY_INTERVAL - newshound.CL_RUN_SLACK)
                self.assertIsNotNone(self.row(DOCKET)[2], "a failure erased the newest entry known")
                if isinstance(answer, Exception):
                    self.assertEqual(len(fetched), 2, "the main loop's two attempts")
                    self.assertEqual(len(self.failures), 1)

    def test_a_courtlistener_read_waits_a_second_except_in_local_testing(self):
        self.seen(DOCKET); self.seen(SEC)
        self.due_at(DOCKET, 0)
        with patch("newshound.time.sleep") as slept:
            self.run_all([DOCKET, SEC], lambda u: feed())
        self.assertEqual([c.args for c in slept.call_args_list], [(newshound.CL_FEED_GAP,)])
        self.due_at(DOCKET, 0)
        with patch("newshound.time.sleep") as slept, patch.dict(os.environ, {"LOCAL_TESTING": "True"}):
            self.run_all([DOCKET, SEC], lambda u: feed())
        self.assertEqual(slept.call_count, 0)

    def test_only_the_courtlistener_host_is_scheduled(self):
        self.assertTrue(newshound.is_courtlistener(DOCKET))
        self.assertTrue(newshound.is_courtlistener(NEWCASE))
        self.assertFalse(newshound.is_courtlistener(SEC))
        self.assertFalse(newshound.is_courtlistener("https://example.com/?u=https://www.courtlistener.com/feed/"))

    def test_courtlistener_entries_reach_the_entry_log(self):
        self.seen(DOCKET)
        self.due_at(DOCKET, 0)
        self.run_all([DOCKET], lambda u: feed(docket_entry(62)))
        with open(newshound.ENTRY_LOG) as fh:
            rows = [json.loads(x) for x in fh]
        self.assertEqual([(r["feed"], r["link"]) for r in rows], [(DOCKET, docket_entry(62)["link"])])


if __name__ == "__main__":
    unittest.main()


class EntryLog(unittest.TestCase):
    """newshound logs every raw entry it reads (not CourtListener's) once, for the morning gather (2026-10-08)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "entries.jsonl")
        self.old = newshound.ENTRY_LOG, newshound._logged
        newshound.ENTRY_LOG, newshound._logged = self.path, None
        self.addCleanup(self._restore)

    def _restore(self):
        newshound.ENTRY_LOG, newshound._logged = self.old

    def lines(self):
        with open(self.path) as fh:
            return [json.loads(x) for x in fh]

    def test_each_entry_is_logged_once_with_its_raw_guid_and_utc_time(self):
        import time as _t
        e = {"id": "https://www.justice.gov/usao-sdny/pr/x", "link": "https://www.justice.gov/usao-sdny/pr/x",
             "title": "Man convicted of defrauding a crypto exchange", "summary": "",
             "published": "Wed, 07 Oct 2026 16:00:00 +0000", "published_parsed": _t.gmtime(1791388800)}
        feed = "http://www.justice.gov/atr/rss/atr_press.xml"
        self.assertEqual(newshound.log_entries(feed, [e]), 1)
        newshound._logged = None                      # a later run reloads the log from disk
        self.assertEqual(newshound.log_entries(feed, [dict(e)]), 0, "an entry already logged is not logged again")
        rows = self.lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["guid"], e["id"])
        self.assertEqual(rows[0]["published_ts"], 1791388800, "feedparser's struct is UTC, read as UTC")

    def test_courtlistener_feeds_are_logged(self):
        # Since 2026-10-08 the morning gather reuses newshound's CourtListener reads instead of reading the feeds again.
        self.assertEqual(newshound.log_entries("https://www.courtlistener.com/feed/search/?q=x",
                                               [{"id": "a", "title": "t"}]), 1)
        self.assertEqual([(r["feed"], r["guid"]) for r in self.lines()],
                         [("https://www.courtlistener.com/feed/search/?q=x", "a")])

    def test_courtlistener_entries_without_a_document_are_each_logged(self):
        # Every one of them is served with the site root as its guid; keyed on that, only the first was ever logged.
        root = "https://www.courtlistener.com"
        entries = [{"id": root, "link": root, "title": t, "published": "2026-10-07", "summary": ""}
                   for t in ("Doe v. Coinbase, Inc.", "Roe v. Payward, Inc.")]
        self.assertEqual(newshound.log_entries(DOCKET, entries), 2)
        self.assertEqual(newshound.log_entries(DOCKET, [dict(e) for e in entries]), 0)
        self.assertEqual([r["guid"] for r in self.lines()], [root, root], "the guid is logged as served")

    def test_old_lines_are_pruned_and_a_cut_line_dropped(self):
        import time as _t
        with open(self.path, "w") as fh:
            fh.write(json.dumps({"feed": "f", "guid": "old", "logged_ts": int(_t.time()) - 30 * 86400}) + "\n")
            fh.write(json.dumps({"feed": "f", "guid": "new", "logged_ts": int(_t.time())}) + "\n")
            fh.write('{"feed": "f", "gu')
        logged = newshound._load_entry_log()
        self.assertEqual(logged, {("f", "new")})
        self.assertEqual([r["guid"] for r in self.lines()], ["new"])


FED = "https://www.federalreserve.gov/feeds/press_all.xml"
OCC = "https://www.occ.treas.gov/rss/occ_news.xml"
LAST_MODIFIED = "Wed, 07 Oct 2026 16:00:00 GMT"


def full(*entries, etag='"v1"', modified=LAST_MODIFIED, status=200):
    """A read that carried a body, with the validators feedparser exposes when the server sent them."""
    parsed = types.SimpleNamespace(entries=[dict(e) for e in entries], status=status)
    if etag:
        parsed.etag = etag
    if modified:
        parsed.modified = modified
    return parsed


def not_modified():
    return types.SimpleNamespace(entries=[], status=304)


def fed_item(n, title="Federal Reserve Board announces action against a bitcoin bank"):
    return {"id": f"https://www.federalreserve.gov/p/{n}", "link": f"https://www.federalreserve.gov/p/{n}",
            "title": title, "summary": ""}


class EveryFeedRun(Base):
    """One newshound run over urls, recording each read and the validators it was sent."""

    def run_all(self, urls, parsed_for):
        import contextlib, io
        self.calls = []

        def parse(url, **kwargs):
            self.calls.append((url, kwargs))
            result = parsed_for(url)
            if isinstance(result, Exception):
                raise result
            return result
        out = io.StringIO()
        with patch("newshound.parse_feed", side_effect=parse), contextlib.redirect_stdout(out):
            self.failures = newshound.run(self.conn, urls)
        self.out = out.getvalue()
        return [u for u, _ in self.calls]

    def state(self):
        return [sorted(self.conn.execute(f"SELECT * FROM {t}").fetchall())
                for t in ("processed_items", "sent_keys", "feeds_seen", "cases_seen", "feed_schedule")]

    def entry_log(self):
        with open(newshound.ENTRY_LOG) as fh:
            return fh.read()

    def validators(self, url):
        return self.conn.execute("SELECT etag, modified FROM feed_validators WHERE feed_source = ?", (url,)).fetchone()


class ConditionalReads(EveryFeedRun):
    def test_a_304_is_quiet_changes_nothing_and_the_validators_go_out_again(self):
        self.seen(FED)
        self.run_all([FED], lambda u: full(fed_item(1)))
        self.assertEqual(self.calls, [(FED, {})], "the first read is unconditional")
        self.assertEqual(len(self.posted()), 1)
        before, log_before = self.state(), self.entry_log()
        for _ in range(2):
            self.run_all([FED], lambda u: not_modified())
            self.assertEqual(self.calls, [(FED, {"etag": '"v1"', "modified": LAST_MODIFIED})])
            self.assertNotIn("EMPTY", self.out)
            self.assertEqual(self.out, "")
            self.assertEqual(self.failures, [])
        self.assertEqual(self.state(), before, "a 304 touched processed or sent state")
        self.assertEqual(self.entry_log(), log_before)
        self.assertEqual(self.validators(FED), ('"v1"', LAST_MODIFIED))
        self.assertEqual(len(self.posted()), 1)

    def test_a_changed_feed_after_a_304_still_alerts_its_new_item(self):
        self.seen(FED)
        self.run_all([FED], lambda u: full(fed_item(1)))
        self.run_all([FED], lambda u: not_modified())
        self.run_all([FED], lambda u: full(fed_item(1), fed_item(2), etag='"v2"'))
        self.assertEqual(len(self.posted()), 2)
        self.assertIn("/p/2", self.posted()[1])
        self.assertEqual(self.validators(FED), ('"v2"', LAST_MODIFIED))

    def test_validators_are_stored_only_from_a_200(self):
        self.seen(FED)
        for answer in (full(fed_item(1), status=301), full(status=503), full(status=404),
                       types.SimpleNamespace(entries=[], bozo_exception=OSError("timed out"))):
            with self.subTest(status=getattr(answer, "status", None)):
                self.run_all([FED], lambda u: answer)
                self.assertIsNone(self.validators(FED))
                self.conn.execute("DELETE FROM feed_health"); self.conn.commit()
        self.run_all([FED], lambda u: full(fed_item(1)))
        self.assertEqual(self.validators(FED), ('"v1"', LAST_MODIFIED))
        # A feed that starts redirecting: a stored ETag sent through the redirect would come back as a 304 dressed in
        # the redirect's status, so the validators are dropped and the next read is a full one.
        self.run_all([FED], lambda u: full(fed_item(1), status=301))
        self.assertIsNone(self.validators(FED))
        self.run_all([FED], lambda u: full(fed_item(1), status=301))
        self.assertEqual(self.calls, [(FED, {})])

    def test_a_200_without_validators_sends_none(self):
        self.seen(OCC)
        self.run_all([OCC], lambda u: full(etag=None, modified=None))
        self.run_all([OCC], lambda u: full(etag=None, modified=None))
        self.assertEqual(self.calls, [(OCC, {})])
        self.run_all([OCC], lambda u: full(etag=None))
        self.run_all([OCC], lambda u: full(etag=None))
        self.assertEqual(self.calls, [(OCC, {"modified": LAST_MODIFIED})])

    def test_apaches_compressed_etag_goes_back_without_its_suffix(self):
        ssf = "https://ssf.gob.sv/seccion/noticias/feed/"
        self.seen(ssf)
        self.run_all([ssf], lambda u: full(etag='"9e5e2567-gzip"'))
        self.run_all([ssf], lambda u: not_modified())
        self.assertEqual(self.calls, [(ssf, {"etag": '"9e5e2567"', "modified": LAST_MODIFIED})])
        self.assertEqual(self.validators(ssf)[0], '"9e5e2567-gzip"', "stored as served")

    def test_courtlistener_is_never_sent_validators(self):
        self.seen(DOCKET)
        for _ in range(2):
            self.conn.execute("INSERT OR REPLACE INTO feed_schedule VALUES (?, 0, 1800, NULL)", (DOCKET,))
            self.conn.commit()
            self.run_all([DOCKET], lambda u: full(docket_entry(1)))
            self.assertEqual(self.calls, [(DOCKET, {})])
        self.assertIsNone(self.validators(DOCKET))

    def test_a_new_keyword_list_reads_the_feed_in_full(self):
        # A 304 skips re-scanning the items already in the feed, which a keyword added since can match.
        self.seen(FED)
        self.run_all([FED], lambda u: full(fed_item(1, title="Stablecoin guidance")))
        self.assertEqual(self.posted(), [])
        with patch("newshound.get_keywords", return_value=["crypto", "bitcoin", "stablecoin"]):
            self.run_all([FED], lambda u: full(fed_item(1, title="Stablecoin guidance")))
        self.assertEqual(self.calls, [(FED, {})])
        self.assertEqual(len(self.posted()), 1)

    def test_validators_are_saved_only_once_every_item_is_sent(self):
        self.seen(FED)
        # item 1 is posted; item 2's post fails on both of the run's attempts
        self.slack.chat_postMessage.side_effect = [None, RuntimeError("slack down"), RuntimeError("slack down")]
        self.run_all([FED], lambda u: full(fed_item(1), fed_item(2)))
        self.assertIsNone(self.validators(FED), "a 304 next run would never send item 2")
        self.slack.chat_postMessage.side_effect = None
        self.run_all([FED], lambda u: full(fed_item(1), fed_item(2)))
        self.assertEqual(self.calls, [(FED, {})])
        self.assertIn("/p/2", self.posted()[-1])
        self.assertEqual(sum("/p/1" in t for t in self.posted()), 1)
        self.assertEqual(self.validators(FED), ('"v1"', LAST_MODIFIED))

    def test_parse_feed_hands_the_validators_and_the_agent_to_feedparser(self):
        with patch("newshound.feedparser.parse") as parse, patch("newshound.FEED_AGENT", "newshound <a@b.c>"):
            newshound.parse_feed(FED, etag='"v1"', modified=LAST_MODIFIED)
            newshound.parse_feed(FED)
        self.assertEqual(parse.call_args_list[0].kwargs,
                         {"agent": "newshound <a@b.c>", "etag": '"v1"', "modified": LAST_MODIFIED})
        self.assertEqual(parse.call_args_list[1].kwargs, {"agent": "newshound <a@b.c>"})


class Backoff(EveryFeedRun):
    def health(self, url):
        return self.conn.execute("SELECT failures, skip_until FROM feed_health WHERE feed_source = ?",
                                 (url,)).fetchone()

    def expire(self):
        self.conn.execute("UPDATE feed_health SET skip_until = 0"); self.conn.commit()

    def test_the_tiers_and_one_line_per_step(self):
        import time as _t
        self.seen(FED)
        lines = []
        for n in range(1, 13):
            t0 = _t.time()
            self.assertEqual(self.run_all([FED], lambda u: full(status=404)), [FED], f"failure {n} was not read")
            lines += [l for l in self.out.splitlines() if l.startswith(("BACKOFF", "RECOVERED"))]
            failures, skip_until = self.health(FED)
            self.assertEqual(failures, n)
            want = 0 if n < 3 else 1800 if n < 6 else 7200 if n < 10 else 21600
            if want:
                got = skip_until - t0
                self.assertTrue(want - newshound.CL_RUN_SLACK - 1 <= got <= want - newshound.CL_RUN_SLACK + 5,
                                (n, got))
            else:
                self.assertIsNone(skip_until, n)
            self.expire()
        self.assertEqual(lines, [f"BACKOFF 30m after 3 failures (http=404): {FED}",
                                 f"BACKOFF 2h after 6 failures (http=404): {FED}",
                                 f"BACKOFF 6h after 10 failures (http=404): {FED}"])
        self.run_all([FED], lambda u: not_modified())
        self.assertIn(f"RECOVERED after 12 failures: {FED}", self.out.splitlines())
        self.assertIsNone(self.health(FED))
        self.run_all([FED], lambda u: full())
        self.assertNotIn("RECOVERED", self.out)

    def test_a_feed_in_backoff_is_not_fetched(self):
        self.seen(FED); self.seen(OCC)
        for _ in range(3):
            self.run_all([FED, OCC], lambda u: full(status=403) if u == FED else full())
        for _ in range(2):
            self.assertEqual(self.run_all([FED, OCC], lambda u: full()), [OCC])
            self.assertNotIn("BACKOFF", self.out)
        self.expire()
        self.assertEqual(self.run_all([FED, OCC], lambda u: full()), [FED, OCC])
        self.assertIn(f"RECOVERED after 3 failures: {FED}", self.out)

    def test_a_success_resets_the_streak(self):
        self.seen(FED)
        for answer in (full(status=500), full(status=500), not_modified(), full(status=500), full(status=500)):
            self.run_all([FED], lambda u: answer)
        self.assertEqual(self.health(FED), (2, None))
        self.assertNotIn("RECOVERED", self.out, "a feed never in backoff does not recover")

    def test_what_counts_as_a_failure(self):
        cases = [(TimeoutError("stalled"), "TimeoutError x2"),
                 (types.SimpleNamespace(entries=[], bozo_exception=OSError("refused")), "OSError"),
                 (full(status=403), "http=403")]
        for answer, why in cases:
            with self.subTest(why=why):
                self.conn.execute("DELETE FROM feed_health"); self.conn.commit()
                self.seen(FED)
                for _ in range(3):
                    self.run_all([FED], lambda u: answer)
                self.assertIn(f"BACKOFF 30m after 3 failures ({why}): {FED}", self.out)
        self.conn.execute("DELETE FROM feed_health"); self.conn.commit()
        for answer in (full(status=301), full(etag=None, modified=None), not_modified()):
            self.run_all([FED], lambda u: answer)
            self.assertIsNone(self.health(FED), answer)

    def test_a_failed_slack_post_is_not_held_against_the_feed(self):
        self.seen(FED)
        self.slack.chat_postMessage.side_effect = RuntimeError("slack down")
        for n in range(1, 4):
            self.run_all([FED], lambda u: full(fed_item(n)))
            self.assertEqual(len(self.failures), 1)
        self.assertIsNone(self.health(FED))

    def test_courtlistener_keeps_its_own_retry_and_gets_no_backoff(self):
        self.seen(DOCKET)
        for _ in range(4):
            self.conn.execute("INSERT OR REPLACE INTO feed_schedule VALUES (?, 0, 1800, NULL)", (DOCKET,))
            self.conn.commit()
            self.assertEqual(self.run_all([DOCKET], lambda u: full(status=503)), [DOCKET])
        self.assertIsNone(self.health(DOCKET))
        self.assertNotIn("BACKOFF", self.out)


class FeedList(unittest.TestCase):
    def test_no_feed_is_read_over_plain_http(self):
        # sec.gov answers http with a 403 or a redirect, justice.gov and cftc.gov with a 301: each read went through a
        # redirect (whose status hides a 304) or failed.
        path = os.path.join(os.path.dirname(newshound.__file__), "rss.md")
        with open(path, encoding="utf-8-sig") as fh:
            urls = [l.strip() for l in fh if l.strip()]
        self.assertEqual([u for u in urls if not u.startswith("https://")], [])
        # Each answered with a redirect (measured 2026-10-08), and a redirect's read stores no validators: the feed
        # would be downloaded in full every run. Listed is where each redirect ends.
        retired = {"https://www.justice.gov/atr/rss/atr_press.xml": "https://www.justice.gov/news/rss",
                   "https://www.sec.gov/rss/litigation/admin.xml":
                       "https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss",
                   "https://www.sec.gov/rss/news/press.xml": "https://www.sec.gov/news/pressreleases.rss",
                   "https://trumpstruth.org/feed": "https://www.trumpstruth.org/feed"}
        self.assertEqual([u for u in urls if u.replace("http://", "https://", 1) in retired], [])
        self.assertTrue(set(retired.values()) <= set(urls))


class ConnectTimeout(unittest.TestCase):
    """A dead address costs CONNECT_TIMEOUT, not the 30 s read timeout; the live one then gets the full timeout."""

    def test_dead_address_skipped_fast_then_full_read_timeout(self):
        tried, timeouts = [], []

        class FakeSock:
            def __init__(self, *a):
                self.t = None
            def settimeout(self, t):
                self.t = t
                timeouts.append(t)
            def connect(self, sa):
                tried.append((sa[0], self.t))
                if sa[0] == "10.0.0.1":
                    raise socket.timeout("timed out")
            def bind(self, a):
                pass
            def close(self):
                pass
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]
        with patch.object(newshound.socket, "getaddrinfo", return_value=addrs), \
             patch.object(newshound.socket, "socket", FakeSock):
            s = newshound._connect_fast(("ssf.example", 443))
        self.assertEqual(tried, [("10.0.0.1", newshound.CONNECT_TIMEOUT), ("10.0.0.2", newshound.CONNECT_TIMEOUT)])
        self.assertEqual(s.t, socket.getdefaulttimeout(), "after connecting, the normal read timeout (30 s)")

    def test_every_address_dead_raises_the_last_error(self):
        class Dead:
            def __init__(self, *a):
                pass
            def settimeout(self, t):
                pass
            def connect(self, sa):
                raise ConnectionRefusedError("refused")
            def close(self):
                pass
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch.object(newshound.socket, "getaddrinfo", return_value=addrs), \
             patch.object(newshound.socket, "socket", Dead):
            with self.assertRaises(ConnectionRefusedError):
                newshound._connect_fast(("x.example", 443))

    def test_installed_and_used_by_http_client(self):
        import http.client
        self.assertIs(socket.create_connection, newshound._connect_fast)
        self.assertIs(http.client.HTTPConnection("example.com")._create_connection, newshound._connect_fast)

    def test_an_explicit_shorter_timeout_bounds_the_connect_too(self):
        seen = []

        class Sock:
            def __init__(self, *a):
                pass
            def settimeout(self, t):
                seen.append(t)
            def connect(self, sa):
                pass
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]
        with patch.object(newshound.socket, "getaddrinfo", return_value=addrs), \
             patch.object(newshound.socket, "socket", Sock):
            newshound._connect_fast(("x.example", 443), timeout=2)
        self.assertEqual(seen, [2, 2])


class BareLinksWithoutApi(Base):
    """A bare entry's docket comes from its siblings or the entry log before any API call (2026-10-08)."""

    def setUp(self):
        super().setUp()
        newshound._docket_links.clear()
        newshound._log_dockets = None
        newshound.BARE_HOW.clear()
        self.feed = "https://www.courtlistener.com/feed/search/?q=%28openai%29"
        self.bare = {"title": "TradeSun, Inc. v. OpenAI OpCo, LLC", "link": "https://www.courtlistener.com"}

    def _no_api(self):
        return patch("urllib.request.urlopen", side_effect=AssertionError("the API must not be called"))

    def test_a_sibling_with_the_same_caption_gives_the_docket(self):
        sib = [self.bare, {"title": "TradeSun, Inc. v. OpenAI OpCo, LLC",
                           "link": "https://www.courtlistener.com/docket/74916072/8/tradesun-inc-v-openai-opco-llc/"}]
        with self._no_api(), patch.object(newshound, "courtlistener_token", return_value="tok"):
            link = newshound.resolve_bare_link(self.bare, self.feed, siblings=sib)
        self.assertEqual(link, "https://www.courtlistener.com/docket/74916072/tradesun-inc-v-openai-opco-llc/")
        self.assertEqual(newshound.BARE_HOW, {"sibling": 1})

    def test_the_entry_log_gives_the_docket_when_no_sibling_does(self):
        with open(newshound.ENTRY_LOG, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"feed": "https://www.courtlistener.com/docket/74916072/feed/",
                                 "title": "Entry #8 in TradeSun, Inc. v. OpenAI OpCo, LLC, 3:26-cv-08123",
                                 "link": "https://www.courtlistener.com/docket/74916072/8/tradesun-inc-v-openai-opco-llc/",
                                 "logged_ts": 1}) + "\n")
        with self._no_api(), patch.object(newshound, "courtlistener_token", return_value="tok"):
            link = newshound.resolve_bare_link(self.bare, self.feed, siblings=[self.bare])
        self.assertEqual(link, "https://www.courtlistener.com/docket/74916072/tradesun-inc-v-openai-opco-llc/")
        self.assertEqual(newshound.BARE_HOW, {"log": 1})

    def test_a_caption_naming_two_dockets_is_not_trusted(self):
        sib = [{"title": "USA v. Sealed", "link": "https://www.courtlistener.com/docket/1/2/usa-v-sealed/"},
               {"title": "USA v. Sealed", "link": "https://www.courtlistener.com/docket/9/1/usa-v-sealed/"}]
        bare = {"title": "USA v. Sealed", "link": "https://www.courtlistener.com"}
        with patch.object(newshound, "courtlistener_token", return_value=""):
            link = newshound.resolve_bare_link(bare, self.feed, siblings=sib)
        self.assertTrue(link.startswith("https://www.courtlistener.com/?"), link)
        self.assertEqual(newshound.BARE_HOW, {"caption-search": 1})

    def test_docket_feed_titles_reduce_to_their_caption(self):
        self.assertEqual(newshound.caption_of("Minute entry from 2026-10-07 in Sun v. Geffen, 1:25-cv-00995"),
                         "Sun v. Geffen")
        self.assertEqual(newshound.caption_of("Entry #514 in United States v. Approximately 127,271 bitcoin (BTC) "
                                              "previously stored at the virtual currency..., 1:25-cv-05745"),
                         "United States v. Approximately 127,271 bitcoin (BTC) previously stored at the virtual currency...")
        self.assertEqual(newshound.caption_of("TradeSun, Inc. v. OpenAI OpCo, LLC"), "TradeSun, Inc. v. OpenAI OpCo, LLC")


class RunLog(Base):
    """Every run leaves a heartbeat, even when no feed had anything new (the gather's staleness test reads it)."""

    def test_a_quiet_run_still_writes_a_heartbeat(self):
        with patch.object(newshound, "check_and_alert", return_value=None), \
             patch.object(newshound, "feed_due", return_value=True), \
             patch.object(newshound, "cl_due", return_value=False):
            newshound.run(self.conn, ["https://example.gov/a.xml", "https://www.courtlistener.com/docket/1/feed/"], set())
        rows = [json.loads(l) for l in open(newshound.RUN_LOG)]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["feeds"], r["read"], r["cl_total"], r["cl_read"], r["cl_not_due"]), (2, 1, 1, 0, 1))
        self.assertGreaterEqual(r["finished"], r["started"])

    def test_old_heartbeats_are_pruned_and_new_ones_appended(self):
        old = {"started": 1.0, "finished": 1.0}
        with open(newshound.RUN_LOG, "w") as fh:
            fh.write(json.dumps(old) + "\n")
        newshound.record_run(1e9, 1e9 + 5, 3, 3, 0, 0, 0, 0)
        newshound.record_run(1e9 + 600, 1e9 + 605, 3, 3, 0, 0, 0, 1)
        rows = [json.loads(l) for l in open(newshound.RUN_LOG)]
        self.assertEqual([r["failures"] for r in rows], [0, 1], "the 1970 line is pruned, both new runs kept")



BUSY = "https://www.courtlistener.com/docket/71639393/feed/"


def busy_entry(n, text, minute=False):
    frag = f"minute-entry-{n}" if minute else f"entry-{n}"
    url = f"https://www.courtlistener.com/docket/71639393/united-states-v-approximately/?order_by=desc#{frag}"
    return {"id": url, "link": url, "title": f"Entry #{n} in United States v. Approximately 127,271 bitcoin, "
            "1:25-cv-05745", "summary": text}


class KeyFilter(Base):
    """'# filter: key' on a docket line (Bennett, 2026-10-21): key filings one by one, the rest in one post."""

    def test_rss_marker_keeps_the_url_and_sets_the_mode(self):
        rss = os.path.join(self.tmp.name, "rss.md")
        with open(rss, "w") as f:
            f.write(f"# a comment\n{DOCKET}\n{BUSY}   # filter: key\n{SEC}  # filter: nonsense\n")
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            with patch("builtins.print") as said:
                urls = newshound.get_rss_feeds()
        finally:
            os.chdir(cwd)
        self.assertEqual(urls, [DOCKET, BUSY, SEC])            # the URL alone: no new key, no quiet first pass
        self.assertEqual(newshound.FEED_FILTERS, {BUSY: "key"})
        self.assertTrue(any("UNKNOWN FILTER" in str(c) for c in said.call_args_list))

    def busy_read(self):
        newshound.FEED_FILTERS.clear()
        newshound.FEED_FILTERS[BUSY] = "key"
        self.addCleanup(newshound.FEED_FILTERS.clear)
        self.seen(BUSY)
        parsed = feed(
            busy_entry(650, "ORDER: LuBian shall respond to the Government's 504 Motion to Compel by October 9."),
            busy_entry(651, "MOTION to Compel as to LuBian by United States of America. (Brown, Christopher)"),
            busy_entry(652, "ANSWER to [399] Answer to Complaint by Fishbeck Claimants. (Katz, Seth)"),
            busy_entry(653, "NOTICE by Chairnoff Claimants re [406] Answer to Complaint"),
            busy_entry(654, "MOTION to Withdraw as Attorney by the Law Offices of Joshua M. Ambush"),
            busy_entry(655, "ORDER granting 541 Motion for Leave to Appear Pro Hac Vice.", minute=True),
            busy_entry(656, "NOTICE OF APPEAL as to [506] Order on Motion for Summary Judgment by Ath Leepinyo"),
            busy_entry(657, "Letter MOTION for Extension of Time as to [504] MOTION to Compel by LuBian."))
        self.run_feed(BUSY, parsed)
        return parsed

    def test_key_filings_post_now_and_the_rest_wait_for_the_roll_up(self):
        parsed = self.busy_read()
        posts = self.posted()
        self.assertEqual(len(posts), 4, posts)                # the order, the government, the appeal, LuBian's motion
        self.assertTrue(all(p.startswith("🚨") for p in posts), posts)
        held = self.conn.execute("SELECT COUNT(*) FROM held_routine").fetchone()[0]
        self.assertEqual(held, 4)                             # answer, notice, withdrawal, pro hac vice grant
        self.slack.chat_postMessage.reset_mock()
        self.run_feed(BUSY, parsed)                           # held entries are recorded as sent: never posted alone
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM held_routine").fetchone()[0], 4)

    def test_the_roll_up_posts_once_a_day_from_nine(self):
        self.busy_read()
        self.slack.chat_postMessage.reset_mock()
        day = __import__("datetime").datetime(2026, 10, 22, 8, 55)
        self.assertEqual(newshound.digest_step(self.conn, day), 0)          # before 09:00: nothing
        self.assertEqual(self.posted(), [])
        self.assertEqual(newshound.digest_step(self.conn, day.replace(hour=9, minute=7)), 1)
        posts = self.posted()
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].startswith("🗂️ Daily roll-up: 4 routine filings"), posts[0])
        self.assertIn("Fishbeck Claimants", posts[0])
        self.assertIn("Docket: https://www.courtlistener.com/docket/71639393/", posts[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM held_routine").fetchone()[0], 0)
        self.slack.chat_postMessage.reset_mock()
        self.assertEqual(newshound.digest_step(self.conn, day.replace(hour=15)), 0)   # once a day
        self.assertEqual(self.posted(), [])

    def test_a_failed_roll_up_keeps_the_held_entries(self):
        self.busy_read()
        self.slack.chat_postMessage.side_effect = RuntimeError("slack down")
        day = __import__("datetime").datetime(2026, 10, 22, 9, 7)
        with self.assertRaises(RuntimeError):
            newshound.digest_step(self.conn, day)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM held_routine").fetchone()[0], 4)
        self.slack.chat_postMessage.side_effect = None
        self.assertEqual(newshound.digest_step(self.conn, day.replace(minute=17)), 1)   # the next run sends it

    def test_an_unmarked_docket_still_posts_every_entry(self):
        newshound.FEED_FILTERS.clear()
        self.seen(BUSY)
        self.run_feed(BUSY, feed(busy_entry(660, "ANSWER to Complaint by Fishbeck Claimants."),
                                 busy_entry(661, "NOTICE by Chairnoff Claimants re [406] Answer")))
        self.assertEqual(len([p for p in self.posted() if p.startswith("🚨")]), 2)

    def test_key_filing_reasons(self):
        cases = {
            "MEMORANDUM AND ORDER: the government's 197 motions to strike are granted": "the court's own entry",
            "STATUS REPORT Regarding TRIA Claimants' Claims by United States of America": "the government's filing",
            "MOTION for Reconsideration re: Memorandum and Order [506], filed by Ravi Anand.": "a motion",
            "CLAIM Verified Claim by Iranian Terrorism Victims.": "",
            "NOTICE of Appearance by Joshua Samuel Sohn on behalf of Iranian Terrorism Victims": "",
            "Electronic Index to Record on Appeal sent to US Court of Appeals.": "",
        }
        got = {t: newshound.key_filing(busy_entry(1, t))[1] for t in cases}
        self.assertEqual(got, cases)
