# simple script to check RSS feeds for updates, filter to specific terms, and send slack message when filter is met
import feedparser
from slack_sdk import WebClient
import os
from io import StringIO
import sqlite3
import time
import hashlib
import re
import socket
import json
import pathlib
import calendar
import collections
import datetime
import html
import urllib.parse
import sys

# Cap any single feed fetch: feedparser has no timeout of its own, so one
# unresponsive server would otherwise stall the whole run.
socket.setdefaulttimeout(30)

# Connecting gets its own, shorter limit PER ADDRESS. The 30 s default applied to the connect as well, so a host
# with one dead address among several cost the full 30 s before Python tried the next: ssf.gob.sv has four
# addresses, one of which (54.147.109.227, 2026-10-08) accepts no connection -- 170+ "SLOW 30s" lines in the log,
# every one a read that then succeeded on the next address. A live server answers a connect in well under a
# second; a slow feed (CourtListener's searches take 11-62 s to build) is slow to ANSWER, and keeps the full read
# timeout once connected.
CONNECT_TIMEOUT = 5
_create_connection = socket.create_connection


def _connect_fast(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    """socket.create_connection with CONNECT_TIMEOUT per address, then the caller's (or the default) timeout for
    everything after the connect. Same addresses, same order, same errors as the original."""
    after = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    per = CONNECT_TIMEOUT if after is None else min(CONNECT_TIMEOUT, after)
    host, port = address[0], address[1]
    err = None
    for af, socktype, proto, _canon, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(per)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            sock.settimeout(after)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError(f"getaddrinfo returned no address for {host}")


socket.create_connection = _connect_fast

# The CourtListener API call below goes through the shared transport (tools/kb-graph/transport.py: one attempt, the
# token redacted from every error, a 4xx not retried, Retry-After parsed in both forms and capped). Found from this
# file's own location, like clpace.KB, and imported on first use, not at import: a broken transport must cost a bare
# link its docket (a caption search is the fallback), not the run its 55 feeds.
# The FEED reads stay on feedparser (permanent): it owns the request (conditional ETag / If-Modified-Since,
# redirects, gzip, charset sniffing, the status and validators parse_feed's callers read), and it opens its
# connections through socket.create_connection, which _connect_fast replaces for the 5 s per-address connect.
TRANSPORT_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools" / "kb-graph"


def _transport():
    if str(TRANSPORT_DIR) not in sys.path:
        sys.path.append(str(TRANSPORT_DIR))
    import transport
    return transport


def _note_courtlistener_throttle(exc):
    """A 429 from the API is CourtListener's own Retry-After: record it so every process (the morning gather, kbpy's
    courts verbs) waits it out, as cl_match.cl_get does. Never raises: the caller is already on its fallback."""
    try:
        import clpace
        clpace._cl_match().note_throttle(getattr(exc, "retry_after", None), why="429 on newshound docket-link search")
    except Exception:
        pass


DB_PATH = os.environ.get("NEWSHOUND_DB", "last_processed.db")
BARE_COURTLISTENER = ("https://www.courtlistener.com", "https://www.courtlistener.com/")

def initialize_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Use a proper relational table to track every unique item per feed source.
    # The primary key ensures no duplicate entries for the same content on the same feed.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_items (
            feed_source TEXT,
            item_id TEXT,
            PRIMARY KEY (feed_source, item_id)
        )
    """)
    # One row per DOCUMENT ever sent (or recorded) from ANY feed. processed_items
    # is per feed, so the same filing reached through two feeds -- a search for
    # "yuchen sun" and the Sun v. World Liberty docket itself -- went out twice,
    # and changing a feed's URL made every item in it new again: on 2026-09-23
    # re-ordering twelve CourtListener searches re-sent ~86 old alerts.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_keys (
            key TEXT PRIMARY KEY,
            feed_source TEXT
        )
    """)
    # Feed URLs fetched at least once. "No processed items" cannot stand in for
    # this: a feed whose items never matched a keyword (SEC litigation, the OCC)
    # has none either, and would have its first real match recorded silently.
    cursor.execute("CREATE TABLE IF NOT EXISTS feeds_seen (feed_source TEXT PRIMARY KEY)")
    if cursor.execute("SELECT COUNT(*) FROM feeds_seen").fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO feeds_seen SELECT DISTINCT feed_source FROM processed_items")
    # New cases are remembered by caption, and captions recur -- "USA v. Sealed",
    # "In re Grand Jury Subpoena" -- so a case is remembered for CASE_MEMORY_DAYS,
    # not forever. The feeds only show cases filed in the last 14 days, so an
    # announced case stays quiet for as long as it can appear.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cases_seen (
            key TEXT PRIMARY KEY,
            feed_source TEXT,
            first_seen TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO cases_seen (key, feed_source, first_seen) "
                   "SELECT key, feed_source, datetime('now') FROM sent_keys WHERE key LIKE 'case:%'")
    cursor.execute("DELETE FROM sent_keys WHERE key LIKE 'case:%'")
    # When each CourtListener feed is next read (see CL_SEARCH_INTERVAL). Every other feed is read every run and
    # never has a row. newest_ts is the newest entry the feed has shown (epoch seconds, UTC), NULL until known.
    cursor.execute("CREATE TABLE IF NOT EXISTS feed_schedule "
                   "(feed_source TEXT PRIMARY KEY, next_due REAL, interval REAL, newest_ts REAL)")
    # Every non-CourtListener feed's ETag and Last-Modified from its last full (200) read, sent back as If-None-Match /
    # If-Modified-Since (see load_validators). keywords is the keyword list they were stored under.
    cursor.execute("CREATE TABLE IF NOT EXISTS feed_validators "
                   "(feed_source TEXT PRIMARY KEY, etag TEXT, modified TEXT, keywords TEXT)")
    # Every non-CourtListener feed's run of consecutive failed reads, and when a feed in backoff is next read (see
    # record_read). No row: the last read succeeded.
    cursor.execute("CREATE TABLE IF NOT EXISTS feed_health "
                   "(feed_source TEXT PRIMARY KEY, failures INTEGER, skip_until REAL, last_error TEXT)")
    # A '# filter: key' docket's routine entries, held for the daily roll-up (digest_step), and the last local date a
    # roll-up went out. A held row is deleted only once its roll-up has posted.
    cursor.execute("CREATE TABLE IF NOT EXISTS held_routine "
                   "(key TEXT PRIMARY KEY, feed_source TEXT, title TEXT, link TEXT, summary TEXT, held_ts REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS digest_state (name TEXT PRIMARY KEY, last_date TEXT)")
    conn.commit()
    if cursor.execute("SELECT COUNT(*) FROM sent_keys").fetchone()[0] == 0:
        rows = cursor.execute("SELECT feed_source, item_id FROM processed_items").fetchall()
        cursor.executemany("INSERT OR IGNORE INTO sent_keys (key, feed_source) VALUES (?, ?)",
                           [(canonical_key(item, item), feed) for feed, item in rows
                            if item not in BARE_COURTLISTENER])
        conn.commit()
    return conn

CASE_MEMORY_DAYS = 30

def get_sent_keys(conn):
    keys = {row[0] for row in conn.execute("SELECT key FROM sent_keys")}
    keys |= {row[0] for row in conn.execute(
        "SELECT key FROM cases_seen WHERE first_seen >= datetime('now', ?)", (f"-{CASE_MEMORY_DAYS} days",))}
    return keys

