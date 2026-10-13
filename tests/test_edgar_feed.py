"""EDGAR's latest-filings feed as a newshound source (edgar_feed.py, 2026-10-12).

Every case runs edgar_feed.step against a real temporary newshound state DB, a temporary graph (listings) and a
temporary SEC pace table, with the transport's fake seam answering: nothing here leaves the process, no real pace
row is reserved, and no sleep is real. tests/fixtures/edgar_current_p0.atom is ONE real page (start=0, count=100),
captured once through the transport on 2026-10-12 (Columbus Day: its newest entry is Friday 2026-10-09 21:56 ET);
the other pages are built in the same shape.
"""
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

import edgar_feed as E
import newshound
from tests.test_newshound import Base

E._transport()
import transport  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
REAL_P0 = (FIXTURES / "edgar_current_p0.atom").read_bytes()
REAL_TOP = "0001214659-26-012761"            # the fixture's first entry (Form 4, Brian P. Friedman / Jefferies)
BITGO = ("1740604", "BitGo")                 # a KB-typed issuer with a Form 4 in the fixture
UA = "newshound (Protos research; test@example.invalid)"


def entry(form, name, cik, role, acc, updated):
    folder = acc.replace("-", "")
    return (f"<entry>\n<title>{form} - {name} ({int(cik):010d}) ({role})</title>\n"
            f'<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{int(cik)}/{folder}/'
            f'{acc}-index.htm"/>\n<summary type="html">\n &lt;b&gt;Filed:&lt;/b&gt; {updated[:10]} &lt;b&gt;AccNo:'
            f"&lt;/b&gt; {acc} &lt;b&gt;Size:&lt;/b&gt; 8 KB\n</summary>\n<updated>{updated}</updated>\n"
            f'<category scheme="https://www.sec.gov/" label="form type" term="{form}"/>\n'
            f"<id>urn:tag:sec.gov,2008:accession-number={acc}</id>\n</entry>\n")


def page(*entries):
    return ('<?xml version="1.0" encoding="ISO-8859-1" ?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
            "<title>Latest Filings</title>\n" + "".join(entries) + "</feed>\n").encode("latin-1")


def acc(n):
    return f"0009999999-26-{n:06d}"


def at(minute):
    """An acceptance time, later for a larger minute."""
    return f"2026-10-13T{9 + minute // 60:02d}:{minute % 60:02d}:00-04:00"


def filler(start, n, minute):
    """n unremarkable one-party filings (10-Q of an unwatched, unnamed issuer), accessions start.."""
    return [entry("10-Q", f"Plain Co {i}", 3000000 + i, "Filer", acc(i), at(minute)) for i in range(start, start + n)]


