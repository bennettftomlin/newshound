# simple script to check RSS feeds for updates, filter to specific terms, and send slack message when filter is met
import feedparser
from slack_sdk import WebClient
import os
from io import StringIO
import sqlite3
import time
import hashlib

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
    with open("keywords.md", encoding='UTF-8') as f:
        lines = [line.lstrip().rstrip() for line in f]
    return lines

def check_and_alert(feed_url, conn):
    processed_ids = get_last_processed_guid_from_db(conn, feed_url)
    feed = feedparser.parse(feed_url)
    new_items = []

    for entry in feed.entries:
        if not entry.get('id') or entry.get('id') == '':
            identifier = f"{entry.get('link', '')}{entry.get('title', '')}"
            entry.id = hashlib.sha256(identifier.encode()).hexdigest()

        # 1. STATE CHECK: Skip if already processed
        if str(entry.get('id')) in processed_ids:
            continue

        # 2. FILTERING: Check the requirement
        content = str(entry.get('summary', '')).lower()
        for keyword in get_keywords():
            if keyword in content:
                new_items.append(entry)
                break
        
    if new_items:
        print(f"Found {len(new_items)} matching items.")
        
        # 3. ALERTING - Process each match
        for item in new_items:
            message = f"🚨 NEW CRYPTO ALERT! Title: {item['title']}\nLink: {item['link']}"
            
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
    with open("rss.md", encoding='UTF-8') as f:
        lines = [line.lstrip('ufeff').rstrip() for line in f]
        print(lines)
    return lines


if __name__ == "__main__":
    for x in get_rss_feeds():
        check_and_alert(x, initialize_db())
