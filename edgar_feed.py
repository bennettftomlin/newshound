"""SEC EDGAR's "latest filings" feed as a gated newshound source (approved by Bennett 2026-10-12).

newshound.run() calls step() once per launchd run (every 10 minutes), so a relevant filing pings within about ten
minutes of acceptance. Full-text search stays in the morning gather (tools/leads/gather.py src_edgar_fts); this
reads only the feed's title line, which is all the feed carries.

THE FEED (browse-edgar?action=getcurrent&...&output=atom, measured 2026-10-12): 100 entries a page, newest first.
An entry's title is "<FORM> - <Name> (<10-digit CIK>) (Issuer|Reporting|Filer|Subject|...)"; one filing with two
parties (a Form 4: issuer and reporting owner; a 13D: subject and filer) is two adjacent entries under ONE
accession. The summary holds the filed date, AccNo and size; the link is the filing index. No filing text.

READING THE WHOLE WINDOW. Each run pages start=0,100,200... until a page holds the high-water mark: the newest
accession of the last complete read (edgar_state in newshound's own DB), or an entry accepted before it (the mark
itself may leave the feed). Filings arriving while it pages push entries DOWN, so a later page can repeat an entry
(grouped away by accession) but never skip one. MAX_PAGES caps a run; reaching it without the mark is a MISSED
WINDOW, printed as "EDGAR CAP HIT" and counted in runs.jsonl, never silent. A run that fails part-way posts what it
read and keeps the old mark, so the next run reads down to it again (posted filings are recorded, so none repeats).
The first run (no mark) reads one page, records its matching filings without posting -- newshound's quiet first
pass -- and sets the mark.

COURTESY. Every request goes through the shared transport (tools/kb-graph/transport.py) with the declared research
User-Agent ("newshound (Protos research; <contact>)", from research_ua; this module never reads the contact) and
reserves its slot in edgar_io's cross-process SEC pace table (derived/edgar/pace.sqlite), the one lane every SEC
client on this machine shares (SEC's 10/s is per IP); newshound asks for a 1 s gap. No contact configured: nothing
is sent (a config error). A 403 is not retried: SEC answers 403 to an undeclared User-Agent (a CONFIG ERROR) and to
a client over its rate threshold ("Request Rate Threshold Exceeded": blocked RATE_BLOCK s). A 429 blocks for its
Retry-After. A block is kept in edgar_state, so later runs send nothing until it ends, and is written to the shared
pace table, so every other SEC client waits too. EDGAR is closed weekends and federal holidays: an unchanged feed
(one request, every entry already behind the mark) is normal.

GATES (an entry passes if ANY matches; the post names each):
  watched CIK   any party's CIK (Issuer, Reporting, Filer, Subject) is one the knowledge base types -- derived/
                graph.sqlite listings, kind 'cik' or 'filer', source 'frontmatter', status 'ok', read through
                graphdb.ro() once per run -- or is on edgar_watch.md (holders and people). A failed graph read falls
                back to the last list read (edgar_watch table), said once per run.
  name keyword  edgar_keywords.md, word-bounded, case-insensitive, on every party's name. A "| forms:" suffix
                limits a keyword to those forms ("mining" on 8-K, S-1, S-4, 424B*, 13D/G); "treasury + w1 w2 ..."
                needs "treasury" and one of the words.
RANK: a PRIORITY form (8-K, 6-K, 13D/G, registration statements and 424B*, merger proxies, delisting and
deregistration, late-filing notices; Forms 3/4/5/144 only with a watched party) is one Slack post each; every other
passing filing (10-Q, 10-K, 13F, Form D, ...) goes into one "routine" batch post per run. newshound had no ranking
before; one ping per item is its convention, so the batch is the quiet tier.

DEDUPE: one item per accession. Posted filings are recorded as newshound records any feed's item: processed_items
(feed SOURCE, accession) and sent_keys "sec:<accession>". newshound.canonical_key gives an EDGAR Archives link
from any other feed the same key, so a filing another source already raised is not raised again.
"""
import os
import pathlib
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
KB = HERE.parent
KB_GRAPH = KB / "tools" / "kb-graph"

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SOURCE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"   # this source's feed_source in the state DB
PAGE = 100                       # the feed's largest page (count=100)
MAX_PAGES = 10                   # a run reads at most this many pages; more is a missed window, said loudly
GAP = 1.0                        # seconds between this process's SEC requests (SEC allows 10/s per IP)
MAX_WAIT = 30.0                  # the most a run waits for its slot in the shared SEC lane
TIMEOUT = (5, 30)                # (connect, read) seconds
AGENT = "newshound"              # the User-Agent's tool name: "newshound (Protos research; <contact>)"
RATE_BLOCK = 600                 # SEC's "Request Rate Threshold Exceeded" lasts 10 minutes
DEFAULT_429 = 600                # a 429 with no usable Retry-After
RETRY_CAP = 3600                 # the longest Retry-After honoured
CONFIG_BLOCK = 3600              # after a 403 for an undeclared UA, ask again in an hour (and say so every run)
BATCH = 40                       # routine filings per batch message
GRAPH_DB = None                  # None: graphdb's default (derived/graph.sqlite); tests repoint it
PACE_DB = None                   # None: edgar_io.PACE_DB; tests repoint it
WATCH_FILE = HERE / "edgar_watch.md"
KEYWORD_FILE = HERE / "edgar_keywords.md"

_ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE = re.compile(r"^(?P<form>.+?) - (?P<name>.*) \((?P<cik>\d{10})\) \((?P<role>[^()]+)\)\s*$")
_ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
_FILED = re.compile(r"Filed:\s*</b>\s*(\d{4}-\d{2}-\d{2})|Filed:\s*(\d{4}-\d{2}-\d{2})")

# Ranking. Base forms (an amendment's "/A" stripped); a prefix ends in "*".
PRIORITY_FORMS = {
    "8-K", "6-K",
    "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G",
    "S-1", "S-1MEF", "S-3", "S-3ASR", "S-3MEF", "S-4", "S-4MEF", "F-1", "F-1MEF", "F-3", "F-3ASR", "F-3MEF", "F-4",
    "F-4MEF",
    "DEFM14A", "DEF 14A", "PREM14A",
    "25-NSE", "15-12G", "15-12B",
    "NT 10-K", "NT 10-Q",
    # tender offers and going-private deals (Bennett's beat: SPACs, reverse mergers, strange acquisitions)
    "SC TO-T", "SC TO-I", "SC 14D9", "SC 13E3",          # offer filings, never their pre-offer communications
}
PRIORITY_PREFIXES = ("424B",)
INSIDER_FORMS = {"3", "4", "5", "144"}          # priority only with a watched party


class EdgarFeedError(RuntimeError):
    """One failed read. kind: config | throttled | blocked | http | network | timeout | parse. block: seconds later
    runs must send nothing (throttled, config), or None."""

    def __init__(self, kind, message, block=None):
        super().__init__(message)
        self.kind, self.block = kind, block


# ------------------------------------------------------------------------------------------------ the shared lane
def _edgar_io():
    d = str(KB / "tools" / "edgar")
    if d not in sys.path:
        sys.path.append(d)
    import edgar_io
    return edgar_io


def _transport():
    if str(KB_GRAPH) not in sys.path:
        sys.path.append(str(KB_GRAPH))
    import transport
    return transport


class LaneBusy(RuntimeError):
    """The shared SEC lane's next slot is further off than MAX_WAIT (another process's 429 or 403 block)."""