class EdgarBase(Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.graph = os.path.join(d, "graph.sqlite")
        g = sqlite3.connect(self.graph)
        g.execute("CREATE TABLE listings (path TEXT NOT NULL, kind TEXT NOT NULL, value TEXT, mic TEXT, ticker TEXT, "
                  "source TEXT NOT NULL, raw TEXT, status TEXT NOT NULL, flag TEXT, confidence TEXT, note TEXT)")
        g.executemany("INSERT INTO listings (path, kind, value, source, status) VALUES (?, ?, ?, ?, ?)", [
            ("Cryptocurrency/Entities/BitGo.md", "cik", "1740604", "frontmatter", "ok"),
            ("Automotive/Entities/Tesla.md", "cik", "1318605", "frontmatter", "ok"),
            ("Finance/Entities/SoftBank.md", "filer", "1065521", "frontmatter", "ok"),
            ("Finance/Entities/Proposed Only.md", "cik", "1111111", "proposed", "ok"),        # not typed: no watch
            ("Finance/Entities/Superseded.md", "cik", "2222222", "frontmatter", "superseded"),
        ])
        g.commit()
        g.close()
        self.watch_file = os.path.join(d, "edgar_watch.md")
        pathlib.Path(self.watch_file).write_text("# holders\n0002025889 Tether (holder)\n")
        self.lane = E.SecLane(db=os.path.join(d, "pace.sqlite"), sleep=lambda s: None)
        self.posts, self.sent, self.script = [], [], []
        self.edgar_patches = [patch.object(E, "PACE_DB", os.path.join(d, "pace.sqlite")),
                              patch.object(E, "GRAPH_DB", self.graph),
                              patch.object(E, "ua_configured", lambda: True),
                              patch.object(transport, "user_agent", lambda agent=None: UA)]
        for p in self.edgar_patches:
            p.start()
        transport.install(self.fake)
        self.out = io.StringIO()

    def tearDown(self):
        transport.uninstall()
        for p in self.edgar_patches:
            p.stop()
        super().tearDown()

    def fake(self, method, url, *, headers, data, json, timeout, allow_redirects):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.sent.append({"start": int(q["start"][0]), "count": q["count"][0], "ua": headers.get("User-Agent"),
                          "action": q["action"][0], "output": q["output"][0]})
        step = self.script.pop(0)
        if isinstance(step, bytes):
            return (200, {"Content-Type": "application/atom+xml"}, step)
        return step

    def step(self, now=None, max_pages=None):
        with contextlib.redirect_stdout(self.out):
            return E.step(self.conn, self.sent_keys(), self.posts.append, pause=None, lane=self.lane, now=now,
                          watch_path=self.watch_file, max_pages=max_pages)

    def sent_keys(self):
        if not hasattr(self, "_keys"):
            self._keys = newshound.get_sent_keys(self.conn)
        return self._keys

    def set_mark(self, a, updated):
        E._state(self.conn)
        E._set_state(self.conn, mark_acc=a, mark_updated=updated)

    def state(self):
        return E._state(self.conn)


class Paging(EdgarBase):
    def test_paging_stops_at_the_page_holding_the_mark(self):
        # guards: reading only page 0 at the afternoon peak (hundreds an hour) drops every filing past it; reading
        # on past the mark spends SEC requests on filings already handled
        self.set_mark(acc(150), at(10))
        p0 = page(*filler(0, 100, 30))
        p1 = page(*filler(100, 100, 20))
        p2 = page(*filler(150, 100, 10))           # never asked for: the mark is on page 1
        self.script = [p0, p1, p2]
        summ, fail = self.step()
        self.assertEqual([s["start"] for s in self.sent], [0, 100])
        self.assertTrue(all(s["count"] == "100" and s["action"] == "getcurrent" and s["output"] == "atom"
                            for s in self.sent))
        self.assertEqual((summ["pages"], summ["filings"], summ["cap_hit"], fail), (2, 150, False, None))
        self.assertEqual(self.state()["mark_acc"], acc(0), "the mark moves to the newest entry read")

    def test_an_entry_older_than_the_mark_stops_paging_when_the_mark_itself_left_the_feed(self):
        # guards: a withdrawn or re-sorted mark accession must not send a run paging to the cap every time
        self.set_mark("0000000000-26-000001", at(15))
        self.script = [page(*filler(0, 100, 30)), page(*filler(100, 100, 10))]
        summ, _ = self.step()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual((summ["filings"], summ["cap_hit"]), (100, False))

    def test_hitting_the_cap_is_a_loud_missed_window_and_the_mark_moves_on(self):
        # guards: a run that stops at the cap without saying so leaves a silent hole in the record
        self.set_mark("0000000000-26-000001", at(1))
        self.script = [page(*filler(i * 100, 100, 50 - i)) for i in range(3)]
        summ, fail = self.step(max_pages=3)
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(summ["cap_hit"])
        self.assertEqual(summ["status"], "cap")
        self.assertIn("EDGAR CAP HIT", self.out.getvalue())
        self.assertIn("NOT read", self.out.getvalue())
        self.assertEqual(self.state()["mark_acc"], acc(0))

    def test_the_feed_ending_before_the_mark_is_a_missed_window_too(self):
        self.set_mark("0000000000-26-000001", at(1))
        self.script = [page(*filler(0, 100, 30)), page()]
        summ, _ = self.step()
        self.assertTrue(summ["cap_hit"])
        self.assertIn("to the end of the feed", self.out.getvalue())

    def test_a_failure_part_way_posts_what_was_read_and_keeps_the_mark(self):
        # guards: advancing the mark after a failed page would skip that page's filings for good
        self.set_mark(acc(250), at(1))
        tesla = entry("8-K", "Tesla, Inc.", 1318605, "Filer", acc(5000), at(40))
        bitgo = entry("8-K", "BitGo Holdings, Inc.", 1740604, "Filer", acc(5001), at(20))
        p0 = page(tesla, *filler(0, 99, 39))
        p1 = page(bitgo, *filler(100, 99, 19))
        self.script = [p0, (503, {}, b"busy")]
        summ, fail = self.step()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Tesla", self.posts[0])
        self.assertEqual(self.state()["mark_acc"], acc(250), "the mark stays until a read reaches it")
        self.assertIsNotNone(fail)
        # next run reads down to the old mark again: Tesla is not posted twice, BitGo is posted
        self.script = [p0, p1, page(*filler(250, 10, 1))]
        self.step()
        self.assertEqual(len(self.posts), 2)
        self.assertIn("BitGo", self.posts[1])
        self.assertEqual(self.state()["mark_acc"], acc(5000))


class FirstRun(EdgarBase):
    def test_the_first_run_reads_one_page_records_its_matches_quietly_and_sets_the_mark(self):
        # guards: a first run that posts would flood the channel with ~100 old filings' matches
        self.script = [REAL_P0]
        summ, fail = self.step()
        self.assertEqual(len(self.sent), 1, "one page only")
        self.assertEqual(self.posts, [])
        self.assertTrue(summ["seeded"])
        self.assertIsNone(fail)
        self.assertEqual(self.state()["mark_acc"], REAL_TOP)
        self.assertGreaterEqual(summ["matched"], 1, "the fixture holds BitGo's Form 4")
        self.assertIn("EDGAR NEW SOURCE", self.out.getvalue())
        # the next run sees the same page (EDGAR closed) and posts nothing
        self.script = [REAL_P0]
        summ, _ = self.step()
        self.assertEqual((self.posts, summ["filings"], len(self.sent)), ([], 0, 2))

    def test_a_filing_recorded_by_the_quiet_pass_is_not_posted_if_reread(self):
        # guards: the quiet pass recording nothing, so the boundary page's old matches post on the next run
        self.script = [REAL_P0]
        self.step()
        E._set_state(self.conn, mark_acc="0000000000-26-000001", mark_updated="2026-10-09T00:00:00-04:00")
        self.script = [REAL_P0, page()]
        self.step()
        self.assertEqual(self.posts, [])

    def test_the_real_page_parses_whole(self):
        entries, unparsed = E.parse_page(REAL_P0)
        self.assertEqual((len(entries), unparsed), (100, 0))
        self.assertEqual(entries[0]["acc"], REAL_TOP)
        roles = {e["role"] for e in entries}
        self.assertTrue({"Issuer", "Reporting"} <= roles, roles)

    def test_a_page_that_is_not_atom_is_a_parse_failure_not_an_empty_window(self):
        # guards: an HTML error page in a 200 read as "no new filings"
        self.set_mark(acc(1), at(1))
        for body in (b"<html><body>Service Unavailable</body></html>",      # XML, not Atom
                     b"<html><body>Service Unavailable<br></body>"):          # not XML at all
            self.script = [body]
            summ, fail = self.step()
            self.assertEqual(summ["status"], "parse", body)
            self.assertIn("parse", fail)
            self.assertEqual(self.state()["mark_acc"], acc(1))


class Gates(EdgarBase):
    def run_one(self, *entries):
        self.set_mark(acc(999), at(0))
        self.script = [page(*entries, entry("10-Q", "Old Co", 3999999, "Filer", acc(999), at(0)))]
        return self.step()

    def test_a_watched_cik_matches_as_issuer(self):
        self.run_one(entry("4", "Doe Jane", 2000001, "Reporting", acc(1), at(5)),
                     entry("4", "BitGo Holdings, Inc.", 1740604, "Issuer", acc(1), at(5)))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("watched CIK 1740604 (KB: BitGo) as Issuer", self.posts[0])

    def test_a_watched_cik_matches_as_reporting_owner(self):
        # guards: matching the Issuer alone misses Tether's own 13D/G and Form 4 filings about other companies
        self.run_one(entry("4", "Tether Investments S.A. de C.V.", 2025889, "Reporting", acc(2), at(5)),
                     entry("4", "Unwatched Corp", 2000002, "Issuer", acc(2), at(5)))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("watched CIK 2025889 (list: Tether (holder)) as Reporting", self.posts[0])

    def test_kb_filer_ciks_are_watched_and_untyped_listings_are_not(self):
        # guards: dropping kind 'filer' (holders), or reading proposed / superseded rows as typed
        w = E.load_watch(self.conn, watch_path=self.watch_file, say=lambda *_: None)
        self.assertIn("1065521", w)
        self.assertNotIn("1111111", w)
        self.assertNotIn("2222222", w)
        self.assertEqual(w["2025889"][1], "list")

    def test_an_unreadable_graph_falls_back_to_the_last_list_and_says_so(self):
        # guards: a locked or missing graph silently watching nothing but the extra list
        E.load_watch(self.conn, watch_path=self.watch_file, say=lambda *_: None)
        said = []
        with patch.object(E, "GRAPH_DB", os.path.join(self.tmp.name, "missing", "graph.sqlite")):
            w = E.load_watch(self.conn, watch_path=self.watch_file, say=said.append)
        self.assertIn("1318605", w)
        self.assertTrue(said and "last list" in said[0])

    def test_name_keywords_match_whole_words(self):
        rules = E.load_keywords()

        def hits(name, form="8-K"):
            return E.keyword_hits([name], form, rules)
        self.assertTrue(hits("Bitcoin Depot Inc."))
        self.assertTrue(hits("BTC Digital Ltd."))
        self.assertTrue(hits("The Ether Machine, Inc.", "10-Q"))
        self.assertTrue(hits("XRP Holdings Corp", "D"))
        self.assertTrue(hits("Tron Inc.", "4"))
        self.assertTrue(hits("Strive Digital Assets Fund", "D"))
        self.assertTrue(hits("SOL Treasury Corp", "S-1"), "treasury + a token word")
        # negatives: no whole word, or the wrong form for mining, or treasury alone
        self.assertFalse(hits("Tronox Holdings plc"))
        self.assertFalse(hits("Electron Capital Partners"))
        self.assertFalse(hits("Cryptologic Holdings"))
        self.assertFalse(hits("Treasury Wine Estates"))
        self.assertFalse(hits("Mining Resources Ltd", "10-K"))
        self.assertTrue(hits("Mining Resources Ltd", "8-K"))
        self.assertTrue(hits("Mining Resources Ltd", "SC 13D/A"))
        self.assertTrue(hits("Mining Resources Ltd", "424B4"))

    def test_an_unwatched_unnamed_filing_is_not_posted(self):
        self.run_one(entry("8-K", "Plain Industries", 2000003, "Filer", acc(3), at(5)))
        self.assertEqual(self.posts, [])


class Ranking(EdgarBase):
    def test_the_rank_of_each_form(self):
        # guards: an 8-K or 13D/A slipping into the quiet batch, or every Form 4 of a crypto-named filer pinging
        for form in ("8-K", "8-K/A", "6-K", "SC 13D", "SC 13D/A", "SCHEDULE 13G/A", "S-1", "S-1/A", "S-3ASR", "F-4",
                     "424B3", "424B5", "DEFM14A", "DEF 14A", "PREM14A", "25-NSE", "15-12G", "15-12B", "NT 10-K",
                     "NT 10-Q", "SC TO-T", "SC TO-T/A", "SC TO-I", "SC 14D9", "SC 13E3"):
            self.assertEqual(E.rank(form, watched=False), "priority", form)
        for form in ("3", "4", "4/A", "5", "144"):
            self.assertEqual(E.rank(form, watched=True), "priority", form)
            self.assertEqual(E.rank(form, watched=False), "routine", form)
        for form in ("10-Q", "10-K", "13F-HR", "D", "SC TO-C", "DEFA14A", "S-8", "N-PX"):
            self.assertEqual(E.rank(form, watched=True), "routine", form)

    def test_priority_posts_alone_and_routine_goes_in_one_batch(self):
        self.set_mark(acc(999), at(0))
        self.script = [page(entry("8-K", "Tesla, Inc.", 1318605, "Filer", acc(1), at(9)),
                            entry("10-Q", "Tesla, Inc.", 1318605, "Filer", acc(2), at(8)),
                            entry("4", "Doe Jane", 2000001, "Reporting", acc(3), at(7)),
                            entry("4", "Bitcoin Depot Inc.", 1901799, "Issuer", acc(3), at(7)),
                            entry("10-Q", "Old Co", 3999999, "Filer", acc(999), at(0)))]
        summ, _ = self.step()
        self.assertEqual((summ["posted"], summ["routine"]), (1, 2))
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.posts[0].startswith("\U0001F6A8 EDGAR 8-K: Tesla"))
        self.assertTrue(self.posts[1].startswith("EDGAR routine filings (2)"))
        self.assertIn("name keyword 'bitcoin'", self.posts[1])


