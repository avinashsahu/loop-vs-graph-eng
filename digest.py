from dotenv import load_dotenv

# Must run before `notify` is imported below -- it reads EMAIL_ENABLED/SMTP_*/etc at
# module level, and nothing else in this script's import chain was loading .env before.
load_dotenv()

import json
import os
import sys
from collections import defaultdict

from logging_config import setup_logging
from market_time import now_ist
from notify import send_email, send_slack

# Read directly rather than `from nse_trade_graph import TRADE_LOG_PATH` -- that import
# would execute nse_trade_graph's module-level code (including its own NSE_SCAN_LABEL
# read) immediately, which matters for intraday_recheck.py importing this module before
# it sets NSE_SCAN_LABEL for its own run.
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")

log = setup_logging("digest")


def read_jsonl_records(path):
    """All of trade_log.jsonl's readers (this module and intraday_recheck.py) go
    through here -- one truncated/corrupt line (e.g. a partial write on disk-full, or
    two processes appending at once) shouldn't crash the whole digest/recheck."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                log.warning("skipping malformed line %d in %s", i + 1, path)
    return records


def _load_records(run_id):
    return [r for r in read_jsonl_records(TRADE_LOG_PATH) if r.get("scan_label") == run_id]


def _format_indicators(indicators):
    if not indicators:
        return "  (no indicators)"
    return "\n".join(
        f"  {tf}: close={ind['close']} SMA20={ind['sma20']} SMA50={ind['sma50']} "
        f"RSI14={ind['rsi14']} MACD={ind['macd']} MACD_signal={ind['macd_signal']} MACD_hist={ind['macd_hist']}"
        for tf, ind in indicators.items()
    )


def format_symbol_section(record):
    return "\n".join(
        [
            f"=== {record['symbol']} ({record.get('company_name') or 'unknown'}) -- {record['status']} ===",
            f"Timestamp: {record['timestamp']}",
            "",
            "Technical indicators:",
            _format_indicators(record.get("technical_indicators")),
            f"Technical verdict: {record.get('technical_verdict')}",
            f"Fundamental verdict: {record.get('fundamental_verdict')}",
            f"Risk verdict: {record.get('risk_verdict')}",
            f"Sentiment verdict: {record.get('sentiment_verdict')}",
            "",
            f"Proposal: {record.get('proposal')}",
        ]
    )


def build_digest(run_id):
    records = _load_records(run_id)
    by_status = defaultdict(list)
    for record in records:
        by_status[record["status"]].append(record)

    proposed = by_status.get("proposed", [])
    flagged = by_status.get("flagged_for_review", [])
    aborted_count = len(by_status.get("aborted", []))
    total = len(records)

    subject = (
        f"NSE Overnight Scan Digest -- {now_ist():%Y-%m-%d} -- "
        f"{len(proposed)} proposed, {len(flagged)} flagged ({total} scanned)"
    )

    if not proposed and not flagged:
        body = f"Scanned {total} symbols ({aborted_count} aborted). Nothing proposed or flagged this run."
    else:
        sections = [format_symbol_section(r) for r in proposed + flagged]
        body = (
            f"Scanned {total} symbols: {len(proposed)} proposed, {len(flagged)} flagged, "
            f"{aborted_count} aborted.\n\n" + "\n\n".join(sections)
        )

    return subject, body


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run digest.py <run_id>")
        sys.exit(1)

    subject, body = build_digest(sys.argv[1])
    send_email(subject, body)
    send_slack(subject, body)
