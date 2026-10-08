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
import urllib.parse
import urllib.request

# Cap any single feed fetch: feedparser has no timeout of its own, so one
# unresponsive server would otherwise stall the whole run.
socket.setdefaulttimeout(30)

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

def canonical_key(item_id, link):
    """The identity of the DOCUMENT an item points at, whichever feed found it.

    CourtListener spells one docket entry two ways: a search feed links
    /docket/<id>/<entry>/[<attachment>/]<slug>/ and a docket feed links
    /docket/<id>/<slug>/?order_by=desc#entry-<entry>. Both reduce to
    cl:<id>:<entry>[:<attachment>]. A minute entry has no entry number, only
    #minute-entry-<n>, and is cl:<id>:minute:<n> -- keyed on the docket page they
    all link to, every minute entry after a docket's first went unsent. Anything
    else is keyed on its link without query string or fragment, or on its feed id
    when it has no link.
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


def parse_feed(feed_url):
    return feedparser.parse(feed_url, agent=FEED_AGENT) if FEED_AGENT \
        else feedparser.parse(feed_url)


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

def resolve_bare_link(entry, feed_url):
    """A real link for a CourtListener entry whose feed link is just the site root.

    Docket entries with no document of their own -- "Case Assigned", "Summons
    Issued", the first entries of every new case -- come through the RSS with
    https://www.courtlistener.com as their link. The search API returns each
    result's docket URL, so ask it once per feed (only when such an entry is about
    to be sent) and match on the caption. If the API is unavailable -- no token, a
    rate limit -- link to a search for the exact caption, which is one click from
    the docket, rather than to the homepage.
    """
    title = entry.get('title', '')
    query = urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get('q', [''])[0]
    token = courtlistener_token()
    if token and query:
        if query not in _docket_links:
            _docket_links[query] = {}
            url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode({"type": "r", "q": query})
            try:
                import clpace                # claude_knowledge's shared CourtListener budget (clpace.py)
                clpace.pace()
                req = urllib.request.Request(url, headers={"Authorization": f"Token {token}",
                                                           "User-Agent": "newshound (Protos)"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    for result in json.loads(resp.read()).get("results", []):
                        if result.get("caseName") and result.get("docket_absolute_url"):
                            _docket_links[query].setdefault(
                                result["caseName"], "https://www.courtlistener.com" + result["docket_absolute_url"])
            except Exception as exc:
                print(f"CourtListener API unavailable for docket links ({type(exc).__name__}); using caption searches")
        if title in _docket_links[query]:
            return _docket_links[query][title]
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

# Every raw entry newshound reads from a non-CourtListener feed, logged once (2026-10-08). The morning gather reads
# its feeds once a day; DOJ's national stream holds 25 items, a few hours of it, so a release newshound alerted on
# (Spalletta, SDNY) had scrolled out before the 07:55 read. This log is what the gather merges back.
ENTRY_LOG = os.environ.get("NEWSHOUND_ENTRY_LOG",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "entries.jsonl"))
ENTRY_LOG_DAYS = 14
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
    the feed served them (before check_and_alert rewrites an entry's id). CourtListener feeds are left out: the
    gather reads and enriches those live. A write failure is printed, never fatal to the alerts."""
    if "courtlistener.com" in feed_url:
        return 0
    seen = _load_entry_log()
    now, rows = time.time(), []
    for e in entries:
        guid = e.get("id")               # as served: None when the feed gives no guid (the gather then hashes link+title)
        key = guid or e.get("link")
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
    feed = parse_feed(feed_url)
    log_entries(feed_url, feed.entries)
    # An empty feed is not an exception. Say so, or a dead source looks identical
    # to a quiet one -- which is how the SEC feeds hid for as long as they did.
    if getattr(feed, "status", 200) >= 400 or not feed.entries:
        print(f"EMPTY feed (http={getattr(feed, 'status', '?')}): {feed_url}")
    keyword_pattern = build_keyword_pattern(get_keywords())
    per_case = new_case_feed(feed_url)
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
        return

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
                link = resolve_bare_link(item, feed_url)
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

def get_rss_feeds():
    # utf-8-sig strips a real BOM; the old lstrip('ufeff') only stripped the
    # literal characters u, f and e from the start of the URL.
    with open("rss.md", encoding='utf-8-sig') as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line]


if __name__ == "__main__":
    conn = initialize_db()
    sent_keys = get_sent_keys(conn)
    failures = []
    try:
        for feed_url in get_rss_feeds():
            # One feed must never cost the rest of the run. socket.setdefaulttimeout
            # stops a dead server stalling us, but feedparser lets the TimeoutError
            # propagate, so before this guard the first slow feed aborted the loop and
            # every feed BELOW it in rss.md went unchecked -- silently, because the
            # next cycle starts again at the top. 148 such aborts in the error log.
            t0 = time.time()
            try:
                check_and_alert(feed_url, conn, sent_keys)
            except Exception as exc:
                # Measured 2026-09-17: the two feeds that time out in production
                # (q=htx, q=yuchen+sun) answer in 0.3-0.4s when asked directly, so
                # a 30s read timeout is not a ceiling set too low -- it is an
                # intermittent stall on a fast server. Raising the ceiling would
                # only make a bad cycle slower. One retry is the mitigation that
                # matches the fault; if it fails twice, record it and move on.
                time.sleep(2)
                try:
                    check_and_alert(feed_url, conn, sent_keys)
                    print(f"RETRY OK after {type(exc).__name__}: {feed_url}")
                except Exception as exc2:
                    failures.append((feed_url, f"{type(exc2).__name__} x2"))
            slow = time.time() - t0
            if slow > 10:
                print(f"SLOW {slow:.0f}s: {feed_url}")
    finally:
        conn.close()
    if failures:
        # stdout, so a skipped feed shows up beside the "Found N" lines rather than
        # only as a traceback in the error log nobody reads.
        print(f"{len(failures)} feed(s) failed this run: " +
              ", ".join(f"{u} ({e})" for u, e in failures))