class Dedupe(EdgarBase):
    def test_issuer_and_reporting_entries_of_one_filing_post_once_naming_both(self):
        self.set_mark(acc(999), at(0))
        self.script = [page(entry("4", "Musk Elon", 1494730, "Reporting", acc(1), at(5)),
                            entry("4", "Tesla, Inc.", 1318605, "Issuer", acc(1), at(5)),
                            entry("10-Q", "Old Co", 3999999, "Filer", acc(999), at(0)))]
        self.step()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Musk Elon (CIK 1494730, Reporting); Tesla, Inc. (CIK 1318605, Issuer)", self.posts[0])
        self.assertIn(f"sec:{acc(1)}", newshound.get_sent_keys(self.conn))

    def test_a_filing_another_source_already_raised_is_not_raised_again(self):
        # guards: the same filing reaching Slack from two sources
        link = f"https://www.sec.gov/Archives/edgar/data/1318605/{acc(1).replace('-', '')}/{acc(1)}-index.htm"
        newshound.record_key(self.conn, newshound.canonical_key("x", link), "https://example.org/feed")
        self.set_mark(acc(999), at(0))
        self.script = [page(entry("8-K", "Tesla, Inc.", 1318605, "Filer", acc(1), at(5)),
                            entry("10-Q", "Old Co", 3999999, "Filer", acc(999), at(0)))]
        self.step()
        self.assertEqual(self.posts, [])

    def test_canonical_key_reads_an_archives_link_as_its_accession_from_any_folder(self):
        a = "0001214659-26-012761"
        for link in ("https://www.sec.gov/Archives/edgar/data/1275002/000121465926012761/0001214659-26-012761-index.htm",
                     "https://www.sec.gov/Archives/edgar/data/96223/000121465926012761/0001214659-26-012761-index.htm",
                     "https://www.sec.gov/Archives/edgar/data/96223/000121465926012761/form4.xml",
                     "https://www.sec.gov/Archives/edgar/data/96223/0001214659-26-012761.txt"):
            self.assertEqual(newshound.canonical_key("x", link), f"sec:{a}", link)
        press = "https://www.sec.gov/newsroom/press-releases/2026-100-x"
        self.assertEqual(newshound.canonical_key("x", press), press, "other sec.gov links keep their old key")


