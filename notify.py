import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage

from logging_config import setup_logging

log = setup_logging("notify")

EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED") == "1"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1") == "1"
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")

SLACK_ENABLED = os.environ.get("SLACK_ENABLED") == "1"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
# Slack renders a message past this length behind a "show more" fold -- split on
# section boundaries instead of sending one huge wall of text.
_SLACK_CHUNK_CHARS = 3500


def get_recipients() -> list[str]:
    return [addr.strip() for addr in os.environ.get("EMAIL_TO", "").split(",") if addr.strip()]


def send_email(subject: str, body: str, recipients: list[str] = None) -> None:
    """Send a plain-text email. Stub by default -- set EMAIL_ENABLED=1 to actually send.

    With EMAIL_ENABLED=0 (default), logs the composed subject/body instead, so callers
    are testable without real SMTP credentials.
    """
    recipients = recipients if recipients is not None else get_recipients()

    if not EMAIL_ENABLED:
        log.info("EMAIL_ENABLED=0, not sending. subject=%r to=%s\n%s", subject, recipients, body)
        return

    if not recipients:
        log.warning("send_email called with no recipients, skipping. subject=%r", subject)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(msg)

    log.info("sent email subject=%r to=%s", subject, recipients)


def _slack_chunks(text: str) -> list[str]:
    """Split on blank-line boundaries (callers put one between records/sections), so a
    chunk break never lands mid-record."""
    sections = text.split("\n\n")
    chunks = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) > _SLACK_CHUNK_CHARS and current:
            chunks.append(current)
            current = section
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_slack(text: str) -> None:
    """Post a message to Slack via an incoming webhook. Stub by default -- set
    SLACK_ENABLED=1 and SLACK_WEBHOOK_URL to actually send.

    `text` is posted as-is (Slack mrkdwn) -- callers build their own header/formatting,
    since a webhook post has no separate subject line the way email does. Long text is
    split into multiple messages on record boundaries, each one valid mrkdwn on its own
    (a chunk break never lands inside a *bold* or ```code``` span).
    """
    if not SLACK_ENABLED:
        log.info("SLACK_ENABLED=0, not sending.\n%s", text)
        return

    if not SLACK_WEBHOOK_URL:
        log.warning("send_slack called with no SLACK_WEBHOOK_URL, skipping.")
        return

    chunks = _slack_chunks(text)
    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk = f"_(cont. {i + 1}/{len(chunks)})_\n{chunk}"
        payload = json.dumps({"text": chunk}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    log.info("sent slack message in %d chunk(s)", len(chunks))