class SecLane:
    """A slot in edgar_io's cross-process SEC pace table (one `last_call` row every SEC client on this machine
    reserves against: edgar_io.Sec, iapd_io, listing_refresh). The same critical section as edgar_io._reserve_slot --
    BEGIN IMMEDIATE, read last_call, write max(now, last_call + gap), commit, THEN sleep -- with transport.send's
    pacer interface: wait(max_wait) refuses a slot too far off before writing it, and block_until(ts) moves the
    shared row forward so every SEC client sits out a 429 or a rate-threshold 403."""

    name = "sec.gov (edgar_io pace table)"

    def __init__(self, gap=GAP, db=None, sleep=None, clock=None):
        self.gap, self._db, self._sleep, self._clock = gap, db, sleep, clock

    def db(self):
        return str(self._db or PACE_DB or _edgar_io().PACE_DB)

    def _con(self):
        path = self.db()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=30, isolation_level=None)
        con.execute("CREATE TABLE IF NOT EXISTS pace (id INTEGER PRIMARY KEY CHECK (id=0), last_call REAL NOT NULL)")
        con.execute("INSERT OR IGNORE INTO pace (id, last_call) VALUES (0, 0)")
        return con

    def now(self):
        return (self._clock or time.time)()

    def wait(self, max_wait=None):
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE")
            last = con.execute("SELECT last_call FROM pace WHERE id=0").fetchone()[0]
            now = self.now()
            next_ok = max(now, last + self.gap)
            if max_wait is not None and next_ok - now > max_wait:
                con.execute("ROLLBACK")
                raise LaneBusy(f"the shared SEC lane's next slot is {next_ok - now:.0f}s away")
            con.execute("UPDATE pace SET last_call=? WHERE id=0", (next_ok,))
            con.execute("COMMIT")
        finally:
            con.close()
        wait = next_ok - now
        if wait > 0:
            (self._sleep or time.sleep)(wait)
        return wait

    def block_until(self, until, why=""):
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE pace SET last_call = MAX(last_call, ?) WHERE id=0", (float(until),))
            con.execute("COMMIT")
        finally:
            con.close()

    def peek(self):
        con = self._con()
        try:
            return con.execute("SELECT last_call FROM pace WHERE id=0").fetchone()[0]
        finally:
            con.close()


# ------------------------------------------------------------------------------------------------ one page
def ua_configured():
    """Whether the declared research contact is set (research_ua; the value is never seen here)."""
    return _transport()._research_ua().configured()


def page_params(start):
    return {"action": "getcurrent", "CIK": "", "type": "", "company": "", "dateb": "", "owner": "include",
            "start": str(start), "count": str(PAGE), "output": "atom"}


def fetch_page(start, lane):
    """ONE request for the page at `start` -> the Atom body (bytes). EdgarFeedError on anything but a 200."""
    if not ua_configured():
        raise EdgarFeedError("config", "no declared research contact: set EDGAR_UA or ~/.config/edgar/ua (read "
                             "only through research_ua); SEC refuses an undeclared User-Agent, so nothing was sent",
                             block=None)
    transport = _transport()
    try:
        r = transport.send("GET", FEED_URL, params=page_params(start), timeout=TIMEOUT, pacer=lane,
                           max_wait=MAX_WAIT, agent=AGENT, what=f"newshound edgar latest filings start={start}",
                           retry_cap=RETRY_CAP)
    except LaneBusy as e:
        raise EdgarFeedError("blocked", str(e))
    except transport.TransportError as e:
        if e.status == 403:
            if "rate threshold" in (e.body or "").lower():
                lane.block_until(lane.now() + RATE_BLOCK, "SEC rate threshold 403")
                raise EdgarFeedError("throttled", f"SEC 403 Request Rate Threshold Exceeded at start={start}; "
                                     f"sending nothing for {RATE_BLOCK}s", block=RATE_BLOCK)
            raise EdgarFeedError("config", f"SEC answered 403 at start={start}: the User-Agent is undeclared or "
                                 "refused (check EDGAR_UA / ~/.config/edgar/ua); not retried", block=CONFIG_BLOCK)
        if e.status == 429:
            secs = e.retry_after if e.retry_after is not None else DEFAULT_429
            if e.retry_after is None:                  # transport shares only a Retry-After it could read
                lane.block_until(lane.now() + secs, "SEC 429")
            raise EdgarFeedError("throttled", f"SEC 429 at start={start}; Retry-After {secs:g}s, not retried",
                                 block=secs)
        kind = e.kind if e.kind in ("network", "timeout") else "http"
        raise EdgarFeedError(kind, f"{e.kind} at start={start}" + (f": HTTP {e.status}" if e.status else ""))
    return r.body


