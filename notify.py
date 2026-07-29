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


def _slack_chunks(body: str) -> list[str]:
    """Split on the blank-line boundaries format_symbol_section already puts between
    records, so a chunk break never lands mid-record."""
    sections = body.split("\n\n")
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


def send_slack(subject: str, body: str) -> None:
    """Post a digest to Slack via an incoming webhook. Stub by default -- set
    SLACK_ENABLED=1 and SLACK_WEBHOOK_URL to actually send.

    Long bodies are split into multiple messages on record boundaries -- same
    "stub by default, testable without real credentials" pattern as send_email.
    """
    if not SLACK_ENABLED:
        log.info("SLACK_ENABLED=0, not sending. subject=%r\n%s", subject, body)
        return

    if not SLACK_WEBHOOK_URL:
        log.warning("send_slack called with no SLACK_WEBHOOK_URL, skipping. subject=%r", subject)
        return

    chunks = _slack_chunks(body)
    for i, chunk in enumerate(chunks):
        header = f"*{subject}*" if i == 0 else f"*{subject}* (cont. {i + 1}/{len(chunks)})"
        # Code-block wrapping keeps the fixed-width verdict/indicator lines aligned --
        # Slack's mrkdwn doesn't otherwise preserve the log-style formatting.
        text = f"{header}\n```{chunk}```"
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    log.info("sent slack message subject=%r in %d chunk(s)", subject, len(chunks))
