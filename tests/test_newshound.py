"""newshound's alerting state: what gets sent, once, and what never floods.

Each case runs check_and_alert against a real temporary SQLite database with the
feed and Slack mocked, so the tests exercise the same SQL the service runs.
The two tests this file replaced called check_and_alert with a signature it no
longer had and had been failing since before the dedupe change.
"""
import json
import os
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
        self.patches = [patch("newshound.get_slack_client", return_value=self.slack),
                        patch("newshound.time.sleep"),
                        patch("newshound.get_keywords", return_value=["crypto", "bitcoin"]),
                        patch.dict(os.environ, {"LOCAL_TESTING": "False"})]
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
        resp = MagicMock(); resp.read.return_value = body
        resp.__enter__.return_value = resp
        with patch("newshound.courtlistener_token", return_value="t"), \
             patch("newshound.urllib.request.urlopen", return_value=resp) as opened:
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22"),
                                       bare("Coinbase, Inc. v. Honscha", "2026-09-23")))
        self.assertTrue(all("/docket/74811111/" in t for t in self.posted()), self.posted())
        self.assertEqual(opened.call_count, 1, "one API call per feed, not per entry")
        self.assertEqual(len(self.paced), 1, "the API call is paced through the shared window")

    def test_a_busy_shared_window_falls_back_to_a_caption_search_without_calling_the_api(self):
        import clpace
        self.seen(SEARCH)
        with patch.object(clpace, "pace", side_effect=clpace.PaceWait("cl-pace: next slot in 900s")), \
             patch("newshound.courtlistener_token", return_value="t"), \
             patch("newshound.urllib.request.urlopen") as opened:
            self.run_feed(SEARCH, feed(bare("Coinbase, Inc. v. Honscha", "2026-09-22")))
        self.assertEqual(opened.call_count, 0, "a refused slot never becomes an unpaced call")
        self.assertIn("caseName", self.posted()[0])

    def test_an_api_failure_still_sends_a_usable_link(self):
        self.seen(SEARCH)
        with patch("newshound.courtlistener_token", return_value="t"), \
             patch("newshound.urllib.request.urlopen", side_effect=OSError("429")):
            self.run_feed(SEARCH, feed(bare("Yuyuan Gao v. Coinbase, Inc.", "2026-09-22")))
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


if __name__ == "__main__":
    unittest.main()
