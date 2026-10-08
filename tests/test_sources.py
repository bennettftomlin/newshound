"""triage/sources.py's CourtListener client, offline (2026-10-08): one client (cl_match.cl_get) with one budget,
a typed budget exception that is never swallowed, one docket-entries call per entry with every attachment cached,
the graph before the API for a docket, and a cache two module instances share. Every test points the pace DB, the
cache and the graph at temporary files, and replaces urllib's urlopen: nothing here can reach courtlistener.com."""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

NH = pathlib.Path(__file__).resolve().parent.parent
SOURCES = NH / "triage" / "sources.py"
sys.path.insert(0, str(NH))
import clpace  # noqa: E402

CL = clpace._cl_match()


def load(name):
    """A fresh module instance of triage/sources.py under its own name (the gather imports it as `sources`, the
    triage as `triage.sources`: two instances in two processes, or one process)."""
    spec = importlib.util.spec_from_file_location(name, SOURCES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def doc(att, text="", number="1", desc=None):
    return {"id": 100 + (att or 0), "document_number": number, "attachment_number": att,
            "description": desc or (f"Exhibit {att}" if att else "Complaint"), "short_description": "Complaint",
            "page_count": 3, "is_available": True, "plain_text": text,
            "absolute_url": f"/docket/74935682/1/{f'{att}/' if att else ''}x/"}


ENTRY = {"results": [{"entry_number": 1, "date_filed": "2026-10-07", "description": "COMPLAINT against OpenAI",
                      "recap_documents": [doc(None, "COMPLAINT text " * 20), doc(1, "Exhibit one text"),
                                          doc(2, "Exhibit two text")]}]}
LINK = "https://www.courtlistener.com/docket/74935682/1/{}usa-today-co-inc-v-openai-foundation/"


class Base(unittest.TestCase):
    """Temp pace DB, cache and graph; a fake cl_match.cl_get recording (endpoint, params) unless a test uses the
    real one; urlopen replaced by a tripwire."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = pathlib.Path(self.td.name)
        self.saved = (CL.PACE_DB, CL.cl_get, urllib.request.urlopen, os.environ.get("COURTLISTENER_TOKEN"),
                      CL.PACE_MAX_WAIT)
        CL.PACE_DB = str(td / "pace.sqlite")
        os.environ["COURTLISTENER_TOKEN"] = "test-token"

        def tripwire(*a, **k):
            raise AssertionError("a test reached urlopen without a fake")
        urllib.request.urlopen = tripwire
        self.S = load("sources_t")
        self.S.CACHE_DB = td / "cache.sqlite"
        self.S.GRAPH_DB = td / "graph.sqlite"
        self.calls = []
        self.replies = {}

        def fake_get(endpoint, params, tok, url=None):
            self.calls.append((endpoint.rsplit("/v4/", 1)[-1], dict(params)))
            r = self.replies.get(endpoint.rsplit("/v4/", 1)[-1])
            if isinstance(r, BaseException) or (isinstance(r, type) and issubclass(r, BaseException)):
                raise r
            return json.loads(json.dumps(r if r is not None else {"results": []}))
        CL.cl_get = fake_get

    def tearDown(self):
        CL.PACE_DB, CL.cl_get, urllib.request.urlopen, tok, CL.PACE_MAX_WAIT = self.saved
        if tok is None:
            os.environ.pop("COURTLISTENER_TOKEN", None)
        else:
            os.environ["COURTLISTENER_TOKEN"] = tok
        self.td.cleanup()

    def graph(self, rows):
        con = sqlite3.connect(self.S.GRAPH_DB)
        con.execute("CREATE TABLE cl_dockets (docket_id INT PRIMARY KEY, docket_number TEXT, case_name TEXT, "
                    "court TEXT, date_filed TEXT, date_terminated TEXT, pacer_case_id TEXT, fetched_at INT)")
        con.executemany("INSERT INTO cl_dockets VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def ends(self):
        return [c[0] for c in self.calls]


class BudgetPropagates(Base):
    def test_pace_exhausted_leaves_cl_document_as_CLBudget(self):
        self.replies["docket-entries/"] = CL.PaceExhausted("day cap")
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_document(LINK.format(""))
        self.assertEqual(self.ends(), ["docket-entries/"], "stopped at the first call")

    def test_pacewait_and_a_throttle_body_are_budget_too(self):
        for exc in (clpace.PaceWait("2 min"), RuntimeError("throttled: Request was throttled.")):
            self.replies["docket-entries/"] = exc
            with self.assertRaises(self.S.CLBudget, msg=repr(exc)):
                self.S.cl_document(LINK.format(""))

    def test_budget_from_cl_docket_and_search_propagates(self):
        self.replies["dockets/1/"] = CL.PaceExhausted("x")
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_docket("1")
        self.replies["search/"] = CL.PaceExhausted("x")
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_search_docs("openai")

    def test_other_errors_still_degrade_to_None_and_are_not_cached(self):
        self.replies["docket-entries/"] = urllib.error.HTTPError("u", 500, "boom", {}, None)
        self.assertIsNone(self.S.cl_document(LINK.format("")))
        self.replies["docket-entries/"] = ENTRY
        got = self.S.cl_document(LINK.format(""), case={"name": "USA Today v. OpenAI"})
        self.assertTrue(got["text_excerpt"].startswith("COMPLAINT text"), "the failure was not cached")
        self.assertEqual(self.ends(), ["docket-entries/", "docket-entries/"])

    def test_the_real_client_records_retry_after_and_the_next_call_never_sends(self):
        CL.cl_get = self.saved[1]                     # the real cl_match.cl_get, through a fake urlopen
        sent = []

        def urlopen(req, timeout=None):
            sent.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, "throttled", {"Retry-After": "3600"}, None)
        urllib.request.urlopen = urlopen
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_get("search/", q="x")
        self.assertEqual(len(sent), 1, "a long Retry-After is not retried")
        con = sqlite3.connect(CL.PACE_DB)
        until = con.execute("SELECT max(until) FROM cl_block").fetchone()[0]
        con.close()
        self.assertGreater(until, time.time() + 3000, "CourtListener's Retry-After recorded for every process")
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_get("search/", q="y")
        self.assertEqual(len(sent), 1, "the next call raises before it sends")
        self.assertEqual(CL.PACE_MAX_WAIT, self.saved[4], "the short wait is restored after the call")

    def test_a_slot_three_minutes_away_is_refused_not_slept(self):
        CL.cl_get = self.saved[1]
        con = CL._pace_con()
        con.execute("INSERT INTO cl_block (until, why) VALUES (?, 'test')", (time.time() + 180,))
        con.commit()
        con.close()
        self.assertGreater(CL.PACE_MAX_WAIT, 180, "cl_match alone would wait it out")
        t0 = time.time()
        with self.assertRaises(clpace.CLBudget):
            self.S.cl_get("search/", q="x")
        self.assertLess(time.time() - t0, 5, "newshound's callers wait at most clpace.MAX_WAIT")

    def test_requests_say_newshound_triage(self):
        req = urllib.request.Request("https://www.courtlistener.com/api/rest/v4/search/",
                                     headers={"User-Agent": "claude-knowledge doc-text pipeline"})
        h = self.S._UAHandler()
        h.https_request(req)
        self.assertEqual(req.get_header("User-agent"), "claude-knowledge doc-text pipeline", "only while cl_get calls")
        tok = self.S._UA_ON.set(True)
        try:
            h.https_request(req)
        finally:
            self.S._UA_ON.reset(tok)
        got = req.get_header("User-agent") or ""
        # the declared form, with or without a configured contact; compared without echoing the contact on failure
        self.assertTrue(got == self.S.UA or got.startswith("newshound-triage (Protos research; "),
                        "the relabelled User-Agent is triage's declared one")
        self.assertTrue(got == self.S._ua(), "the handler sends _ua()")
        self.S._ensure_ua_handler()
        self.assertTrue(any(type(x).__name__ == "_UAHandler" for x in urllib.request._opener.handlers))


class OneCallPerEntry(Base):
    def test_one_docket_entries_call_caches_every_attachment(self):
        self.replies["docket-entries/"] = ENTRY
        case = {"name": "USA Today Co., Inc. v. OpenAI Foundation", "court": "nysd"}
        a = self.S.cl_document(LINK.format(""), case=case)
        b = self.S.cl_document(LINK.format("1/"), case=case)
        c = self.S.cl_document(LINK.format("2/"), case=case)
        self.assertEqual(self.ends(), ["docket-entries/"], "one call for the entry and its two exhibits")
        fields = self.calls[0][1]["fields"]
        self.assertIn("recap_documents__plain_text", fields)
        self.assertEqual(self.calls[0][1]["entry_number"], 1)
        self.assertEqual((b["text_excerpt"], c["text_excerpt"]), ("Exhibit one text", "Exhibit two text"))
        self.assertEqual(a["filing"]["description"], "COMPLAINT against OpenAI")
        self.assertEqual(b["filing"]["attachment_number"], 1)
        self.assertLessEqual(len(a["text_excerpt"]), self.S.EXCERPT, "only the opening excerpt is kept")
        self.assertIs(a["case"], case, "a case the caller holds is used: no docket lookup")
        self.assertEqual(self.S.CL_STATS["doc_cache"], 2)

    def test_fallback_when_the_nested_documents_are_absent(self):
        self.replies["docket-entries/"] = {"results": [{"entry_number": 1, "date_filed": "2026-10-07",
                                                        "description": "COMPLAINT"}]}
        self.replies["recap-documents/"] = {"results": [doc(None, "main text"), doc(1, "exhibit text")]}
        got = self.S.cl_document(LINK.format("1/"), case={"name": "x"})
        self.assertEqual(self.ends(), ["docket-entries/", "recap-documents/"])
        self.assertEqual(got["text_excerpt"], "exhibit text")
        self.assertEqual(got["filing"]["entry_date"], "2026-10-07", "the entry's date still comes through")
        self.S.cl_document(LINK.format(""), case={"name": "x"})
        self.assertEqual(len(self.calls), 2, "the fallback's documents are cached too")

    def test_a_document_recap_lacks_gets_no_text_and_the_short_ttl(self):
        self.replies["docket-entries/"] = ENTRY
        got = self.S.cl_document(LINK.format("7/"), case={"name": "x"})
        self.assertNotIn("text_excerpt", got)
        con = sqlite3.connect(self.S.CACHE_DB)
        row = con.execute("SELECT has_text FROM documents WHERE attachment_number=7").fetchone()
        con.close()
        self.assertEqual(row, (0,))


class DocketGraphFirst(Base):
    def test_graph_then_cache_then_api(self):
        self.graph([(73709688, "1:26-mj-00300", "United States v. Yaroch", "vaed", "2026-08-03", None, "1", 0)])
        self.replies["dockets/5/"] = {"case_name": "Doe v. Roe", "court_id": "nysd", "docket_number": "1:26-cv-1",
                                      "date_filed": "2026-10-01", "nature_of_suit": "Contract", "cause": "28:1332"}
        g = self.S.cl_docket("73709688")
        self.assertEqual((g["name"], g["court"], g["docket_number"], g["filed"]),
                         ("United States v. Yaroch", "vaed", "1:26-mj-00300", "2026-08-03"))
        self.assertEqual(self.calls, [], "a docket the graph holds costs no call")
        a = self.S.cl_docket("5")
        self.assertEqual(a["nature_of_suit"], "Contract")
        self.S._docket_cache.clear()
        self.assertEqual(self.S.cl_docket("5"), a)
        self.assertEqual(self.ends(), ["dockets/5/"], "then the cache answers")

    def test_a_missing_graph_or_table_is_no_error(self):
        self.replies["dockets/9/"] = {"case_name": "X v. Y", "court_id": "dde"}
        self.assertEqual(self.S.cl_docket("9")["name"], "X v. Y")
        self.S.GRAPH_DB.write_bytes(b"")                # an empty file: no cl_dockets table
        self.S._docket_cache.clear()
        self.assertEqual(self.S.cl_docket("9")["name"], "X v. Y")

    def test_a_failed_docket_is_not_cached(self):
        self.replies["dockets/9/"] = urllib.error.HTTPError("u", 503, "down", {}, None)
        self.assertEqual(self.S.cl_docket("9"), {"error": "HTTPError"})
        con = sqlite3.connect(self.S.CACHE_DB)
        self.assertEqual(con.execute("SELECT count(*) FROM dockets").fetchone()[0], 0)
        con.close()


class SharedCache(Base):
    def test_two_module_instances_share_one_cache_file(self):
        other = load("sources_other")
        other.CACHE_DB, other.GRAPH_DB = self.S.CACHE_DB, self.S.GRAPH_DB
        self.replies["docket-entries/"] = ENTRY
        self.replies["search/"] = {"results": [{"caseName": "USA Today v. OpenAI", "docket_id": 74935682,
                                                "recap_documents": [{"absolute_url": "/docket/74935682/1/x/"}]}]}
        self.S.cl_document(LINK.format(""), case={"name": "x"})
        self.S.cl_search_docs("openai", pages=1)
        self.assertEqual(len(self.calls), 2)
        got = other.cl_document(LINK.format("2/"), case={"name": "x"})
        docs = other.cl_search_docs("openai", pages=1)
        self.assertEqual(len(self.calls), 2, "the second instance asked the API nothing")
        self.assertEqual(got["text_excerpt"], "Exhibit two text")
        self.assertEqual(docs[0]["case"]["name"], "USA Today v. OpenAI")
        con = sqlite3.connect(self.S.CACHE_DB)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        con.close()


if __name__ == "__main__":
    unittest.main()
