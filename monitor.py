import argparse
import logging
import os
import smtplib
import socket
import sqlite3
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import yaml
from dotenv import load_dotenv

CONFIG_PATH = Path(__file__).parent / "config.yaml"
FEED_TIMEOUT_SECONDS = 15
# Some wires (e.g. Nasdaq/Akamai) hang until timeout on feedparser's default
# User-Agent but respond immediately to a browser-like one.
FEED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

load_dotenv(Path(__file__).parent / ".env")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    email_cfg = config.setdefault("email", {})
    email_cfg["username"] = os.environ.get("GMAIL_USERNAME", email_cfg.get("username", ""))
    email_cfg["app_password"] = os.environ.get("GMAIL_APP_PASSWORD", email_cfg.get("app_password", ""))
    email_cfg["to"] = os.environ.get("GMAIL_TO", email_cfg.get("to", ""))

    # SEC EDGAR requires a self-identifying User-Agent with a real contact
    # (https://www.sec.gov/os/webmaster-faq#developers) or it 403s - kept out
    # of the committed config.yaml so no personal email lands in the repo.
    sec_contact = os.environ.get("SEC_EDGAR_CONTACT", "")
    for feed in config.get("feeds", []):
        if isinstance(feed, dict) and feed.get("user_agent") == "${SEC_EDGAR_CONTACT}":
            feed["user_agent"] = sec_contact or FEED_USER_AGENT

    return config


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen (guid TEXT PRIMARY KEY, seen_at TEXT)"
    )
    conn.commit()
    return conn


def already_seen(conn, guid):
    row = conn.execute("SELECT 1 FROM seen WHERE guid = ?", (guid,)).fetchone()
    return row is not None


def mark_seen(conn, guid):
    conn.execute(
        "INSERT OR IGNORE INTO seen (guid, seen_at) VALUES (?, datetime('now'))",
        (guid,),
    )
    conn.commit()


def find_matches(text, watchlist, global_keywords):
    text_lower = text.lower()
    matched = []
    for term in watchlist:
        if term.lower() in text_lower:
            matched.append(term)
    for term in global_keywords:
        if term.lower() in text_lower:
            matched.append(term)
    return matched


def send_email(config, subject, body):
    email_cfg = config["email"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg["username"]
    msg["To"] = email_cfg["to"]

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["username"], email_cfg["app_password"])
        server.send_message(msg)


def poll_once(config, conn, logger):
    watchlist = config.get("watchlist", [])
    global_keywords = config.get("global_keywords", [])
    total_new = 0
    total_matches = 0

    for feed in config["feeds"]:
        if isinstance(feed, dict):
            feed_url = feed["url"]
            agent = feed.get("user_agent", FEED_USER_AGENT)
        else:
            feed_url = feed
            agent = FEED_USER_AGENT

        try:
            parsed = feedparser.parse(feed_url, agent=agent)
        except Exception:
            logger.exception("Failed to fetch feed %s", feed_url)
            continue

        if parsed.bozo and not parsed.entries:
            logger.warning("Failed to fetch/parse feed %s: %s", feed_url, parsed.bozo_exception)
            continue

        new_in_feed = 0
        for entry in parsed.entries:
            guid = entry.get("id") or entry.get("link")
            if not guid or already_seen(conn, guid):
                continue

            mark_seen(conn, guid)
            new_in_feed += 1
            total_new += 1

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            published = entry.get("published", "")

            matched = find_matches(f"{title} {summary}", watchlist, global_keywords)
            if matched:
                total_matches += 1
                subject = f"[PR Alert] {matched[0]} - {title}"
                body = (
                    f"Matched: {', '.join(matched)}\n"
                    f"Headline: {title}\n"
                    f"Source: {feed_url}\n"
                    f"Published: {published}\n"
                    f"Link: {link}\n"
                )
                try:
                    send_email(config, subject, body)
                    logger.info("Alert sent for: %s (matched: %s)", title, matched)
                except Exception:
                    logger.exception("Failed to send email for: %s", title)

        logger.info("Feed %s: %d new entries", feed_url, new_in_feed)

    logger.info("Poll complete: %d new entries, %d matches", total_new, total_matches)


def setup_logging(log_file):
    logger = logging.getLogger("press_release_monitor")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # GitHub Actions runners are ephemeral - a log file there is just discarded, and
    # the console output already goes to the Actions run log.
    if not os.environ.get("GITHUB_ACTIONS"):
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def main():
    socket.setdefaulttimeout(FEED_TIMEOUT_SECONDS)

    parser = argparse.ArgumentParser(description="Press release wire monitor")
    parser.add_argument("--once", action="store_true", help="Poll feeds once and exit")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit")
    args = parser.parse_args()

    config = load_config()
    logger = setup_logging(Path(__file__).parent / config.get("log_file", "monitor.log"))

    if args.test_email:
        send_email(
            config,
            subject="[PR Alert] Test email",
            body="This is a test email from press_release_monitor.",
        )
        logger.info("Test email sent to %s", config["email"]["to"])
        return

    db_path = Path(__file__).parent / config.get("state_db", "state.db")
    conn = init_db(db_path)

    if args.once:
        poll_once(config, conn, logger)
        return

    interval = config.get("poll_interval_seconds", 60)
    logger.info("Starting monitor loop (interval=%ds)", interval)
    while True:
        try:
            poll_once(config, conn, logger)
        except Exception:
            logger.exception("Error during poll cycle")
        time.sleep(interval)


if __name__ == "__main__":
    main()
