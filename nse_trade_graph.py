from dotenv import load_dotenv

# Must run before any of this project's own modules are imported below -- several of
# them (fundamentals, nse_data, cache) read env-configured constants at module level,
# and .env was previously only ever loaded as a side effect of importing llm.py, which
# doesn't reliably happen first. Confirmed live: FUNDAMENTALS_CACHE_TTL_HOURS from .env
# was silently ignored because `import fundamentals` (below) ran before load_dotenv().
load_dotenv()

import json
import os
import time

from nsemine.live import get_stock_live_quotes

import fundamentals
import nse_data
import ta_analysis
from llm import call_llm
from logging_config import setup_logging
from market_time import now_ist

MAX_ITERS = 3
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
NSE_SCAN_LABEL = os.environ.get("NSE_SCAN_LABEL", "manual")

log = setup_logging("nse")


def _verdict_prompt(question):
    return f"{question}\nReply with exactly one word first: GOOD or BAD. Then, on the same line, a short reason."


def node_fetch(state):
    state["iters"] += 1
    state["quote"] = get_stock_live_quotes(state["symbol"])
    state["hist_multi"] = nse_data.get_multi_timeframe_history(state["symbol"])
    state["hist"] = state["hist_multi"]["D"]
    state["fundamental_snapshot"] = fundamentals.get_fundamental_snapshot(state["symbol"])
    return "technical", state


def node_technical(state):
    indicators = {tf: ta_analysis.compute_indicators(hist) for tf, hist in state["hist_multi"].items()}
    state["technical_indicators"] = indicators
    log.info("iter=%d indicators=%s", state["iters"], indicators)

    timeframe_lines = "\n".join(
        f"{tf}: close={ind['close']}, SMA20={ind['sma20']}, SMA50={ind['sma50']}, "
        f"RSI14={ind['rsi14']}, MACD={ind['macd']}, MACD_signal={ind['macd_signal']}, MACD_hist={ind['macd_hist']}"
        for tf, ind in indicators.items()
    )
    verdict = call_llm(
        _verdict_prompt(
            f"Multi-timeframe technical indicators for {state['symbol']} (D=daily, 30/15/5=minutes):\n"
            f"{timeframe_lines}\n"
            "Rules of thumb: SMA20 above SMA50 is a bullish trend; RSI above 70 is overbought "
            "(caution), below 30 is oversold; positive MACD histogram is bullish momentum. "
            "Weight the daily trend most heavily, use the intraday timeframes to confirm timing.\n"
            "Is there a clear short-term uptrend/momentum worth considering a BUY?"
        ),
        mode="check",
    )
    state["technical_verdict"] = verdict
    log.info("iter=%d technical_verdict=%r", state["iters"], verdict)

    if verdict.startswith("GOOD"):
        return "fundamental", state
    return "technical_retry_guard", state


def node_technical_retry_guard(state):
    if state["iters"] >= MAX_ITERS:
        log.warning("technical check never GOOD after MAX_ITERS=%d", MAX_ITERS)
        return "abort", state
    return "fetch", state


def node_fundamental(state):
    snap = state["fundamental_snapshot"] or {}
    eps, pat = snap.get("eps"), snap.get("pat")

    if (isinstance(eps, (int, float)) and eps < 0) or (isinstance(pat, (int, float)) and pat < 0):
        state["fundamental_verdict"] = f"BAD: negative EPS/PAT (eps={eps}, pat={pat})"
        log.warning(state["fundamental_verdict"])
        return "abort", state

    verdict = call_llm(
        _verdict_prompt(
            f"Fundamental snapshot for {state['symbol']} ({snap.get('company_name')}): "
            f"corp actions={snap.get('corp_actions')}, "
            f"shareholding pattern (recent periods)={snap.get('shareholding_pattern')}, "
            f"yearwise returns={snap.get('yearwise_returns')}, "
            f"peer comparison (quarter {snap.get('peer_comparison_quarter')})={snap.get('peer_comparison')}.\n"
            "Does the company look fundamentally sound -- no red flags in recent corporate actions "
            "or shareholding trend, and reasonable standing versus peers?"
        ),
        mode="check",
    )
    state["fundamental_verdict"] = verdict
    log.info("fundamental_verdict=%r", verdict)

    if verdict.startswith("GOOD"):
        return "risk", state
    return "flag_review", state