# /Archives/edgar/data/<cik>/<18-digit accession folder>/... or .../<cik>/<0001234567-26-000123>.txt
_SEC_ARCHIVES = re.compile(r"sec\.gov/Archives/edgar/data/\d+/(?:(\d{10})(\d{2})(\d{6})(?:/|$)|(\d{10}-\d{2}-\d{6}))")


def canonical_key(item_id, link):
    """The identity of the DOCUMENT an item points at, whichever feed found it.

    CourtListener spells one docket entry two ways: a search feed links
    /docket/<id>/<entry>/[<attachment>/]<slug>/ and a docket feed links
    /docket/<id>/<slug>/?order_by=desc#entry-<entry>. Both reduce to
    cl:<id>:<entry>[:<attachment>]. A minute entry has no entry number, only
    #minute-entry-<n>, and is cl:<id>:minute:<n> -- keyed on the docket page they
    all link to, every minute entry after a docket's first went unsent. An EDGAR
    filing (a sec.gov/Archives link) is sec:<accession>: the latest-filings feed
    links one filing from each party's CIK folder, and edgar_feed records what it
    posts under that key. Anything else is keyed on its link without query string
    or fragment, or on its feed id when it has no link.
    """
    link = link or ""
    m = re.search(r"courtlistener\.com/docket/(\d+)/(\d+)/(?:(\d+)/)?", link)
    if m:
        return "cl:" + ":".join(g for g in m.groups() if g)
    m = re.search(r"courtlistener\.com/docket/(\d+)/[^#]*#entry-(\d+)", link)
    if m:
        return f"cl:{m.group(1)}:{m.group(2)}"
    m = re.search(r"courtlistener\.com/docket/(\d+)/[^#]*#minute-entry-(\d+)", link)
    if m:
        return f"cl:{m.group(1)}:minute:{m.group(2)}"
    m = _SEC_ARCHIVES.search(link)
    if m:
        return "sec:" + (m.group(4) or f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    if link and link not in BARE_COURTLISTENER:
        return re.split(r"[?#]", link)[0].rstrip("/")
    return f"id:{item_id}"

def get_last_processed_guid_from_db(conn, feed_url):
    #"\"\"Returns a set of all IDs already processed for this specific feed.\"\"\"
    cursor = conn.cursor()
    try:
        # Fetches ids from the relational table
        cursor.execute("SELECT item_id FROM processed_items WHERE feed_source = ?", (feed_url,))
        results = cursor.fetchall()
        return {row[0] for row in results}
    except sqlite3.OperationalError:
        return set()

def update_last_processed_guid_in_db(conn, feed_url, item_id, key=None):
    #"\"\"Marks a single specific item as processed in the database.\"\"\"
    cursor = conn.cursor()
    try:
        # Insert or ignore ensures that if we retry anyway, it doesn't crash on duplicates
        cursor.execute("INSERT OR IGNORE INTO processed_items (feed_source, item_id) VALUES (?, ?)", (feed_url, str(item_id)))
        if key:
            cursor.execute("INSERT OR IGNORE INTO sent_keys (key, feed_source) VALUES (?, ?)", (key, feed_url))
        conn.commit()
    except sqlite3.OperationalError:
        pass


# Built on first use rather than at import: importing the module (tests, dry runs)
# must not need a token, and a missing one should fail the post, not the import.
slack_client = None

def get_slack_client():
    global slack_client
    if slack_client is None:
        slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        try:
            slack_client.auth_test()
        except Exception as e:
            print(f"Warning: Failed to authenticate Slack Client. Ensure SLACK_BOT_TOKEN is set in environment variables. Error: {e}")
    return slack_client

def get_keywords():
    with open("keywords.md", encoding='utf-8-sig') as f:
        lines = [line.strip() for line in f]
    # Blank lines would compile into an alternation branch that matches
    # everywhere, alerting on every item in every feed.
    return [line for line in lines if line]

def get_excluded_dockets():
    """CourtListener docket ids newshound never alerts on (excluded_dockets.md): a docket id or a docket URL per line,
    a reason after '#'. A false match a feed's query cannot keep out -- q=htx finds Clark v. Housing Alliance HTX, a
    Houston housing case, on every filing (Bennett, 2026-11-03). No file means none."""
    try:
        with open("excluded_dockets.md", encoding="utf-8-sig") as f:
            lines = [re.split(r"\s+#|^#", line, maxsplit=1)[0].strip() for line in f]
    except FileNotFoundError:
        return frozenset()
    ids = set()
    for line in filter(None, lines):
        m = re.fullmatch(r"(\d+)", line) or _DOCKET_ID.search(line)
        if m:
            ids.add(m.group(1))
        else:
            print(f"UNREADABLE EXCLUSION {line!r} (want a docket id or URL): ignored")
    return frozenset(ids)

def excluded_docket(link, excluded):
    """The excluded docket id a CourtListener link belongs to, else None."""
    m = _DOCKET_ID.search(link or "")
    return m.group(1) if m and m.group(1) in excluded else None

def build_keyword_pattern(keywords):
    # One alternation so each entry is scanned once. The lookarounds act as
    # word boundaries that also work for keywords with leading/trailing
    # punctuation, keeping "ADA" out of "Canada" and "BTC" out of "LGBTQ".
    alternation = "|".join(re.escape(keyword) for keyword in keywords)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)

def get_entry_text(entry):
    #"\"\"Everything worth matching against: feeds vary in where they put the detail.\"\"\"
    parts = [entry.get('title', ''), entry.get('summary', '')]
    for block in entry.get('content', []) or []:
        parts.append(block.get('value', '') if isinstance(block, dict) else block)
    return " ".join(str(part) for part in parts)

# sec.gov refuses feedparser's default User-Agent with HTTP 403, and a 403 is not
# an error here -- it parses as a feed with zero entries, so both SEC feeds went
# silently dark with nothing in any log. Their automated-access policy wants a UA
# carrying a contact address; measured 2026-09-17, any UA containing an
# email-shaped token is accepted and returns 25 entries. Set FEED_USER_AGENT to
# something like "Bennett Tomlin <you@example.com>". Unset, SEC stays dark but
# every other feed behaves exactly as before.
FEED_AGENT = os.environ.get("FEED_USER_AGENT", "").strip()


def parse_feed(feed_url, etag=None, modified=None):
    """feedparser's read of feed_url, conditional when etag / modified are given: an unchanged feed then answers 304
    with no body, and feedparser returns status 304 and no entries."""
    kwargs = {"agent": FEED_AGENT} if FEED_AGENT else {}
    if etag:
        kwargs["etag"] = etag
    if modified:
        kwargs["modified"] = modified
    return feedparser.parse(feed_url, **kwargs)


def pre_filtered(feed_url):
    """Feeds whose URL already expresses what we want, so no keyword gate."""
    return "courtlistener.com" in feed_url


def item_identifier(entry):
    """newshound's id for an entry. CourtListener gives entries with no document
    of their own (minute entries, "Case Assigned") the bare site URL as their id,
    so every such entry in a feed shared ONE id and all but the first were skipped
    as already processed. Those get an id from their own content instead."""
    ident = entry.get('id') or ''
    if not ident or ident in BARE_COURTLISTENER:
        basis = f"{entry.get('link', '')}{entry.get('title', '')}"
        if ident in BARE_COURTLISTENER:
            basis += f"{entry.get('published', '')}{entry.get('summary', '')}"
        ident = hashlib.sha256(basis.encode()).hexdigest()
    return ident