class Courtesy(EdgarBase):
    def test_every_request_carries_the_declared_user_agent_and_waits_on_the_shared_lane(self):
        self.set_mark(acc(150), at(10))
        self.script = [page(*filler(0, 100, 30)), page(*filler(100, 100, 20))]
        t0 = time.time()
        self.step()
        self.assertEqual({s["ua"] for s in self.sent}, {UA})
        self.assertGreaterEqual(self.lane.peek(), t0 + E.GAP, "two requests: the second a gap after the first")

    def test_the_lane_shares_edgar_ios_table_and_refuses_a_far_slot_without_reserving_it(self):
        slept = []
        lane = E.SecLane(gap=1.0, db=os.path.join(self.tmp.name, "lane.sqlite"), sleep=slept.append, clock=lambda: 100.0)
        lane.wait()
        lane.wait()
        self.assertEqual(slept, [1.0])
        lane.block_until(500.0)
        with self.assertRaises(E.LaneBusy):
            lane.wait(max_wait=30)
        self.assertEqual(lane.peek(), 500.0, "a refused slot is not written")
        con = sqlite3.connect(os.path.join(self.tmp.name, "lane.sqlite"))
        self.assertEqual(con.execute("SELECT id FROM pace").fetchall(), [(0,)], "edgar_io's one-row schema")
        con.close()

    def test_no_declared_contact_is_a_config_error_and_nothing_is_sent(self):
        # guards: an undeclared UA reaching SEC (403 every run) instead of a clear configuration error
        self.set_mark(acc(1), at(1))
        with patch.object(E, "ua_configured", lambda: False):
            summ, fail = self.step()
        self.assertEqual(self.sent, [])
        self.assertEqual(summ["status"], "config")
        self.assertIn("config", fail)
        self.assertIn("EDGAR CONFIG ERROR", self.out.getvalue())

    def test_a_403_is_a_config_error_not_retried_and_later_runs_send_nothing(self):
        self.set_mark(acc(1), at(1))
        now = time.time()
        self.script = [(403, {}, b"<html>Your Request Originates from an Undeclared Automated Tool</html>")]
        summ, fail = self.step(now=now)
        self.assertEqual(len(self.sent), 1, "a 403 is never retried")
        self.assertEqual(summ["status"], "config")
        self.assertIn("config", fail)
        summ, fail = self.step(now=now + 600)
        self.assertEqual(len(self.sent), 1, "inside the block nothing is sent")
        self.assertIn("config", fail, "and every run still says so")
        self.assertEqual(self.state()["mark_acc"], acc(1))

    def test_a_rate_threshold_403_is_a_ten_minute_block_not_a_config_error(self):
        self.set_mark(acc(1), at(1))
        now = time.time()
        self.script = [(403, {}, b"<html><h1>Request Rate Threshold Exceeded</h1></html>")]
        summ, fail = self.step(now=now)
        self.assertEqual((summ["status"], fail), ("throttled", None))
        self.assertGreaterEqual(float(self.state()["blocked_until"]), now + E.RATE_BLOCK - 1)
        self.assertGreaterEqual(self.lane.peek(), now + E.RATE_BLOCK - 1, "every SEC client sits it out")

    def test_a_429_is_not_retried_and_its_retry_after_holds_every_later_run(self):
        # guards: retrying into a 429, or a later run (or another SEC client) sending before Retry-After ends
        self.set_mark(acc(1), at(1))
        now = time.time()
        self.script = [(429, {"Retry-After": "120"}, b"slow down")]
        summ, fail = self.step(now=now)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual((summ["status"], fail), ("throttled", None))
        self.assertAlmostEqual(float(self.state()["blocked_until"]), now + 120, delta=2)
        self.assertGreaterEqual(self.lane.peek(), now + 119, "the shared lane carries the block")
        summ, _ = self.step(now=now + 60)
        self.assertEqual((len(self.sent), summ["status"]), (1, "blocked"))
        self.assertEqual(self.state()["mark_acc"], acc(1))

    def test_the_fixture_holds_no_contact(self):
        # guards: a captured page carrying our contact into the repo
        ru = transport._research_ua()
        contact = ru._contact()
        if not contact:
            self.skipTest("no research contact configured here")
        self.assertNotIn(contact.encode("latin-1", "replace"), REAL_P0)
        self.assertNotIn(b"Protos research", REAL_P0)


