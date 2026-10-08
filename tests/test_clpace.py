"""clpace: newshound's CourtListener calls share claude_knowledge's cl-pace window (2026-10-08)."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clpace  # noqa: E402


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT count(*) FROM cl_window").fetchone()[0]
    finally:
        con.close()


def test_pace_reserves_shared_slots_and_refuses_a_long_wait():
    cl = clpace._cl_match()
    real = cl.PACE_DB
    with tempfile.TemporaryDirectory() as td:
        cl.PACE_DB = os.path.join(td, "pace.sqlite")
        try:
            slept = []
            for _ in range(cl.MINUTE_CAP):
                assert clpace.pace(max_wait=5, sleep=slept.append) == 0.0
            assert slept == [] and _rows(cl.PACE_DB) == cl.MINUTE_CAP, "the first 9 go at once, each reserved"
            try:
                clpace.pace(max_wait=5, sleep=slept.append)
                raise AssertionError("the 10th call in a minute must not go unpaced")
            except clpace.PaceWait as e:
                assert "shared" in str(e)
            assert _rows(cl.PACE_DB) == cl.MINUTE_CAP, "a refused wait deletes its own reservation"
            waited = clpace.pace(max_wait=120, sleep=slept.append)
            assert 50 < waited <= 60 and slept == [waited], "within the limit it sleeps until the slot opens"
        finally:
            cl.PACE_DB = real


def test_sources_cl_get_paces_every_attempt():
    """Since 2026-10-08 sources.cl_get goes through cl_match.cl_get: every attempt, the 429's retry too, reserves a
    slot in the shared window, and the 429's Retry-After is recorded for every process (cl_block). The pace DB is a
    temporary file: a fake 429 must never block or spend the real shared window."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "triage"))
    import sources
    cl = clpace._cl_match()

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"results": []}'
    calls, slept = [], []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise sources.urllib.error.HTTPError(req.full_url, 429, "throttled", {"Retry-After": "2"}, None)
        return R()
    real = (cl.PACE_DB, sources.urllib.request.urlopen, sources.time.sleep, os.environ.get("COURTLISTENER_TOKEN"))
    with tempfile.TemporaryDirectory() as td:
        cl.PACE_DB = os.path.join(td, "pace.sqlite")
        os.environ["COURTLISTENER_TOKEN"] = "test-token"
        sources.urllib.request.urlopen = urlopen
        sources.time.sleep = slept.append
        try:
            assert sources.cl_get("search/", q="x") == {"results": []}
            assert len(calls) == 2 and _rows(cl.PACE_DB) == 2, "the 429's retry is paced too"
            con = sqlite3.connect(cl.PACE_DB)
            blocks = con.execute("SELECT count(*) FROM cl_block").fetchone()[0]
            con.close()
            assert blocks == 1 and slept and 0 < slept[-1] <= 2, "the Retry-After is recorded and waited out"
        finally:
            cl.PACE_DB, sources.urllib.request.urlopen, sources.time.sleep = real[:3]
            if real[3] is None:
                os.environ.pop("COURTLISTENER_TOKEN", None)
            else:
                os.environ["COURTLISTENER_TOKEN"] = real[3]


if __name__ == "__main__":
    test_pace_reserves_shared_slots_and_refuses_a_long_wait()
    test_sources_cl_get_paces_every_attempt()
    print("all checks passed")
