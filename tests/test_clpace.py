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
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "triage"))
    import sources
    paced = []
    real_pace, real_open = clpace.pace, sources.urllib.request.urlopen
    clpace.pace = lambda *a, **k: paced.append(1)

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"results": []}'
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise sources.urllib.error.HTTPError(req.full_url, 429, "throttled", {}, None)
        return R()
    sources.urllib.request.urlopen = urlopen
    real_sleep, sources.time.sleep = sources.time.sleep, lambda s: None
    try:
        assert sources.cl_get("search/", q="x") == {"results": []}
        assert len(calls) == 2 and len(paced) == 2, "the 429's retry is paced too"
    finally:
        clpace.pace, sources.urllib.request.urlopen, sources.time.sleep = real_pace, real_open, real_sleep


if __name__ == "__main__":
    test_pace_reserves_shared_slots_and_refuses_a_long_wait()
    test_sources_cl_get_paces_every_attempt()
    print("all checks passed")
