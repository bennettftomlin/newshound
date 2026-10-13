# Agent Guide - newshound
This repository processes RSS feeds to alert on specific keywords via Slack.

## 🧠 General Workflow Notes
*   **Core Components:** The main logic resides in `newshound.py`.
*   **Core Components:** The main logic resides in `newshound.py`. Modules include `feedparser` for fetching and `sqlite3` for persistence.
*   **State Management (CRITICAL):** The service is **stateful**. It uses a local SQLite database (`last_processed.db`) to track processed items, scoped uniquely by the source feed URL in the `processed_items` table. This ensures that processing status is maintained independently for *each* configured RSS feed.
*   **Execution Flow:** The process runs sequentially: 1) Initializes DB. 2) Fetches all feeds from `rss.md`. 3) Checks each feed's unique items against the database state and global keywords, alerting on new matches before committing updates to the state for that specific feed source.
*   **Operational Command:** Running `python newshound.py` handles this entire cycle: initializing DB -> checking all configured feeds (statefully) -> sending alerts if matching content is found -> marking *all* unique matched items as processed in the database, per feed source.

## 🛠️ Operations & Commands
The core functionality is handled by executing `python newshound.py`.

*   **`python newshound.py`**: Initializes the database, reads all RSS feeds from `rss.md`, checks each feed for un-processed items containing specified keywords (from keywords.md), alerts Slack if matches are found, and updates the state individually for each successful feed source.

## ✨ Known Assumptions / Missing Context
This file will be updated when the following details are confirmed:
*   Specific RSS Feed source/loading method (e.g., from a structured config file).
*   Exact Slack Webhook URL loading location.
*   The precise command/workflow for testing or local simulation of feed changes.\n\n## 🧠 Internal Tools\nUse the memory tool to maintain context; prioritize consulting it at the start and end of every session.

## SEC EDGAR latest filings (edgar_feed.py, 2026-10-12)
*   **Off by default since 2026-10-13** (it alerted too often): the entry point runs it only when `NEWSHOUND_EDGAR=1` is in the environment. Filings still reach the morning gather through its EDGAR full-text search.
*   When on, read first in every run: pages EDGAR's getcurrent Atom feed down to the high-water mark in `edgar_state` (at most `MAX_PAGES`; more prints `EDGAR CAP HIT`), through the shared transport with the declared research User-Agent, paced in edgar_io's cross-process SEC pace table. The first run sets the mark and posts nothing.
*   Gates: a watched CIK as any party (every CIK the KB types in derived/graph.sqlite, plus `edgar_watch.md`) or a company-name keyword (`edgar_keywords.md`). Priority forms post one by one; the rest go in one "routine" batch per run.
*   Dedupe: `sec:<accession>` in `sent_keys` (newshound's `canonical_key` maps any sec.gov/Archives link to it). Counts go to `runs.jsonl` under `edgar`. Tests: `tests/test_edgar_feed.py` (offline; one real page in `tests/fixtures/`).