class RunWiring(Base):
    def test_the_run_reads_edgar_first_and_logs_its_counts_in_the_heartbeat(self):
        calls = []

        def edgar(conn, keys):
            calls.append(conn)
            return {"pages": 2, "filings": 140, "matched": 3, "cap_hit": False, "status": "ok"}, None
        with contextlib.redirect_stdout(io.StringIO()):
            failures = newshound.run(self.conn, [], edgar=edgar)
        self.assertEqual((len(calls), failures), (1, []))
        with open(newshound.RUN_LOG) as fh:
            row = json.loads(fh.readlines()[-1])
        self.assertEqual(row["edgar"]["filings"], 140)
        self.assertEqual(row["failures"], 0)

    def test_an_edgar_failure_counts_and_a_crash_never_costs_the_feeds(self):
        def broken(conn, keys):
            raise ValueError("boom")
        with contextlib.redirect_stdout(io.StringIO()), \
                patch("newshound.check_and_alert", return_value=None) as feeds:
            failures = newshound.run(self.conn, ["https://example.org/feed"], edgar=broken)
        self.assertEqual(feeds.call_count, 1)
        self.assertEqual(failures[0][0], newshound.EDGAR_SOURCE)
        with open(newshound.RUN_LOG) as fh:
            row = json.loads(fh.readlines()[-1])
        self.assertEqual((row["edgar"]["status"], row["failures"]), ("crashed", 1))

    def test_the_main_entry_point_runs_the_edgar_step_only_when_switched_on(self):
        main = pathlib.Path(newshound.__file__).read_text().split('if __name__ == "__main__":')[1]
        self.assertIn('edgar=edgar_step if os.environ.get("NEWSHOUND_EDGAR") == "1" else None', main)


if __name__ == "__main__":
    unittest.main()
