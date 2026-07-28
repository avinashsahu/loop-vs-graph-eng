import json
import os
import sys
from datetime import datetime

from digest import format_symbol_section
from notify import send_email

# Read directly, not `from nse_trade_graph import TRADE_LOG_PATH` -- importing
# nse_trade_graph here would run its module-level NSE_SCAN_LABEL read before this
# script sets its own label below, and sys.modules caching would make the later
# `import nse_trade_graph` a no-op re-execution-wise.
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")


def _find_latest_overnight_label():
    if not os.path.exists(TRADE_LOG_PATH):
        return None
    # "overnight_YYYYMMDD_HHMM" sorts lexicographically == chronologically for this format.
    with open(TRADE_LOG_PATH) as f:
        labels = {json.loads(line).get("scan_label", "") for line in f}
    overnight_labels = [label for label in labels if label.startswith("overnight_")]
    return max(overnight_labels) if overnight_labels else None


def _load_pick_symbols(run_id):
    symbols = []
    seen = set()
    with open(TRADE_LOG_PATH) as f:
        for line in f:
            record = json.loads(line)
            if record.get("scan_label") == run_id and record["status"] in ("proposed", "flagged_for_review"):
                if record["symbol"] not in seen:
                    seen.add(record["symbol"])
                    symbols.append(record["symbol"])
    return symbols


if __name__ == "__main__":
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
    os.environ["NSE_SCAN_LABEL"] = f"intraday_{datetime.now():%Y%m%d_%H%M%S}"
    import nse_trade_graph

    principal = float(os.environ.get("NSE_PRINCIPAL", "100000"))
    risk_pct = float(os.environ.get("NSE_RISK_PCT", "10"))

    for symbol in symbols:
        final_state = nse_trade_graph.run(symbol, principal, risk_pct)
        if final_state["status"] not in ("proposed", "flagged_for_review"):
            continue  # dropped to aborted since the overnight scan -- no longer actionable

        record = {
            "symbol": symbol,
            "company_name": (final_state.get("quote") or {}).get("name"),
            "status": final_state["status"],
            "timestamp": datetime.now().isoformat(),
            "technical_indicators": final_state.get("technical_indicators"),
            "technical_verdict": final_state.get("technical_verdict"),
            "fundamental_verdict": final_state.get("fundamental_verdict"),
            "risk_verdict": final_state.get("risk_verdict"),
            "sentiment_verdict": final_state.get("sentiment_verdict"),
            "proposal": final_state.get("proposal"),
        }
        subject = f"NSE Intraday Alert -- {symbol} -- {final_state['status']}"
        send_email(subject, format_symbol_section(record))
