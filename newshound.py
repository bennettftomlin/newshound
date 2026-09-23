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

# Cap any single feed fetch: feedparser has no timeout of its own, so one
# unresponsive server would otherwise stall the whole run.
socket.setdefaulttimeout(30)

DB_PATH = "last_processed.db"

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
    conn.commit()
    return conn

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

def update_last_processed_guid_in_db(conn, feed_url, item_id):
    #"\"\"Marks a single specific item as processed in the database.\"\"\"
    cursor = conn.cursor()
    try:
        # Insert or ignore ensures that if we retry anyway, it doesn't crash on duplicates
        cursor.execute("INSERT OR IGNORE INTO processed_items (feed_source, item_id) VALUES (?, ?)", (feed_url, str(item_id)))
        conn.commit()
    except sqlite3.OperationalError:
        pass


slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
# Example of using the client for initialization purposes (assuming token is set)
try:
    slack_client.auth_test() 
except Exception as e:
    print(f"Warning: Failed to authenticate Slack Client. Ensure SLACK_BOT_TOKEN is set in environment variables. Error: {e}")

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


def check_and_alert(feed_url, conn):
    processed_ids = get_last_processed_guid_from_db(conn, feed_url)
    feed = parse_feed(feed_url)
    # An empty feed is not an exception. Say so, or a dead source looks identical
    # to a quiet one -- which is how the SEC feeds hid for as long as they did.
    if getattr(feed, "status", 200) >= 400 or not feed.entries:
        print(f"EMPTY feed (http={getattr(feed, 'status', '?')}): {feed_url}")
    keyword_pattern = build_keyword_pattern(get_keywords())
    new_items = []

    for entry in feed.entries:
        if not entry.get('id') or entry.get('id') == '':
            identifier = f"{entry.get('link', '')}{entry.get('title', '')}"
            entry.id = hashlib.sha256(identifier.encode()).hexdigest()

        # 1. STATE CHECK: Skip if already processed
        if str(entry.get('id')) in processed_ids:
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


    if new_items:
        print(f"Found {len(new_items)} matching items.")
        
        # 3. ALERTING - Process each match
        for item in new_items:
            message = f"🚨 NEW ALERT! Title: {item['title']}\nLink: {item['link']}"
            
            # Send alert (Prints if LOCAL_TESTING env var is set)
            if os.environ.get("LOCAL_TESTING") == "True":
                print(f"🔌 [TEST MODE MOCK]: {message}")
            else:
                slack_client.chat_postMessage(text=message, channel="#newshound-feed")
                time.sleep(10)

        # Update database with all newly found items
        for item in new_items:
            update_last_processed_guid_in_db(conn, feed_url, item['id'])

def get_rss_feeds():
    # utf-8-sig strips a real BOM; the old lstrip('ufeff') only stripped the
    # literal characters u, f and e from the start of the URL.
    with open("rss.md", encoding='utf-8-sig') as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line]


if __name__ == "__main__":
    conn = initialize_db()
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
                check_and_alert(feed_url, conn)
            except Exception as exc:
                # Measured 2026-09-17: the two feeds that time out in production
                # (q=htx, q=yuchen+sun) answer in 0.3-0.4s when asked directly, so
                # a 30s read timeout is not a ceiling set too low -- it is an
                # intermittent stall on a fast server. Raising the ceiling would
                # only make a bad cycle slower. One retry is the mitigation that
                # matches the fault; if it fails twice, record it and move on.
                time.sleep(2)
                try:
                    check_and_alert(feed_url, conn)
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