def node_risk(state):
    quote = state["quote"]
    # nsemine's upper_circuit/lower_circuit fields are swapped — correct on read.
    lower_circuit = quote["upper_circuit"]
    upper_circuit = quote["lower_circuit"]

    if not (0 < state["risk_pct"] <= 25):
        state["risk_verdict"] = f"BAD: risk_pct={state['risk_pct']} outside sane 0-25% bound"
        log.warning(state["risk_verdict"])
        return "abort", state

    day_low = state["hist"].iloc[-1]["low"]
    if day_low <= lower_circuit * 1.02:
        state["risk_verdict"] = f"BAD: price near lower circuit ({day_low} vs {lower_circuit})"
        log.warning(state["risk_verdict"])
        return "abort", state

    # quote["open"] is the session's opening print, fixed all day -- not what you'd
    # actually pay. get_stock_live_quotes exposes no live/last-price field at all, so
    # reconstruct it: previous_close + change (verified against real data: matches the
    # actual last-traded price seen elsewhere in the same quote response).
    price = quote["previous_close"] + quote["change"]
    if not price or price <= 0:
        state["risk_verdict"] = f"BAD: no usable price for sizing (price={price})"
        log.warning(state["risk_verdict"])
        return "abort", state

    position_size = state["principal"] * (state["risk_pct"] / 100)
    state["position_size"] = position_size
    state["max_shares"] = int(position_size // price)
    state["risk_verdict"] = (
        f"GOOD: {state['risk_pct']}% of principal={state['principal']}, "
        f"lower_circuit={lower_circuit}, upper_circuit={upper_circuit}"
    )
    log.info(state["risk_verdict"])
    return "sentiment", state


def node_sentiment(state):
    quote = state["quote"]
    verdict = call_llm(
        _verdict_prompt(
            f"Stock {state['symbol']} ({quote['name']}) is {quote['changepct']}% today, sector {quote['sector']}. "
            "Does this look like a reasonable entry point (not a crash, not an extreme spike)?"
        ),
        mode="check",
    )
    state["sentiment_verdict"] = verdict
    log.info("sentiment_verdict=%r", verdict)

    if verdict.startswith("GOOD"):
        return "propose", state
    return "flag_review", state


def node_propose(state):
    state["status"] = "proposed"
    state["proposal"] = (
        f"PROPOSAL (not executed): BUY {state['symbol']} — up to {state['max_shares']} shares "
        f"(~₹{state['position_size']:.0f}, {state['risk_pct']}% of ₹{state['principal']:.0f} principal). "
        "Confirm manually before placing any order."
    )
    return "log", state


def node_flag_review(state):
    state["status"] = "flagged_for_review"
    # fundamental and sentiment are the only two checks that route here; whichever one
    # actually ran and came back BAD is the one worth surfacing.
    if state.get("sentiment_verdict"):
        check, verdict = "sentiment", state["sentiment_verdict"]
    else:
        check, verdict = "fundamental", state["fundamental_verdict"]
    state["proposal"] = f"FLAGGED FOR MANUAL REVIEW: {check} check failed for {state['symbol']} — {verdict!r}"
    return "log", state


def node_abort(state):
    state["status"] = "aborted"
    state["proposal"] = None
    return "log", state


def node_log(state):
    record = {
        "timestamp": now_ist().isoformat(),
        "scan_label": NSE_SCAN_LABEL,
        "symbol": state["symbol"],
        "company_name": (state.get("quote") or {}).get("name"),
        "principal": state["principal"],
        "risk_pct": state["risk_pct"],
        "iters": state["iters"],
        "technical_indicators": state.get("technical_indicators"),
        "technical_verdict": state.get("technical_verdict"),
        "fundamental_verdict": state.get("fundamental_verdict"),
        "risk_verdict": state.get("risk_verdict"),
        "sentiment_verdict": state.get("sentiment_verdict"),
        "status": state["status"],
        "proposal": state["proposal"],
    }
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    log.info("status=%s proposal=%r", state["status"], state["proposal"])
    return None, state


GRAPH = {
    "fetch": node_fetch,
    "technical": node_technical,
    "technical_retry_guard": node_technical_retry_guard,
    "fundamental": node_fundamental,
    "risk": node_risk,
    "sentiment": node_sentiment,
    "propose": node_propose,
    "flag_review": node_flag_review,
    "abort": node_abort,
    "log": node_log,
}


def run(symbol, principal, risk_pct=10.0, start="fetch"):
    state = {
        "symbol": symbol,
        "principal": principal,
        "risk_pct": risk_pct,
        "iters": 0,
        "quote": None,
        "hist": None,
        "hist_multi": None,
        "fundamental_snapshot": None,
        "technical_indicators": None,
        "technical_verdict": None,
        "fundamental_verdict": None,
        "risk_verdict": None,
        "sentiment_verdict": None,
        "status": None,
        "proposal": None,
    }
    node = start

    while node is not None:
        log.debug("node=%s", node)
        fn = GRAPH[node]
        node, state = fn(state)

    return state


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        symbols = [s.strip().upper() for s in " ".join(sys.argv[1:]).replace(",", " ").split()]
    else:
        index_name = os.environ.get("NSE_INDEX", "").strip()
        if index_name:
            symbols = nse_data.get_index_symbols(index_name)
            scan_limit = os.environ.get("NSE_SCAN_LIMIT", "").strip()
            if scan_limit:
                symbols = symbols[: int(scan_limit)]
        else:
            symbols = [os.environ.get("NSE_SYMBOL", "RELIANCE")]

    principal = float(os.environ.get("NSE_PRINCIPAL", "100000"))
    risk_pct = float(os.environ.get("NSE_RISK_PCT", "10"))
    scan_delay_seconds = float(os.environ.get("NSE_SCAN_DELAY_SECONDS", "1"))

    for i, symbol in enumerate(symbols):
        if i > 0:
            time.sleep(scan_delay_seconds)
        try:
            final_state = run(symbol, principal, risk_pct)
            log.info("final[%s]: %s", symbol, final_state["proposal"])
        except Exception:
            log.warning("run failed for %s, continuing batch", symbol, exc_info=True)
