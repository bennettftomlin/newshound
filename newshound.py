# simple script to check RSS feeds for updates, filter to specific terms, and send slack message when filter is met
import feedparser
from slack_sdk import WebClient
import os
from io import StringIO
import sqlite3

DB_PATH = "last_processed.db"

DB_PATH = "last_processed.db"

def initialize_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Use feed_source as the unique identifier for the feed (the URL)
    # State now tracks key: feed_url, value: last_guid
    cursor.execute("CREATE TABLE IF NOT EXISTS state (feed_source TEXT PRIMARY KEY, last_guid TEXT)")
    conn.commit()
    return conn

def get_last_processed_guid_from_db(conn, feed_url):
    """Retrieves the last processed GUID for a specific feed URL."""
    cursor = conn.cursor()
    try:
        # Key is now scoped by feed source (URL)
        cursor.execute("SELECT value FROM state WHERE feed_source = ?", (feed_url,))
        result = cursor.fetchone()
        return result[0] if result and result[0] else None
    except sqlite3.OperationalError:
        return None

def update_last_processed_guid_in_db(conn, feed_url, guid):
    """Updates the last processed GUID for a specific feed URL."""
    cursor = conn.cursor()
    # Use REPLACE INTO with both source and value
    cursor.execute("""INSERT OR REPLACE INTO state (feed_source, last_guid) VALUES (?, ?)""", (feed_url, str(guid)))
    conn.commit()

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
# Example of using the client for initialization purposes (assuming token is set)
try:
    slack_client.auth_test() 
except Exception as e:
    print(f"Warning: Failed to authenticate Slack Client. Ensure SLACK_BOT_TOKEN is set in environment variables. Error: {e}")

def get_keywords():
    with open("keywords.md", encoding='UTF-8') as f:
        lines = [line.lstrip('ufeff').rstrip() for line in f]
    return lines

def check_and_alert(feed_url, conn):
    last_processed_guid = get_last_processed_guid_from_db(conn)
    feed = feedparser.parse(feed_url)
    new_items = []
    
    for entry in feed.entries:
        # 1. STATE CHECK: Skip if already processed
        if str(entry.get('id')) == last_processed_guid:
            continue

        # 2. FILTERING: Check the requirement
        content = str(entry.get('summary', '')).lower()
        for keyword in get_keywords():
            if keyword in content:
                new_items.append(entry)
        
    if new_items:
        print(f"Found {len(new_items)} matching items.")
        
        # 3. ALERTING - Process each match
        for item in new_items:
            message = f"🚨 NEW CRYPTO ALERT! Title: {item['title']}\nLink: {item['link']}"
            
            # Send to Slack
            slack_client.chat_postMessage(text=message) 

    if new_items and new_items[-1].get('id'):
        # 4. STATE UPDATE
        update_last_processed_guid_in_db(conn, new_items[-1]['id'])

def get_rss_feeds():
    with open("rss.md", encoding='UTF-8') as f:
        lines = [line.lstrip('ufeff').rstrip() for line in f]
    return lines


if __name__ == "__main__":
    for x in get_rss_feeds():
        check_and_alert(x)