def courtlistener_token():
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    path = pathlib.Path.home() / ".config" / "courtlistener" / "token"
    if not token and path.exists():
        token = path.read_text().strip()
    return token

_docket_links = {}
_log_dockets = None
BARE_HOW = {}                   # how each bare link was resolved this run: sibling / log / api / caption-search
_DOCKET_ID = re.compile(r"https://www\.courtlistener\.com/docket/(\d+)/")
_ENTRY_TITLE = re.compile(r"^(?:Entry #\d+|Minute entry from \S+) in (.+), [\w:.-]+$")


def caption_of(title):
    """The case caption in a feed entry's title: a search feed's title IS the caption; a docket feed's reads
    "Entry #15 in <caption>, <docket number>" or "Minute entry from <date> in <caption>, <docket number>"."""
    t = re.sub(r"\s+", " ", title or "").strip()
    m = _ENTRY_TITLE.match(t)
    return m.group(1) if m else t


def _unique_docket(links):
    """docket_link of the links when they all name ONE docket, else None: captions recur ("USA v. Sealed", "In re
    Grand Jury Subpoena"), and two dockets under one caption means the caption cannot pick."""
    ids = {m.group(1) for m in (_DOCKET_ID.search(l or "") for l in links) if m}
    if len(ids) != 1:
        return None
    return next(docket_link(l) for l in links if _DOCKET_ID.search(l or ""))


