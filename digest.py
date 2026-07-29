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
            (
                "Decision: "
                f"{record.get('disposition') or 'unknown'} "
                f"({(record.get('decision_reason') or {}).get('stage') or 'unknown'}/"
                f"{(record.get('decision_reason') or {}).get('code') or 'unknown'})"
            ),
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


_SLACK_STATUS_EMOJI = {"proposed": ":large_green_circle:", "flagged_for_review": ":warning:"}


def _slack_technical_summary(verdict):
    """The full technical_verdict string includes a per-timeframe breakdown dict --
    useful in the full-detail email, too dense for a Slack line. Keep the verdict,
    score, confluence, family breakdown, and RSI note; drop the per-timeframe tail."""
    if not verdict:
        return verdict
    return verdict.split("; per-timeframe")[0]


def format_symbol_section_slack(record):
    emoji = _SLACK_STATUS_EMOJI.get(record["status"], "")
    return "\n".join(
        [
            f"{emoji} *{record['symbol']}* ({record.get('company_name') or 'unknown'}) — _{record['status']}_",
            (
                f"*Decision:* {record.get('disposition') or 'unknown'} "
                f"(`{(record.get('decision_reason') or {}).get('stage') or 'unknown'}/"
                f"{(record.get('decision_reason') or {}).get('code') or 'unknown'}`)"
            ),
            f"*Technical:* {_slack_technical_summary(record.get('technical_verdict'))}",
            f"*Fundamental:* {record.get('fundamental_verdict')}",
            f"*Risk:* {record.get('risk_verdict')}",
            f"*Sentiment:* {record.get('sentiment_verdict')}",
            f">{record.get('proposal')}",
        ]
    )


def _summarize(records):
    by_status = defaultdict(list)
    for record in records:
        by_status[record["status"]].append(record)
    return (
        by_status.get("proposed", []),
        by_status.get("flagged_for_review", []),
        len(by_status.get("aborted", [])),
        len(records),
    )


def build_digest(run_id):
    proposed, flagged, aborted_count, total = _summarize(_load_records(run_id))

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


def build_slack_digest(run_id):
    proposed, flagged, aborted_count, total = _summarize(_load_records(run_id))

    header = (
        f":bar_chart: *NSE Overnight Scan Digest* — {now_ist():%Y-%m-%d}\n"
        f"Scanned *{total}* symbols: *{len(proposed)}* proposed, *{len(flagged)}* flagged, {aborted_count} aborted."
    )

    if not proposed and not flagged:
        return header

    sections = [format_symbol_section_slack(r) for r in proposed + flagged]
    return header + "\n\n" + "\n\n".join(sections)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run digest.py <run_id>")
        sys.exit(1)

    run_id = sys.argv[1]
    subject, body = build_digest(run_id)
    send_email(subject, body)
    send_slack(build_slack_digest(run_id))
