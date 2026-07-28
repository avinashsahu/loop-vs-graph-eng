import os
import smtplib
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
