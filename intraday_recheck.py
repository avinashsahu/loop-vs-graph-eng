from dotenv import load_dotenv

# Must run before `digest`/`notify` are imported below -- they read env-configured
# constants (EMAIL_ENABLED/SMTP_*/etc) at module level, and nothing else in this
# script's import chain was loading .env before.
load_dotenv()

import os
import sys
from functools import partial

import notify
from alert_ledger import (
    AlertLedger,
    AlertLedgerStateError,
    build_decision_fingerprint,
)
from digest import (
    format_symbol_section,
    format_symbol_section_slack,
    read_jsonl_records,
)
from logging_config import setup_logging
from market_time import is_market_hours, now_ist

log = setup_logging("intraday_recheck")

# Read directly, not `from nse_trade_graph import TRADE_LOG_PATH` -- importing
# nse_trade_graph here would run its module-level NSE_SCAN_LABEL read before this
# script sets its own label below, and sys.modules caching would make the later
# `import nse_trade_graph` a no-op re-execution-wise.
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
INTRADAY_ALERT_STATE_PATH = os.environ.get(
    "INTRADAY_ALERT_STATE_PATH",
    ".intraday_alert_state.json",
)


def _find_latest_overnight_label():
    # "overnight_YYYYMMDD_HHMM" sorts lexicographically == chronologically for this format.
    labels = {r.get("scan_label", "") for r in read_jsonl_records(TRADE_LOG_PATH)}
    overnight_labels = [label for label in labels if label.startswith("overnight_")]
    return max(overnight_labels) if overnight_labels else None


def _load_pick_symbols(run_id):
    symbols = []
    seen = set()
    for record in read_jsonl_records(TRADE_LOG_PATH):
        if (
            record.get("scan_label") == run_id
            and record["status"] in ("proposed", "flagged_for_review")
            and record["symbol"] not in seen
        ):
            seen.add(record["symbol"])
            symbols.append(record["symbol"])
    return symbols


if __name__ == "__main__":
    # Self-gate on IST market hours rather than relying on the cron schedule/host
    # timezone to line up -- makes this safe to run on any machine, any system timezone,
    # even a broad "every 15 min, every day" cron entry. Override for manual testing.
    if not is_market_hours() and os.environ.get("NSE_SKIP_MARKET_HOURS_CHECK") != "1":
        print(f"Outside NSE market hours (now={now_ist().isoformat()}), skipping.")
        sys.exit(0)

    run_id = sys.argv[1] if len(sys.argv) > 1 else _find_latest_overnight_label()
    if not run_id:
        print("No overnight scan_label found in trade_log.jsonl -- nothing to recheck.")
        sys.exit(0)

    symbols = _load_pick_symbols(run_id)
    if not symbols:
        print(f"No proposed/flagged symbols found for scan_label={run_id!r}.")
        sys.exit(0)

    # Set before importing nse_trade_graph -- its NSE_SCAN_LABEL constant is read once at
    # import time, same pattern every other config value in that module already uses.
    os.environ["NSE_SCAN_LABEL"] = f"intraday_{now_ist():%Y%m%d_%H%M%S}"
    import nse_trade_graph

    principal = float(os.environ.get("NSE_PRINCIPAL", "100000"))
    max_allocation_pct = float(
        os.environ.get(
            "NSE_MAX_ALLOCATION_PCT",
            os.environ.get("NSE_RISK_PCT", "10"),
        )
    )
    max_loss_pct = float(os.environ.get("NSE_MAX_LOSS_PCT", "1"))
    atr_stop_multiple = float(os.environ.get("NSE_ATR_STOP_MULTIPLE", "2"))
    reward_risk_ratio = float(os.environ.get("NSE_REWARD_RISK_RATIO", "2"))
    alert_ledger = AlertLedger(INTRADAY_ALERT_STATE_PATH)

    for symbol in symbols:
        try:
            final_state = nse_trade_graph.run(
                symbol,
                principal,
                max_allocation_pct,
                max_loss_pct,
                atr_stop_multiple,
                reward_risk_ratio,
            )
        except Exception:
            # nsemine can return None (not raise) for illiquid/no-data symbols, which
            # crashes deeper in the fetch chain -- one bad symbol shouldn't cost every
            # remaining alert in this run, same as the batch scanner's own per-symbol
            # try/except in nse_trade_graph.py's __main__.
            log.warning("recheck failed for %s, continuing", symbol, exc_info=True)
            continue

        status = final_state["status"]
        # The fingerprint excludes timestamps and prose, so only disposition, material
        # plan fields, or typed decision reasons create a new alert transition.
        record = nse_trade_graph.build_record(final_state)
        fingerprint = build_decision_fingerprint(record)
        channels = {}
        if status in ("proposed", "flagged_for_review"):
            # Same builder node_log already used internally (run() logs as part of the graph
            # itself) -- guarantees alerts contain exactly what's in trade_log.jsonl.
            subject = f"NSE Intraday Alert -- {symbol} -- {status}"
            if notify.EMAIL_ENABLED and notify.get_recipients():
                channels["email"] = partial(
                    notify.send_email,
                    subject,
                    format_symbol_section(record),
                )
            if notify.SLACK_ENABLED and notify.SLACK_WEBHOOK_URL:
                slack_header = ":rotating_light: *NSE Intraday Alert*\n"
                channels["slack"] = partial(
                    notify.send_slack,
                    slack_header + format_symbol_section_slack(record),
                )

        try:
            outcomes = alert_ledger.observe_and_deliver(
                run_id=run_id,
                symbol=symbol,
                status=status,
                fingerprint=fingerprint,
                channels=channels,
            )
        except AlertLedgerStateError:
            log.exception(
                "alert ledger failed closed for %s; no notification attempted",
                symbol,
            )
            continue
        except Exception:
            log.exception(
                "alert delivery state uncertain for %s; a channel may have been sent and retried",
                symbol,
            )
            continue
        for channel, outcome in outcomes.items():
            if outcome.startswith("failed:"):
                log.warning(
                    "intraday alert failed symbol=%s channel=%s outcome=%s",
                    symbol,
                    channel,
                    outcome,
                )