def _logged_docket_links():
    """{caption: [document links]} from the entry log's last ENTRY_LOG_DAYS (read once per run)."""
    global _log_dockets
    if _log_dockets is None:
        _log_dockets = {}
        try:
            with open(ENTRY_LOG, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    link = r.get("link") or ""
                    if _DOCKET_ID.search(link):
                        _log_dockets.setdefault(caption_of(r.get("title")), []).append(link)
        except OSError:
            pass
    return _log_dockets


def resolve_bare_link(entry, feed_url, siblings=None):
    """A real link for a CourtListener entry whose feed link is just the site root.

    Docket entries with no document of their own -- "Case Assigned", "Summons
    Issued", the first entries of every new case -- come through the RSS with
    https://www.courtlistener.com as their link, and the entry carries no docket id.
    Free answers first (2026-10-08, Bennett: use the API only when nothing else
    has it): a sibling entry in the same feed with the same caption that does
    link a document; else the same caption in newshound's own entry log (any feed,
    14 days); each only when every such link names ONE docket. Then the search
    API, once per feed and run, matched on the caption. Else a search for the
    exact caption, one click from the docket, rather than the homepage.
    """
    title = entry.get('title', '')
    cap = caption_of(title)
    found = _unique_docket([e.get("link") for e in (siblings or ()) if caption_of(e.get("title")) == cap])
    if found:
        BARE_HOW["sibling"] = BARE_HOW.get("sibling", 0) + 1
        return found
    found = _unique_docket(_logged_docket_links().get(cap, []))
    if found:
        BARE_HOW["log"] = BARE_HOW.get("log", 0) + 1
        return found
    query = urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get('q', [''])[0]
    token = courtlistener_token()
    if token and query:
        if query not in _docket_links:
            _docket_links[query] = {}
            url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode({"type": "r", "q": query})
            try:
                import clpace                # claude_knowledge's shared CourtListener budget (clpace.py)
                clpace.pace()
                transport = _transport()
                # One attempt through the shared transport: the token is redacted from any error, a 4xx other
                # than 429 is not retried (there is no retry here at all), and a redirect is followed as urlopen did.
                resp = transport.send("GET", url, headers={"Authorization": f"Token {token}",
                                                           "User-Agent": "newshound (Protos)"},
                                      timeout=(CONNECT_TIMEOUT, 15), redact=(token,), allow_redirects=True,
                                      retry_cap=86400,  # as cl_match: a day-cap 429 must block for the day
                                      what="newshound courtlistener docket-link search")
                for result in resp.json().get("results", []):
                    if result.get("caseName") and result.get("docket_absolute_url"):
                        _docket_links[query].setdefault(
                            result["caseName"], "https://www.courtlistener.com" + result["docket_absolute_url"])
            except Exception as exc:
                if getattr(exc, "status", None) == 429:
                    _note_courtlistener_throttle(exc)
                print(f"CourtListener API unavailable for docket links ({type(exc).__name__}); using caption searches")
        if title in _docket_links[query]:
            BARE_HOW["api"] = BARE_HOW.get("api", 0) + 1
            return _docket_links[query][title]
    BARE_HOW["caption-search"] = BARE_HOW.get("caption-search", 0) + 1
    return "https://www.courtlistener.com/?" + urllib.parse.urlencode({"q": f'caseName:("{title}")', "type": "r"})

def new_case_feed(feed_url):
    """A CourtListener feed filtered on the CASE filing date asks for new cases.

    CourtListener's RSS ignores order_by -- dateFiled, entry date and relevance
    return the identical newest filings from any case (tested 2026-09-23) -- so
    "new cases only" has to be a filter in the query, dateFiled:[now-14d TO *].
    Such a feed alerts once per case, not once per document.
    """
    q = urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get('q', [''])[0]
    return "courtlistener.com" in feed_url and "datefiled:[" in q.lower().replace(" ", "")

def case_key(entry):
    """One case, however many documents it files. The RSS gives no docket id for
    entries without a document, so the caption is the only thing every entry of a
    case shares."""
    return "case:" + re.sub(r"\s+", " ", entry.get('title', '')).strip().lower()

def docket_link(link):
    """The docket page a CourtListener link belongs to. The page needs a slug after
    the id -- /docket/<id>/ alone is a 404, which every NEW CASE link was until
    2026-09-28 -- but any slug serves it, so keep the case's own: the last segment
    of a document link, /docket/<id>/<entry>/[<attachment>/]<slug>/."""
    m = re.search(r"(https://www\.courtlistener\.com/docket/\d+/)([^?#]*)", link or "")
    if not m:
        return None
    segments = [s for s in m.group(2).split("/") if s]
    return m.group(1) + (segments[-1] if segments else "docket") + "/"

def record_key(conn, key, feed_url):
    if key.startswith("case:"):
        conn.execute("INSERT OR REPLACE INTO cases_seen (key, feed_source, first_seen) "
                     "VALUES (?, ?, datetime('now'))", (key, feed_url))
    else:
        conn.execute("INSERT OR IGNORE INTO sent_keys (key, feed_source) VALUES (?, ?)", (key, feed_url))
    conn.commit()

# Every raw entry newshound reads from any feed, CourtListener's included, logged once (2026-10-08). The morning gather reads
# its feeds once a day; DOJ's national stream holds 25 items, a few hours of it, so a release newshound alerted on
# (Spalletta, SDNY) had scrolled out before the 07:55 read. This log is what the gather merges back.
ENTRY_LOG = os.environ.get("NEWSHOUND_ENTRY_LOG",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "entries.jsonl"))
ENTRY_LOG_DAYS = 14
# One line per run, written whether or not anything new arrived: the morning gather judges "is newshound running?"
# from it. It used to judge from the entry log's newest line, which only moves when a feed has a new entry, so a quiet
# night (no entry after 06:28) read as "stale" on 2026-10-09 while newshound had run every 10 minutes.
RUN_LOG = os.environ.get("NEWSHOUND_RUN_LOG",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.jsonl"))
RUN_LOG_DAYS = 14


def record_run(started, finished, feeds, read, cl_read, cl_due, cl_total, failures, edgar=None):
    """Append this run's heartbeat to RUN_LOG and prune lines older than RUN_LOG_DAYS. Never fatal to the run.
    edgar: the EDGAR latest-filings step's counts (edgar_feed.step: pages, filings, matched, posted, routine,
    cap_hit, seeded, watch, status), when the run read it."""
    row = {"started": round(started, 1), "finished": round(finished, 1), "feeds": feeds, "read": read,
           "cl_read": cl_read, "cl_not_due": cl_total - cl_due, "cl_total": cl_total, "failures": failures}
    if edgar is not None:
        row["edgar"] = edgar
    try:
        keep = []
        cutoff = finished - RUN_LOG_DAYS * 86400
        try:
            with open(RUN_LOG, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        if json.loads(line).get("finished", 0) >= cutoff:
                            keep.append(line if line.endswith("\n") else line + "\n")
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        keep.append(json.dumps(row) + "\n")
        tmp = RUN_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, RUN_LOG)
    except OSError as exc:
        print(f"run log not written ({type(exc).__name__}: {exc})")
    return row
_logged = None


def _load_entry_log():
    """{(feed, guid)} already logged, pruning lines older than ENTRY_LOG_DAYS (the file is rewritten only when
    something was pruned). Read once per run."""
    global _logged
    if _logged is not None:
        return _logged
    _logged, keep, pruned = set(), [], 0
    cutoff = time.time() - ENTRY_LOG_DAYS * 86400
    try:
        with open(ENTRY_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    pruned += 1                 # a line cut by a crash mid-write
                    continue
                if r.get("logged_ts", 0) < cutoff:
                    pruned += 1
                    continue
                _logged.add((r.get("feed"), r.get("key") or r.get("guid")))
                keep.append(line if line.endswith("\n") else line + "\n")
    except FileNotFoundError:
        return _logged
    if pruned:
        tmp = ENTRY_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, ENTRY_LOG)
    return _logged


def log_entries(feed_url, entries):
    """Append each entry of this feed not logged before: the raw guid, link, title, summary and published time, as
    the feed served them (before check_and_alert rewrites an entry's id). CourtListener feeds are logged too (since
    2026-10-08): newshound reads each on its own schedule, and the morning gather reuses what it logged rather than
    reading 41 CourtListener feeds again. A write failure is printed, never fatal to the alerts."""
    seen = _load_entry_log()
    now, rows = time.time(), []
    for e in entries:
        guid = e.get("id")               # as served: None when the feed gives no guid (the gather then hashes link+title)
        key = guid or e.get("link")
        if guid in BARE_COURTLISTENER:
            # CourtListener gives every entry with no document of its own the site root as its guid, so keyed on the
            # guid only a feed's first such entry would ever be logged. The guid itself is still logged as served.
            key = item_identifier(e)
        if not key or (feed_url, key) in seen:
            continue
        pp = e.get("published_parsed") or e.get("updated_parsed")
        rows.append({"feed": feed_url, "guid": guid, "key": key, "link": e.get("link"), "title": e.get("title"),
                     "summary": e.get("summary"), "published": e.get("published") or e.get("updated"),
                     "published_ts": calendar.timegm(pp) if pp else None,      # feedparser's struct is UTC
                     "logged_ts": int(now)})
        seen.add((feed_url, key))
    if rows:
        try:
            with open(ENTRY_LOG, "a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        except OSError as exc:
            print(f"entry log not written ({type(exc).__name__}: {exc}); the morning gather may miss {len(rows)} entries")
    return len(rows)


def check_and_alert(feed_url, conn, sent_keys=None):
    processed_ids = get_last_processed_guid_from_db(conn, feed_url)
    if sent_keys is None:
        sent_keys = get_sent_keys(conn)
    # A feed URL never fetched before is new -- added, or edited, which makes it a
    # new key. Record what it holds now without posting; alert from the next item on.
    first_sight = conn.execute("SELECT 1 FROM feeds_seen WHERE feed_source = ?",
                               (feed_url,)).fetchone() is None
    keywords = get_keywords()
    feed = parse_feed(feed_url, **load_validators(conn, feed_url, keywords))
    try:
        return handle_feed(feed_url, conn, sent_keys, feed, first_sight, keywords, processed_ids)
    except Exception as exc:
        # The read itself worked; what failed came after it (a Slack post). Say so, so the run does not count it
        # against the feed's health (record_read).
        try:
            exc.feed_read = feed
        except AttributeError:
            pass
        raise


def handle_feed(feed_url, conn, sent_keys, feed, first_sight, keywords, processed_ids):
    """Log, filter, alert on and record one feed's read: check_and_alert after the fetch."""
    # 304 Not Modified: nothing changed since the last full read, whose entries were all handled then. Not an empty
    # feed, and no state to touch; the stored validators go out again next run.
    if getattr(feed, "status", None) == 304:
        return feed
    log_entries(feed_url, feed.entries)
    # An empty feed is not an exception. Say so, or a dead source looks identical
    # to a quiet one -- which is how the SEC feeds hid for as long as they did.
    if getattr(feed, "status", 200) >= 400 or not feed.entries:
        print(f"EMPTY feed (http={getattr(feed, 'status', '?')}): {feed_url}")
    keyword_pattern = build_keyword_pattern(keywords)
    per_case = new_case_feed(feed_url)
    excluded = get_excluded_dockets()
    skipped = 0
    new_items = []

    for entry in feed.entries:
        entry['id'] = item_identifier(entry)
        entry['_key'] = canonical_key(entry['id'], entry.get('link', ''))
        entry['_case'] = case_key(entry) if per_case else None

        # 1. STATE CHECK: skip if this feed processed it, or ANY feed already sent
        # the same document
        if str(entry.get('id')) in processed_ids or entry['_key'] in sent_keys:
            # A case whose filing already went out through another feed is not
            # news as a new case either.
            if per_case and entry['_case'] not in sent_keys:
                record_key(conn, entry['_case'], feed_url)
                sent_keys.add(entry['_case'])
            continue
        if per_case and entry['_case'] in sent_keys:
            continue                   # later filings in a case already announced
        if excluded_docket(entry.get('link', ''), excluded):
            skipped += 1
            continue

        # 2. FILTERING: Check the requirement
        # A CourtListener URL is ALREADY the filter: /feed/search/?q=... carries the
        # query, and /docket/<id>/feed/ is one docket picked on purpose. Re-running
        # the keyword pattern over it can only subtract, and it does -- a docket is
        # captioned by party ("United States v. Saylor"), so the q=michael+saylor
        # search returned 20 results of which the keyword "michael saylor" matched
        # ZERO. Trust the search.
        if pre_filtered(feed_url) or keyword_pattern.search(get_entry_text(entry)):
            new_items.append(entry)


    # Mark the feed seen on any successful fetch, EMPTY included: a new-case feed
    # with no cases yet (michael saylor, 2026-09-23) would otherwise stay unseen
    # and have its first real case swallowed by this quiet first pass.
    fetched = getattr(feed, "status", None) is not None and feed.status < 400
    if first_sight and (feed.entries or fetched):
        # Recorded for THIS feed only: the quiet pass must not stop another feed
        # from sending an item it has never sent.
        for item in new_items:
            update_last_processed_guid_in_db(conn, feed_url, item['id'])
            if per_case and item['_case'] not in sent_keys:
                record_key(conn, item['_case'], feed_url)
                sent_keys.add(item['_case'])
        conn.execute("INSERT OR IGNORE INTO feeds_seen (feed_source) VALUES (?)", (feed_url,))
        conn.commit()
        print(f"NEW FEED: recorded {len(new_items)} existing item(s) without alerting: {feed_url}")
        for item in new_items:
            print(f"    - {item.get('title', '')[:100]} {item.get('link', '')}")
        save_validators(conn, feed_url, feed, keywords)
        return feed

    if skipped:
        print(f"EXCLUDED: {skipped} entr{'y' if skipped == 1 else 'ies'} of excluded dockets not alerted: {feed_url}")
    mode = FEED_FILTERS.get(feed_url)
    routine = []
    if mode == "key" and new_items:
        keyed = []
        for item in new_items:
            (keyed if key_filing(item)[0] else routine).append(item)
        new_items = keyed
        if routine:
            print(f"FILTER key: {len(keyed)} key, {len(routine)} batched: {feed_url}")

    if new_items:
        print(f"Found {len(new_items)} matching items.")
        
        # 3. ALERTING - Process each match
        for item in new_items:
            # Two feeds in one run can carry the same document, and one new case
            # can file several documents at once
            if item['_key'] in sent_keys or (per_case and item['_case'] in sent_keys):
                continue
            link = item.get('link', '')
            if link in BARE_COURTLISTENER:
                link = resolve_bare_link(item, feed_url, siblings=feed.entries)
                # Its docket is known only now. Recorded as processed, not as sent: this feed never resolves it again.
                if (docket := excluded_docket(link, excluded)):
                    print(f"EXCLUDED: a bare-link entry of docket {docket} not alerted: {feed_url}")
                    update_last_processed_guid_in_db(conn, feed_url, item['id'])
                    continue
            if per_case:
                link = docket_link(link) or link
                message = f"🚨 NEW CASE! Title: {item['title']}\nLink: {link}"
            else:
                message = f"🚨 NEW ALERT! Title: {item['title']}\nLink: {link}"
            
            # Send alert (Prints if LOCAL_TESTING env var is set)
            if os.environ.get("LOCAL_TESTING") == "True":
                print(f"🔌 [TEST MODE MOCK]: {message}")
            else:
                get_slack_client().chat_postMessage(text=message, channel="#newshound-feed")
            # Record each item as soon as it is sent, not after the whole feed: a run
            # stopped mid-feed used to leave its posted items unrecorded, to be
            # posted again next time.
            update_last_processed_guid_in_db(conn, feed_url, item['id'], item['_key'])
            sent_keys.add(item['_key'])
            if per_case:
                record_key(conn, item['_case'], feed_url)
                sent_keys.add(item['_case'])
            if os.environ.get("LOCAL_TESTING") != "True":
                time.sleep(10)
    for item in routine:
        if item['_key'] in sent_keys:
            continue
        conn.execute("INSERT OR IGNORE INTO held_routine (key, feed_source, title, link, summary, held_ts) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (item['_key'], feed_url, item.get('title', ''), item.get('link', ''),
                                                   item.get('summary', ''), time.time()))
        update_last_processed_guid_in_db(conn, feed_url, item['id'], item['_key'])
        sent_keys.add(item['_key'])
    conn.commit()
    # Last, once every item is sent and recorded: validators saved earlier would let a run stopped mid-feed come back
    # to a 304 and never send the rest.
    save_validators(conn, feed_url, feed, keywords)
    return feed

def get_rss_feeds():
    """The feed URLs in rss.md, in order. A line may end in a marker after whitespace and '#' ('<url>  # filter: key',
    FEED_FILTERS); the URL alone is the feed's identity, so adding or removing a marker never gives it a new key (a new
    key would mean a quiet first pass). A line starting with '#' is a comment."""
    # utf-8-sig strips a real BOM; the old lstrip('ufeff') only stripped the
    # literal characters u, f and e from the start of the URL.
    with open("rss.md", encoding='utf-8-sig') as f:
        lines = [line.strip() for line in f]
    urls = []
    FEED_FILTERS.clear()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+#", line, maxsplit=1)
        url, marker = parts[0].strip(), (parts[1] if len(parts) > 1 else "")
        m = re.match(r"filter:\s*(\S+)", marker.strip())
        if m:
            if m.group(1) in FILTER_MODES:
                FEED_FILTERS[url] = m.group(1)
            else:
                print(f"UNKNOWN FILTER {m.group(1)!r} (known: {', '.join(FILTER_MODES)}): every entry posts: {url}")
        urls.append(url)
    return urls


# --- Busy dockets: post the key filings, batch the rest (Bennett, 2026-10-21) ------------------------------------
# The 127,271 BTC forfeiture docket (E.D.N.Y. 1:25-cv-05745, docket 71639393) posted 168 entries in two weeks, 10 to
# 30 a day: terror-victim creditor groups' answers, claims, crossclaims and counsel notices. A docket line in rss.md
# marked '# filter: key' posts one by one only what moves the case -- the court's own entries (orders, opinions,
# judgments, minute entries), appeals, motions other than counsel housekeeping, and filings by the government or a
# party named in FILTER_PRINCIPALS -- and holds everything else for ONE daily roll-up per docket (Bennett: "once a
# day ~9am"), posted by the first run at or after DIGEST_HOUR local time, naming how many, of what kind and by whom.
# A held entry is recorded as sent at once, so no feed posts it alone; it waits in held_routine until its roll-up
# posts, so a restart or a missed run never loses it.
FILTER_MODES = ("key",)
DIGEST_HOUR = 9                  # local time: the roll-up goes out with the first run at or after 09:00
FEED_FILTERS = {}                # feed url -> mode, filled by get_rss_feeds()
FILTER_PRINCIPALS = re.compile(r"lubian|chen\s+zhi|prince\s+(?:group|holding)", re.I)
_COURT = re.compile(r"^(?:electronic\s+|amended\s+|sealed\s+)?(?:order|memorandum|opinion|judgment|decision|"
                    r"scheduling order|report and recommendation|mandate|usca|stipulation and order|"
                    r"(?:clerk'?s\s+)?entry of default)\b", re.I)
_APPEAL = re.compile(r"\bnotice of (?:interlocutory\s+|cross[- ])?appeal\b|\bleave to appeal\b|\bpetition for "
                     r"(?:writ|review)\b", re.I)
_MOTION = re.compile(r"^(?:[\w'.-]+\s+){0,3}motion\b", re.I)
_HOUSEKEEPING = re.compile(r"withdraw(?:al)?\s+as\s+(?:attorney|counsel)|pro\s+hac\s+vice|substitut\w*\s+(?:of\s+)?"
                           r"(?:attorney|counsel)|notice of appearance|electronic(?:ally)? fil|admission|"
                           r"index to (?:the )?record on appeal|consent to electronic", re.I)
_GOVERNMENT = re.compile(r"^(?:the\s+)?(?:plaintiff\s+)?(?:united states(?: of america)?|usa|government)\b", re.I)


def docket_text(entry):
    """A docket feed entry's docket text, tags and runs of space dropped."""
    s = re.sub(r"<[^>]+>", " ", str(entry.get("summary") or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def filer_of(text):
    """'ANSWER to Complaint by Gold Star Claimants re [1] ...' -> 'Gold Star Claimants' (the 'by' clause before 're',
    '[', '(' or a comma), or ''."""
    m = re.search(r"\bby\s+(.{2,80}?)(?=\s+re\b|\s*\[|\s*\(|,|\.\s|$)", text[:300])
    return m.group(1).strip() if m else ""


def filing_kind(text):
    """'ANSWER to Complaint by ...' -> 'answer': the docket text's first words up to 'by', 're', 'to' or '['."""
    m = re.match(r"(.{1,40}?)(?=\s+(?:by|re|to|on|of|for)\b|\s*\[|\s*\(|:|$)", text)
    return (m.group(1) if m else text[:30]).strip().lower() or "entry"


def key_filing(entry):
    """-> (True, why) for an entry a '# filter: key' docket posts one by one, else (False, '')."""
    text = docket_text(entry)
    filer = filer_of(text)
    if _HOUSEKEEPING.search(text[:200]):             # counsel and clerk housekeeping, the court's own grants included
        return False, ""
    if "#minute-entry-" in str(entry.get("link") or entry.get("id") or ""):
        return True, "the court's minute entry"
    if _COURT.match(text):
        return True, "the court's own entry"
    if _APPEAL.search(text[:200]):
        return True, "an appeal"
    if _GOVERNMENT.match(filer):
        return True, "the government's filing"
    if FILTER_PRINCIPALS.search(filer) or FILTER_PRINCIPALS.search(text[:120]):
        return True, "a principal's filing"
    if _MOTION.match(text):
        return True, "a motion"
    return False, ""


def batch_message(items, since=None):
    """One Slack post for a filtered docket's routine entries: how many, of what kind, by whom, and the docket.
    since: the earliest held time (a datetime), said in the header."""
    title = str(items[0].get("title") or "")
    caption = title.split(" in ", 1)[1] if " in " in title else title
    kinds, filers = collections.Counter(), collections.Counter()
    for it in items:
        text = docket_text(it)
        kinds[filing_kind(text)] += 1
        f = filer_of(text)
        if f:
            filers[f] += 1
    link = docket_link(str(items[0].get("link") or "")) or str(items[0].get("link") or "")
    kind_s = ", ".join(f"{k} {n}" for k, n in kinds.most_common(6)) + (" and more" if len(kinds) > 6 else "")
    by_s = "; ".join(f for f, _n in filers.most_common(6)) + (f"; +{len(filers) - 6} more" if len(filers) > 6 else "")
    when = f" since {since.strftime('%a %d %b %H:%M')}" if since else ""
    return (f"🗂️ Daily roll-up: {len(items)} routine filing{'s' if len(items) != 1 else ''} in {caption}{when} "
            f"('# filter: key')\n"
            f"Kinds: {kind_s}" + (f"\nBy: {by_s}" if by_s else "") + f"\nDocket: {link}")


# --- How often each CourtListener feed is read (2026-10-08) -------------------------------------------------------
# CourtListener is shared, donor-funded infrastructure. Its /feeds/ page offers the feeds to "an app or other program
# that can regularly check the feed" -- regularly, not every ten minutes. The 2026-10-08 audit counted 41 CourtListener
# feeds in rss.md read on every one of launchd's 144 runs a day: ~5,900 requests a day (~3,500 on a day of failed
# runs). Conditional requests are no help: measured 2026-10-08, a docket feed answered an If-Modified-Since matching
# its own Last-Modified with 200 and the full body, so how often we ask is the only lever. A docket's newest entry
# says how active it is; every other feed is still read on every run.
CL_HOST = "www.courtlistener.com"
CL_SEARCH_INTERVAL = 30 * 60           # /feed/search/ (new cases, every filing matching a query): every 30 min
CL_ACTIVE_DAYS = 2                     # a docket whose newest entry is at most this old ...
CL_ACTIVE_INTERVAL = 30 * 60           # ... every 30 min
CL_RECENT_DAYS = 14                    # newest entry at most this old ...
CL_RECENT_INTERVAL = 2 * 3600          # ... every 2 h
CL_STALE_INTERVAL = 6 * 3600           # older, or no dated entry at all: every 6 h
CL_RETRY_INTERVAL = 30 * 60            # a read that failed (both attempts, or an HTTP error) is tried again in 30 min
CL_FEED_GAP = 1                        # seconds between CourtListener reads in one run
# launchd starts a run every 600 s and a feed is reached some seconds into it, at a slightly different point each run.
# Due exactly `interval` after the last read, a 30 min feed would be a few seconds short of due on the run 30 min later
# and wait for the next one: 40 min, not 30. Due half a run early, it is read on the run nearest its interval.
CL_RUN_SLACK = 300


def is_courtlistener(feed_url):
    return (urllib.parse.urlparse(feed_url).hostname or "").lower() == CL_HOST


def cl_search_feed(feed_url):
    return urllib.parse.urlparse(feed_url).path.startswith("/feed/search/")


def cl_interval(feed_url, newest_ts, now):
    """Seconds until a CourtListener feed is read again: a search feed every CL_SEARCH_INTERVAL, a docket feed by the
    age of its newest entry."""
    if cl_search_feed(feed_url):
        return CL_SEARCH_INTERVAL
    if newest_ts is None:
        return CL_STALE_INTERVAL
    age = now - newest_ts
    if age <= CL_ACTIVE_DAYS * 86400:
        return CL_ACTIVE_INTERVAL
    if age <= CL_RECENT_DAYS * 86400:
        return CL_RECENT_INTERVAL
    return CL_STALE_INTERVAL


def cl_first_window(feed_url):
    """The window a feed with no schedule row is staggered across. A docket's activity is unknown until it is read
    under the schedule, so it gets the shortest docket interval: no docket waits longer than 30 min for that read."""
    return CL_SEARCH_INTERVAL if cl_search_feed(feed_url) else CL_ACTIVE_INTERVAL


def cl_stagger(feed_url, window):
    """A stable offset in [0, window) from the URL alone (sha256, not hash(), which changes every process), so the
    CourtListener feeds come due spread across the window rather than all in one run."""
    return int.from_bytes(hashlib.sha256(feed_url.encode()).digest()[:8], "big") % int(window)


def newest_entry_ts(feed):
    """The newest entry's time (epoch seconds, UTC) -- or, when no entry is dated, the Last-Modified header -- or
    None."""
    stamps = [calendar.timegm(pp) for e in getattr(feed, "entries", []) or []
              for pp in [e.get("published_parsed") or e.get("updated_parsed")] if pp]
    if stamps:
        return max(stamps)
    headers = getattr(feed, "headers", None) or {}
    modified = headers.get("last-modified") or headers.get("Last-Modified")
    if modified:
        try:
            import email.utils
            return email.utils.parsedate_to_datetime(modified).timestamp()
        except (TypeError, ValueError):
            return None
    return None


def cl_due(conn, feed_url, now):
    """Whether a CourtListener feed is read this run. A feed never fetched (not in feeds_seen) is read now, so its
    first-sight baseline happens when it is added. A known feed with no schedule row gets one, staggered."""
    row = conn.execute("SELECT next_due FROM feed_schedule WHERE feed_source = ?", (feed_url,)).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM feeds_seen WHERE feed_source = ?", (feed_url,)).fetchone() is None:
            return True
        window = cl_first_window(feed_url)
        conn.execute("INSERT INTO feed_schedule (feed_source, next_due, interval, newest_ts) VALUES (?, ?, ?, NULL)",
                     (feed_url, now + cl_stagger(feed_url, window), window))
        conn.commit()
        row = conn.execute("SELECT next_due FROM feed_schedule WHERE feed_source = ?", (feed_url,)).fetchone()
    return row[0] <= now


def cl_reschedule(conn, feed_url, feed, now):
    """Set when a CourtListener feed is next read, after this run read it. feed is what check_and_alert returned, None
    when both attempts raised. An HTTP error parses as an empty feed rather than raising, and is a failure too: a 429
    or 503 is no evidence the docket went quiet. A failure keeps the newest entry already known."""
    row = conn.execute("SELECT newest_ts FROM feed_schedule WHERE feed_source = ?", (feed_url,)).fetchone()
    known = row[0] if row else None
    if read_ok(feed):
        newest = max([t for t in (known, newest_entry_ts(feed)) if t is not None], default=None)
        interval = cl_interval(feed_url, newest, now)
        next_due = now + interval - CL_RUN_SLACK
    else:
        newest = known
        interval = cl_interval(feed_url, newest, now)
        next_due = now + CL_RETRY_INTERVAL - CL_RUN_SLACK
    conn.execute("INSERT OR REPLACE INTO feed_schedule (feed_source, next_due, interval, newest_ts) VALUES (?, ?, ?, ?)",
                 (feed_url, next_due, interval, newest))
    conn.commit()
    return next_due


# --- Conditional reads and failure backoff for every other feed (2026-10-08) ----------------------------------------
# Every non-CourtListener feed was downloaded in full on each of launchd's 144 runs a day. Measured 2026-10-08, the
# gov.uk FCDO atom, OCC, FDIC (govdelivery), Federal Reserve, FCA, CFPB, ECB, White House and ssf.gob.sv feeds answer
# a conditional request with 304 and no body, so each sends back the ETag / Last-Modified of its last 200.
# CourtListener ignores them (its feeds are paced by feed_schedule instead) and is sent none, as before.
# A feed that keeps failing -- the Federal Reserve's 404s, cftc.gov's 403s and 60 s stalls -- was asked again every
# run. After BACKOFF_TIERS consecutive failures it is left alone for a while, saying so once per step.
BACKOFF_TIERS = ((10, 6 * 3600), (6, 2 * 3600), (3, 30 * 60))     # (consecutive failures, seconds not read)


def keywords_signature(keywords):
    return hashlib.sha256("\n".join(keywords).encode()).hexdigest()


def load_validators(conn, feed_url, keywords):
    """{etag, modified} to send with this feed's read: what its last 200 carried. None for CourtListener, and none
    when the keyword list changed since: a 304 would skip re-scanning the items already in the feed, which a new
    keyword can match."""
    if is_courtlistener(feed_url):
        return {}
    row = conn.execute("SELECT etag, modified, keywords FROM feed_validators WHERE feed_source = ?",
                       (feed_url,)).fetchone()
    if row is None or row[2] != keywords_signature(keywords):
        return {}
    # Apache's mod_deflate serves a compressed body under the ETag plus "-gzip" and then fails to match that ETag sent
    # back (measured 2026-10-08, ssf.gob.sv: "X-gzip" -> 200, "X" -> 304). Sent without the suffix it matches; a server
    # whose ETag really ends so just answers 200, as with no ETag.
    etag = re.sub(r'-(?:gzip|br|deflate)"$', '"', row[0]) if row[0] else None
    return {k: v for k, v in (("etag", etag), ("modified", row[1])) if v}


def save_validators(conn, feed_url, feed, keywords):
    """Store the validators of a 200 (none if it carried none). Any other answer but a 304 drops them: a redirect or
    an error is no full read, and a stored ETag sent through a redirect comes back as a 304 hidden behind the
    redirect's status."""
    if is_courtlistener(feed_url):
        return
    status = getattr(feed, "status", None)
    if status == 304:
        return
    if status == 200:
        etag, modified = getattr(feed, "etag", None), getattr(feed, "modified", None)
        conn.execute("INSERT OR REPLACE INTO feed_validators (feed_source, etag, modified, keywords) "
                     "VALUES (?, ?, ?, ?)", (feed_url, etag, modified, keywords_signature(keywords)))
    else:
        conn.execute("DELETE FROM feed_validators WHERE feed_source = ?", (feed_url,))
    conn.commit()


def read_ok(feed):
    """Whether a read succeeded. feed is what check_and_alert returned, None when both attempts raised. An HTTP error
    parses as an empty feed rather than raising; a connection error as one with no status at all. 304 is a success."""
    if feed is None:
        return False
    status = getattr(feed, "status", None)
    return status < 400 if status is not None else bool(getattr(feed, "entries", None))


def read_error(feed, exc_note=None):
    """A short reason for a failed read, for the BACKOFF line."""
    if feed is None:
        return exc_note or "no answer"
    status = getattr(feed, "status", None)
    if status is not None:
        return f"http={status}"
    bozo = feed.get("bozo_exception") if hasattr(feed, "get") else getattr(feed, "bozo_exception", None)
    return type(bozo).__name__ if bozo is not None else "no status"


def backoff_seconds(failures):
    return next((wait for n, wait in BACKOFF_TIERS if failures >= n), 0)


def span(seconds):
    return f"{seconds // 3600:g}h" if seconds % 3600 == 0 else f"{seconds // 60:g}m"


def feed_due(conn, feed_url, now):
    """Whether a non-CourtListener feed is read this run: always, unless it is in backoff."""
    row = conn.execute("SELECT skip_until FROM feed_health WHERE feed_source = ?", (feed_url,)).fetchone()
    return row is None or not row[0] or row[0] <= now


def record_read(conn, feed_url, ok, error, now):
    """Count a non-CourtListener feed's consecutive failures. At 3, 6 and 10 it is left unread for 30 min, 2 h and 6 h
    after each failure (due half a run early, as CL_RUN_SLACK explains), with one BACKOFF line at each step; a
    success clears the count, with one RECOVERED line if the feed had been in backoff."""
    row = conn.execute("SELECT failures FROM feed_health WHERE feed_source = ?", (feed_url,)).fetchone()
    failures = row[0] if row else 0
    if ok:
        if row is not None:
            conn.execute("DELETE FROM feed_health WHERE feed_source = ?", (feed_url,))
            conn.commit()
        if backoff_seconds(failures):
            print(f"RECOVERED after {failures} failures: {feed_url}")
        return
    failures += 1
    wait = backoff_seconds(failures)
    skip_until = now + wait - CL_RUN_SLACK if wait else None
    conn.execute("INSERT OR REPLACE INTO feed_health (feed_source, failures, skip_until, last_error) "
                 "VALUES (?, ?, ?, ?)", (feed_url, failures, skip_until, error))
    conn.commit()
    if wait and wait != backoff_seconds(failures - 1):
        print(f"BACKOFF {span(wait)} after {failures} failures ({error}): {feed_url}")


def post_alert(message):
    """One Slack post to #newshound-feed (printed instead under LOCAL_TESTING)."""
    if os.environ.get("LOCAL_TESTING") == "True":
        print(f"🔌 [TEST MODE MOCK]: {message}")
    else:
        get_slack_client().chat_postMessage(text=message, channel="#newshound-feed")


def post_pause():
    """newshound's 10 s between Slack posts (none under LOCAL_TESTING)."""
    if os.environ.get("LOCAL_TESTING") != "True":
        time.sleep(10)


# SEC EDGAR's latest-filings feed (edgar_feed.py, approved 2026-10-12): read first in each run, so a filing pings
# within one launchd interval. Imported on first use, like the transport: a broken EDGAR source must cost the run
# its EDGAR read, never its other 55 feeds.
EDGAR_SOURCE = "EDGAR latest filings"


def digest_step(conn, now=None):
    """Post each filtered docket's held routine entries as one roll-up, once a local day, with the first run at or after
    DIGEST_HOUR; delete a docket's held rows only after its post went out. -> number of roll-ups posted."""
    now = now or datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    row = conn.execute("SELECT last_date FROM digest_state WHERE name = 'routine'").fetchone()
    if now.hour < DIGEST_HOUR or (row and row[0] == today):
        return 0
    posted = 0
    feeds = [r[0] for r in conn.execute("SELECT DISTINCT feed_source FROM held_routine ORDER BY feed_source")]
    for feed_url in feeds:
        held = conn.execute("SELECT key, title, link, summary, held_ts FROM held_routine WHERE feed_source = ? "
                            "ORDER BY held_ts", (feed_url,)).fetchall()
        items = [{"title": t, "link": l, "summary": s_} for _k, t, l, s_, _ts in held]
        post_alert(batch_message(items, since=datetime.datetime.fromtimestamp(held[0][4])))
        conn.executemany("DELETE FROM held_routine WHERE key = ?", [(k,) for k, *_ in held])
        conn.commit()
        posted += 1
        post_pause()
    conn.execute("INSERT OR REPLACE INTO digest_state (name, last_date) VALUES ('routine', ?)", (today,))
    conn.commit()
    return posted


def edgar_step(conn, sent_keys):
    """-> (edgar_feed.step's summary, failure note or None)."""
    import edgar_feed
    return edgar_feed.step(conn, sent_keys, post_alert, post_pause)


def run(conn, feed_urls, sent_keys=None, edgar=None):
    """Read every feed once -- CourtListener's only when due, any other only when not in backoff -- and return
    [(feed url, error)] for those that failed. edgar: the EDGAR step (edgar_step), run first; None skips it
    (the tests' runs, a dry run of the feeds alone)."""
    if sent_keys is None:
        sent_keys = get_sent_keys(conn)
    failures = []
    started = time.time()
    edgar_summary = None
    if edgar is not None:
        try:
            edgar_summary, note = edgar(conn, sent_keys)
            if note:
                failures.append((EDGAR_SOURCE, note))
        except Exception as exc:
            failures.append((EDGAR_SOURCE, f"{type(exc).__name__}: {str(exc)[:160]}"))
            edgar_summary = {"status": "crashed"}
            print(f"EDGAR step failed ({type(exc).__name__}: {str(exc)[:200]}); the feeds are read as usual")
    try:
        rolled = digest_step(conn)
        if rolled:
            print(f"ROLL-UP: posted {rolled} daily routine roll-up(s)")
    except Exception as exc:
        failures.append(("daily roll-up", f"{type(exc).__name__}: {str(exc)[:160]}"))
        print(f"daily roll-up failed ({type(exc).__name__}: {str(exc)[:200]}); held entries kept for the next run")
    read_total = 0
    cl_total = cl_read = cl_skipped = 0
    for feed_url in feed_urls:
        cl = is_courtlistener(feed_url)
        if cl:
            cl_total += 1
            if not cl_due(conn, feed_url, time.time()):
                cl_skipped += 1
                continue
            cl_read += 1
            if os.environ.get("LOCAL_TESTING") != "True":
                time.sleep(CL_FEED_GAP)
        elif not feed_due(conn, feed_url, time.time()):
            continue
        read_total += 1
        # One feed must never cost the rest of the run. socket.setdefaulttimeout
        # stops a dead server stalling us, but feedparser lets the TimeoutError
        # propagate, so before this guard the first slow feed aborted the loop and
        # every feed BELOW it in rss.md went unchecked -- silently, because the
        # next cycle starts again at the top. 148 such aborts in the error log.
        t0 = time.time()
        parsed = exc_note = read_anyway = None
        try:
            parsed = check_and_alert(feed_url, conn, sent_keys)
        except Exception as exc:
            # Measured 2026-09-17: the two feeds that time out in production
            # (q=htx, q=yuchen+sun) answer in 0.3-0.4s when asked directly, so
            # a 30s read timeout is not a ceiling set too low -- it is an
            # intermittent stall on a fast server. Raising the ceiling would
            # only make a bad cycle slower. One retry is the mitigation that
            # matches the fault; if it fails twice, record it and move on.
            time.sleep(2)
            try:
                parsed = check_and_alert(feed_url, conn, sent_keys)
                print(f"RETRY OK after {type(exc).__name__}: {feed_url}")
            except Exception as exc2:
                exc_note = f"{type(exc2).__name__} x2"
                failures.append((feed_url, exc_note))
                read_anyway = getattr(exc2, "feed_read", None)
        if cl:
            try:
                cl_reschedule(conn, feed_url, parsed, time.time())
            except sqlite3.Error as exc:
                print(f"CourtListener schedule not saved ({type(exc).__name__}: {exc}): {feed_url}")
        else:
            try:
                read = parsed if parsed is not None else read_anyway
                ok = read_ok(read)
                record_read(conn, feed_url, ok, None if ok else read_error(read, exc_note), time.time())
            except sqlite3.Error as exc:
                print(f"feed health not saved ({type(exc).__name__}: {exc}): {feed_url}")
        slow = time.time() - t0
        if slow > 10:
            print(f"SLOW {slow:.0f}s: {feed_url}")
    if cl_total:
        print(f"{time.strftime('%Y-%m-%d %H:%M')} CourtListener: read {cl_read} of {cl_total} feeds due "
              f"({cl_skipped} not due)")
    record_run(started, time.time(), len(feed_urls), read_total, cl_read, cl_total - cl_skipped, cl_total, len(failures),
               edgar=edgar_summary)
    return failures


if __name__ == "__main__":
    conn = initialize_db()
    try:
        # The EDGAR step is off by default (Bennett 2026-10-13: it alerted too often); NEWSHOUND_EDGAR=1 turns it
        # back on. Filings still reach the morning gather through its full-text search.
        failures = run(conn, get_rss_feeds(), get_sent_keys(conn),
                       edgar=edgar_step if os.environ.get("NEWSHOUND_EDGAR") == "1" else None)
    finally:
        conn.close()
    if failures:
        # stdout, so a skipped feed shows up beside the "Found N" lines rather than
        # only as a traceback in the error log nobody reads.
        print(f"{len(failures)} feed(s) failed this run: " +
              ", ".join(f"{u} ({e})" for u, e in failures))
