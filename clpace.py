"""Pace newshound's CourtListener API calls through claude_knowledge's cl-pace window.

One budget for every process that calls the CourtListener API: kbpy's courts.* verbs, the morning
gather (which imports triage/sources.py) and newshound itself all reserve a slot in
derived/courtlistener/pace.sqlite (cl_match._cl_reserve: 9 a minute and 70 an hour, under the account's
10 and 75) before they send. Added 2026-10-08 (Bennett: "Newshound should pace too"), after a day when
unpaced calls drew 429s across sessions and spent the hour for everyone. CourtListener's RSS feeds are
web pages, not the API, and are not paced here.

A wait longer than MAX_WAIT seconds raises PaceWait instead of sleeping, so a launchd run never stalls
for most of an hour; the caller reports the call as failed (newshound falls back to a caption search, the
gather names it in its status line). The reservation a refused wait made is deleted again.

Since 2026-10-08 triage/sources.py (the triage and the morning gather) no longer calls pace(): its cl_get goes
through cl_match.cl_get itself, with cl_match.PACE_MAX_WAIT lowered to MAX_WAIT for the call, so CourtListener's
Retry-After is recorded and honoured too. Both PaceWait and cl_match.PaceExhausted reach those callers as CLBudget.
"""
import pathlib
import sys
import time

KB = pathlib.Path(__file__).resolve().parent.parent / "tools" / "kb-graph"
MAX_WAIT = 120

_cl = None


class CLBudget(RuntimeError):
    """The shared CourtListener budget is spent for now: no slot within the caller's wait (PaceWait here,
    cl_match.PaceExhausted re-raised by triage/sources.cl_get), a 429 whose Retry-After is too long, or a throttle
    body. One class every caller can catch -- the gather's document loop and the triage loops stop on it, since
    every later call would only wait and fail the same way. Defined here, not in sources.py, because sources is
    imported under two names (`sources` by the gather, `triage.sources` by triage/run.py) and a class defined
    there would be two classes."""


class PaceWait(CLBudget):
    """The shared CourtListener budget has no slot within MAX_WAIT seconds."""


def _cl_match():
    global _cl
    if _cl is None:
        if str(KB) not in sys.path:
            sys.path.append(str(KB))
        import cl_match
        _cl = cl_match
    return _cl


def pace(max_wait=None, sleep=time.sleep):
    """Reserve the next CourtListener API slot and wait for it; PaceWait when it is too far off."""
    cl = _cl_match()
    wait, rowid = cl._cl_reserve()
    limit = MAX_WAIT if max_wait is None else max_wait
    if wait > limit:
        cl._cl_unreserve(rowid)
        raise PaceWait(f"cl-pace: the next CourtListener API slot is {wait:.0f}s away (the shared 9-a-minute, "
                       f"70-an-hour budget); not waiting more than {limit}s")
    if wait > 0:
        sleep(wait)
    return wait