def parse_page(body):
    """Atom body -> [entry dicts] in feed order: acc, form, name, cik (int, as str), role, updated (ISO, as served),
    filed, link. An entry whose title or accession cannot be read is skipped and counted in the returned
    `unparsed`. A body that is not an Atom feed is EdgarFeedError('parse')."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise EdgarFeedError("parse", f"the latest-filings page is not XML ({e})")
    if root.tag != _ATOM + "feed":
        raise EdgarFeedError("parse", f"the latest-filings page is <{root.tag}>, not an Atom feed")
    out, unparsed = [], 0
    for el in root.findall(_ATOM + "entry"):
        title = (el.findtext(_ATOM + "title") or "").strip()
        summary = el.findtext(_ATOM + "summary") or ""
        ident = el.findtext(_ATOM + "id") or ""
        link_el = el.find(_ATOM + "link")
        link = link_el.get("href") if link_el is not None else ""
        m = _TITLE.match(re.sub(r"\s+", " ", title))
        acc = _ACC.search(ident) or _ACC.search(summary) or _ACC.search(link or "")
        if not m or not acc:
            unparsed += 1
            continue
        fm = _FILED.search(summary)
        out.append({"acc": acc.group(1), "form": m.group("form").strip(), "name": m.group("name").strip(),
                    "cik": str(int(m.group("cik"))), "role": m.group("role").strip(),
                    "updated": (el.findtext(_ATOM + "updated") or "").strip(),
                    "filed": (fm.group(1) or fm.group(2)) if fm else "", "link": link})
    return out, unparsed


# ------------------------------------------------------------------------------------------------ gates
def _norm_cik(v):
    s = str(v or "").strip()
    return str(int(s)) if s.isdigit() else None


def graph_ciks(path=None):
    """{cik: label} for every CIK the knowledge base types (listings kind cik/filer, from frontmatter, status ok),
    labelled by the note's name. graphdb.ro(): read-only, waiting out the nightly delta's commits."""
    if str(KB_GRAPH) not in sys.path:
        sys.path.append(str(KB_GRAPH))
    import graphdb
    con = graphdb.ro(path or GRAPH_DB)
    try:
        rows = con.execute("SELECT value, path FROM listings WHERE kind IN ('cik', 'filer') "
                           "AND source = 'frontmatter' AND status = 'ok'").fetchall()
    finally:
        con.close()
    out = {}
    for value, note in rows:
        cik = _norm_cik(value)
        if cik:
            out.setdefault(cik, os.path.splitext(os.path.basename(note or ""))[0] or "KB")
    return out


def extra_ciks(path=None):
    """{cik: label} from edgar_watch.md: "<CIK> <label>" per line, '#' starts a comment."""
    out = {}
    try:
        with open(path or WATCH_FILE, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                head, _, label = line.partition(" ")
                cik = _norm_cik(head)
                if cik:
                    out[cik] = label.strip() or f"CIK {cik}"
    except FileNotFoundError:
        pass
    return out


def load_watch(conn, graph_path=None, watch_path=None, say=print):
    """{cik: (label, origin)} for this run, origin 'kb' or 'list'. The graph's part is saved in the state DB on each
    good read and used from there when the graph cannot be read (said, never silent)."""
    conn.execute("CREATE TABLE IF NOT EXISTS edgar_watch (cik TEXT PRIMARY KEY, label TEXT)")
    try:
        kb = graph_ciks(graph_path)
        if not kb:
            raise RuntimeError("no typed CIK in the graph")
        conn.execute("DELETE FROM edgar_watch")
        conn.executemany("INSERT INTO edgar_watch (cik, label) VALUES (?, ?)", sorted(kb.items()))
        conn.commit()
    except Exception as exc:                                                   # noqa: BLE001
        kb = dict(conn.execute("SELECT cik, label FROM edgar_watch").fetchall())
        say(f"EDGAR: the graph's CIKs could not be read ({type(exc).__name__}: {str(exc)[:120]}); using the last "
            f"list read ({len(kb)} CIKs)")
    watch = {c: (lab, "KB") for c, lab in kb.items()}
    for c, lab in extra_ciks(watch_path).items():
        watch.setdefault(c, (lab, "list"))
    return watch


def _form_spec(tokens):
    return tuple(t.strip().upper() for t in tokens if t.strip())


def _form_in(form, spec):
    f = form.upper()
    base = f[:-2] if f.endswith("/A") else f
    for s in spec:
        if s.endswith("*"):
            if f.startswith(s[:-1]):
                return True
        elif base == s or f == s:
            return True
    return False


def _word(term):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])", re.I)


def load_keywords(path=None):
    """[rule] from edgar_keywords.md. A line is a term ("bitcoin"), a term limited to forms ("mining | forms: 8-K
    S-1 424B*"), or a pair ("treasury + eth sol token": "treasury" and any one of the words). Forms are
    space-separated base forms; "*" ends a prefix; an amendment (/A) counts as its form."""
    rules = []
    with open(path or KEYWORD_FILE, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            forms = None
            if "|" in line:
                line, _, rest = line.partition("|")
                rest = rest.strip()
                if rest.lower().startswith("forms:"):
                    rest = rest[6:]
                # "SC 13D*" is one form: split on commas when given, else keep two-word forms together
                parts = [p for p in re.split(r",", rest)] if "," in rest else re.findall(
                    r"(?:SC|SCHEDULE|DEF|NT)\s+\S+|\S+", rest, re.I)
                forms = _form_spec(parts)
                line = line.strip()
            if "+" in line:
                head, _, words = line.partition("+")
                alts = [w for w in words.split() if w]
                rules.append({"label": head.strip(), "all": _word(head.strip()),
                              "any": [(w, _word(w)) for w in alts], "forms": forms})
            else:
                rules.append({"label": line, "all": _word(line), "any": None, "forms": forms})
    return rules


def keyword_hits(names, form, rules):
    """The rules any of `names` matches (a pair: both parts in ONE name), as labels for the post."""
    hits = []
    for r in rules:
        if r["forms"] is not None and not _form_in(form, r["forms"]):
            continue
        for n in names:
            if not r["all"].search(n):
                continue
            if r["any"] is None:
                hits.append(f"name keyword '{r['label']}'")
                break
            w = next((w for w, rx in r["any"] if rx.search(n)), None)
            if w:
                hits.append(f"name keyword '{r['label']} + {w}'")
                break
    return hits


def rank(form, watched):
    """'priority' or 'routine' (see the module docstring)."""
    if _form_in(form, PRIORITY_FORMS) or any(form.upper().startswith(p) for p in PRIORITY_PREFIXES):
        return "priority"
    if watched and _form_in(form, INSIDER_FORMS):
        return "priority"
    return "routine"


def gate(filing, watch, rules):
    """(gates, rank) for one filing: gates is [] when nothing matches."""
    gates = []
    for p in filing["parties"]:
        hit = watch.get(p["cik"])
        if hit:
            label, origin = hit
            g = f"watched CIK {p['cik']} ({origin}: {label}) as {p['role']}"
            if g not in gates:
                gates.append(g)
    watched = bool(gates)
    gates += keyword_hits([p["name"] for p in filing["parties"]], filing["form"], rules)
    return gates, (rank(filing["form"], watched) if gates else None)


# ------------------------------------------------------------------------------------------------ state
def _state(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS edgar_state (key TEXT PRIMARY KEY, value TEXT)")
    return dict(conn.execute("SELECT key, value FROM edgar_state").fetchall())


def _set_state(conn, **kv):
    conn.executemany("INSERT OR REPLACE INTO edgar_state (key, value) VALUES (?, ?)",
                     [(k, None if v is None else str(v)) for k, v in kv.items()])
    conn.commit()


def sent_key(acc):
    return f"sec:{acc}"


def _done(conn, acc, sent_keys):
    if sent_key(acc) in sent_keys:
        return True
    return conn.execute("SELECT 1 FROM processed_items WHERE feed_source = ? AND item_id = ?",
                        (SOURCE, acc)).fetchone() is not None


# ------------------------------------------------------------------------------------------------ posts
def _when(f):
    u = f.get("updated") or ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", u)
    if not m:
        return f.get("filed") or "?"
    tz = " ET" if re.search(r"-0[45]:00$", u) else (" " + u[19:] if len(u) > 19 else "")
    return f"{m.group(1)} {m.group(2)}{tz}"


def _parties(f):
    return "; ".join(f"{p['name']} (CIK {p['cik']}, {p['role']})" for p in f["parties"])


def priority_message(f, gates):
    return (f"\U0001F6A8 EDGAR {f['form']}: {_parties(f)}\nAccepted {_when(f)} · {f['acc']}\n"
            f"Gate: {'; '.join(gates)}\nLink: {f['link']}")


def routine_message(items):
    lines = [f"EDGAR routine filings ({len(items)}):"]
    for f, gates in items:
        lines.append(f"• {f['form']}: {_parties(f)} · {_when(f)} · {'; '.join(gates)} · {f['link']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the run
def read_window(conn, lane, max_pages=None, say=print):
    """Page the feed down to the mark. -> dict: filings (accession -> filing, feed order), pages, entries, unparsed,
    reached (the mark, or an older entry, was reached), seeded (no mark: one page read), top (newest entry),
    error (EdgarFeedError or None: what was read before it is kept)."""
    max_pages = MAX_PAGES if max_pages is None else max_pages
    st = _state(conn)
    mark, mark_at = st.get("mark_acc"), st.get("mark_updated")
    out = {"filings": {}, "pages": 0, "entries": 0, "unparsed": 0, "reached": False, "seeded": mark is None,
           "top": None, "error": None, "mark": mark, "oldest": None}
    for page in range(max_pages if mark else 1):
        try:
            body = fetch_page(page * PAGE, lane)
            entries, unparsed = parse_page(body)
        except EdgarFeedError as e:
            out["error"] = e
            return out
        out["pages"] += 1
        out["unparsed"] += unparsed
        out["entries"] += len(entries)
        if not entries:
            if unparsed:
                out["error"] = EdgarFeedError("parse", f"no entry of {unparsed} on the page at start={page * PAGE} "
                                              "could be read: has the feed's title format changed?")
            break                                 # the end of the feed: reported by the caller as a missed window
        if out["top"] is None:
            out["top"] = entries[0]
        for e in entries:
            if mark and (e["acc"] == mark or (mark_at and e["updated"] and _older(e["updated"], mark_at))):
                out["reached"] = True
                break
            f = out["filings"].get(e["acc"])
            if f is None:
                f = out["filings"][e["acc"]] = {"acc": e["acc"], "form": e["form"], "updated": e["updated"],
                                                "filed": e["filed"], "link": e["link"], "parties": []}
            if not any(p["cik"] == e["cik"] and p["role"] == e["role"] for p in f["parties"]):
                f["parties"].append({"cik": e["cik"], "name": e["name"], "role": e["role"]})
            out["oldest"] = e
        if out["reached"] or out["seeded"]:
            break
    return out


def _older(a, b):
    """Whether ISO time a is strictly before b (both as SEC serves them, with an offset)."""
    import datetime
    try:
        return datetime.datetime.fromisoformat(a) < datetime.datetime.fromisoformat(b)
    except ValueError:
        return False


def step(conn, sent_keys, post, pause=None, lane=None, now=None, say=print, graph_path=None, watch_path=None,
         keyword_path=None, max_pages=None):
    """One run's EDGAR read: page, gate, post, record. -> (summary dict for runs.jsonl, failure note or None).
    post(text) sends one Slack message; pause() runs after each (newshound's 10 s between posts)."""
    now = time.time() if now is None else now
    lane = lane or SecLane()
    summ = {"pages": 0, "entries": 0, "filings": 0, "matched": 0, "posted": 0, "routine": 0, "cap_hit": False,
            "seeded": False, "watch": 0, "status": "ok"}
    st = _state(conn)
    blocked = float(st.get("blocked_until") or 0)
    if blocked > now:
        why = st.get("block_why") or "blocked"
        summ["status"] = "blocked"
        say(f"EDGAR: sending nothing until {time.strftime('%H:%M:%S', time.localtime(blocked))} ({why})")
        if why.startswith("config"):
            summ["status"] = "config"
            return summ, f"EDGAR config error ({why})"
        return summ, None
    watch = load_watch(conn, graph_path, watch_path, say)
    rules = load_keywords(keyword_path)
    summ["watch"] = len(watch)

    w = read_window(conn, lane, max_pages, say)
    summ.update(pages=w["pages"], entries=w["entries"], filings=len(w["filings"]))
    err = w["error"]
    if w["unparsed"]:
        say(f"EDGAR: {w['unparsed']} entr{'y' if w['unparsed'] == 1 else 'ies'} with an unreadable title skipped")

    # gate every filing above the mark, oldest first
    passing = []
    for f in reversed(list(w["filings"].values())):
        if _done(conn, f["acc"], sent_keys):
            continue
        gates, rk = gate(f, watch, rules)
        if gates:
            passing.append((f, gates, rk))
    summ["matched"] = len(passing)

    if w["seeded"] and w["top"] is not None:
        for f, gates, rk in passing:
            conn.execute("INSERT OR IGNORE INTO processed_items (feed_source, item_id) VALUES (?, ?)",
                         (SOURCE, f["acc"]))
        conn.commit()
        _set_state(conn, mark_acc=w["top"]["acc"], mark_updated=w["top"]["updated"])
        summ.update(seeded=True, status="seeded")
        say(f"EDGAR NEW SOURCE: set the mark at {w['top']['acc']} ({w['top']['updated']}); recorded "
            f"{len(passing)} matching filing(s) of {len(w['filings'])} without alerting (watching {len(watch)} CIKs)")
        for f, gates, rk in passing:
            say(f"    - {f['form']} {_parties(f)[:100]} [{'; '.join(gates)[:80]}]")
        return summ, None

    def record(f):
        conn.execute("INSERT OR IGNORE INTO processed_items (feed_source, item_id) VALUES (?, ?)", (SOURCE, f["acc"]))
        conn.execute("INSERT OR IGNORE INTO sent_keys (key, feed_source) VALUES (?, ?)", (sent_key(f["acc"]), SOURCE))
        conn.commit()
        sent_keys.add(sent_key(f["acc"]))

    routine = []
    for f, gates, rk in passing:
        if rk == "priority":
            post(priority_message(f, gates))
            record(f)
            summ["posted"] += 1
            if pause:
                pause()
        else:
            routine.append((f, gates))
    for i in range(0, len(routine), BATCH):
        chunk = routine[i:i + BATCH]
        post(routine_message(chunk))
        for f, _ in chunk:
            record(f)
        summ["routine"] += len(chunk)
        if pause:
            pause()

    failure = None
    if err is not None:
        summ["status"] = err.kind
        if err.block:
            why = f"{err.kind}: {err}"
            _set_state(conn, blocked_until=now + err.block, block_why=why)
        loud = "EDGAR CONFIG ERROR" if err.kind == "config" else "EDGAR read failed"
        say(f"{loud} ({err.kind}): {err}; the mark stays at {w['mark']}, so the next run reads down to it again")
        if err.kind != "throttled" and err.kind != "blocked":
            failure = f"EDGAR {err.kind}: {str(err)[:160]}"
    elif w["top"] is not None:
        if not w["reached"]:
            summ["cap_hit"] = True
            summ["status"] = "cap"
            oldest = (w["oldest"] or {}).get("updated", "?")
            ended = w["pages"] < (MAX_PAGES if max_pages is None else max_pages)
            say(f"EDGAR CAP HIT: read {w['pages']} page(s) ({w['entries']} entries) "
                f"{'to the end of the feed' if ended else 'at the cap'} without reaching the mark {w['mark']} "
                f"({_state(conn).get('mark_updated')}); filings accepted between then and {oldest} were NOT read "
                "(a missed window)")
        _set_state(conn, mark_acc=w["top"]["acc"], mark_updated=w["top"]["updated"])
        if st.get("blocked_until"):
            _set_state(conn, blocked_until=None, block_why=None)
    say(f"{time.strftime('%Y-%m-%d %H:%M')} EDGAR: {summ['pages']} page(s), {summ['filings']} new filing(s), "
        f"{summ['matched']} matched ({summ['posted']} posted, {summ['routine']} routine), watching {summ['watch']} "
        "CIKs" + (" -- CAP HIT" if summ["cap_hit"] else ""))
    return summ, failure
