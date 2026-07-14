# Agent Guide - newshound
This repository processes RSS feeds to alert on specific keywords via Slack.

## 🧠 General Workflow Notes
*   **Core Components:** The main logic resides in `newshound.py`.
*   **Deployment Target:** Designed for stateless, serverless execution (e.g., Cloud Functions). State persistence is managed using a local SQLite database (`last_processed.db`).
*   **Workflow Quirk:** RSS feeds are loaded from an external configuration file (`rss.md`), and API credentials/keywords are stored in environment variables (e.g., `SLACK_BOT_TOKEN`, or updated constants).

## 🛠️ Operations & Commands
The core functionality is handled by executing `python newshound.py`.

*   **`python newshound.py`**: Initializes the database, reads all RSS feeds from `rss.md`, checks each feed for un-processed items containing specified keywords ("crypto"), alerts Slack if matches are found, and updates the state individually for each successful feed source.

## ✨ Known Assumptions / Missing Context
This file will be updated when the following details are confirmed:
*   Specific RSS Feed source/loading method (e.g., from a structured config file).
*   Exact Slack Webhook URL loading location.
*   The precise command/workflow for testing or local simulation of feed changes.