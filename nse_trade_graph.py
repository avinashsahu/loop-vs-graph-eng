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

import bhavcopy
import fundamentals
import nse_data
import ta_analysis
from llm import call_llm
from logging_config import setup_logging
from market_time import now_ist

TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
NSE_SCAN_LABEL = os.environ.get("NSE_SCAN_LABEL", "manual")

log = setup_logging("nse")


def _verdict_prompt(question):
    return f"{question}\nReply with exactly one word first: GOOD or BAD. Then, on the same line, a short reason."


def node_fetch(state):
    state["iters"] += 1
    state["quote"] = get_stock_live_quotes(state["symbol"])
    time.sleep(nse_data.NSE_CALL_DELAY_SECONDS)  # was the one unthrottled call in the pipeline

    # get_stock_live_quotes returns None on error, and can occasionally return a raw
    # dict missing expected keys on an internal nsemine exception -- checking here once
    # avoids a raw crash (and a missing log record) deeper in node_risk/node_sentiment.
    required_quote_keys = (
        "name", "sector", "changepct", "previous_close", "change", "upper_circuit", "lower_circuit",
    )
    if not state["quote"] or not all(k in state["quote"] for k in required_quote_keys):
        state["risk_verdict"] = f"BAD: quote fetch failed or malformed for {state['symbol']}"
        log.warning(state["risk_verdict"])
        return "abort", state

    state["hist_multi"] = nse_data.get_multi_timeframe_history(state["symbol"])
    state["hist"] = state["hist_multi"]["D"]
    state["fundamental_snapshot"] = fundamentals.get_fundamental_snapshot(state["symbol"])

    try:
        state["delivery_trend"] = bhavcopy.get_delivery_trend(state["symbol"])
    except Exception:
        # bhavcopy.db may not exist/be backfilled yet -- this is additional context,
        # not a required input, so a missing/fresh DB shouldn't abort the whole run.
        log.warning("bhavcopy delivery trend unavailable for %s", state["symbol"], exc_info=True)
        state["delivery_trend"] = None

    return "technical", state


def node_technical(state):
    indicators = {tf: ta_analysis.compute_indicators(hist) for tf, hist in state["hist_multi"].items()}
    state["technical_indicators"] = indicators
    log.info("iter=%d indicators=%s", state["iters"], indicators)

    # Deterministic, not an LLM call -- see ta_analysis.score_technical's docstring for
    # why: live testing showed this exact threshold/comparison task isn't something an
    # LLM (gemma4 or Fin-R1) applies reliably, regardless of model quality.
    result = ta_analysis.score_technical(indicators)
    state["technical_verdict"] = (
        f"{result['verdict']} (score={result['score']}, confluence={result['confluence_ratio']} "
        f"of {result['engaged_families']} engaged families): families={result['families']}; "
        f"daily RSI14={result['daily_rsi']} {result['rsi_note']} (adaptive band={result['rsi_band']}); "
        f"per-timeframe {result['breakdown']}"
    )
    log.info("iter=%d technical_verdict=%r", state["iters"], state["technical_verdict"])

    if result["verdict"] == "GOOD":
        return "fundamental", state
    return "abort", state


def node_fundamental(state):
    snap = state["fundamental_snapshot"] or {}
    eps, pat = snap.get("eps"), snap.get("pat")

    if (isinstance(eps, (int, float)) and eps < 0) or (isinstance(pat, (int, float)) and pat < 0):
        state["fundamental_verdict"] = f"BAD: negative EPS/PAT (eps={eps}, pat={pat})"
        log.warning(state["fundamental_verdict"])
        return "abort", state

    if not snap.get("complete", True):
        # One or more fetches failed (NSE rate-limit/block, transient error) -- judging a
        # prompt full of Nones isn't a real fundamental read, and silently defaulting to
        # GOOD would present an unvetted symbol as fully checked. Surface it for a human
        # to look at instead of guessing.
        state["fundamental_verdict"] = "BAD: fundamental data fetch was incomplete, not evaluated"
        log.warning(state["fundamental_verdict"])
        return "flag_review", state

    verdict = call_llm(
        _verdict_prompt(
            f"Fundamental snapshot for {state['symbol']} ({snap.get('company_name')}): "
            f"corp actions={snap.get('corp_actions')}, "
            f"corp announcements={snap.get('corp_announcements')}, "
            f"shareholding pattern (recent periods)={snap.get('shareholding_pattern')}, "
            f"yearwise returns={snap.get('yearwise_returns')}, "
            f"peer comparison (quarter {snap.get('peer_comparison_quarter')})={snap.get('peer_comparison')}, "
            f"delivery-volume trend (from NSE bhavcopy, recent vs prior average, rising means "
            f"more genuine buying interest not just intraday churn)={state.get('delivery_trend')}.\n"
            "Does the company look fundamentally sound -- no red flags in recent corporate actions, "
            "announcements, shareholding trend, or delivery trend, and reasonable standing versus peers?"
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

    # Compute today's low/high from the 5-minute bars (5 min TTL, refreshes through the
    # day) rather than the daily bar's row (cached until IST midnight -- its low/high
    # freeze at whatever they were when first fetched today). Otherwise a stock crashing
    # through its circuit limit later in the day still reads as fine on a later recheck.
    today = now_ist().date()
    intraday_5m = state["hist_multi"]["5"]
    today_bars = intraday_5m[intraday_5m["datetime"].dt.date == today]
    day_low = today_bars["low"].min() if not today_bars.empty else state["hist"].iloc[-1]["low"]
    day_high = today_bars["high"].max() if not today_bars.empty else state["hist"].iloc[-1]["high"]

    if day_low <= lower_circuit * 1.02:
        state["risk_verdict"] = f"BAD: price near lower circuit ({day_low} vs {lower_circuit})"
        log.warning(state["risk_verdict"])
        return "abort", state

    if day_high >= upper_circuit * 0.98:
        state["risk_verdict"] = f"BAD: price near upper circuit ({day_high} vs {upper_circuit}) -- likely unfillable"
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


def build_record(state):
    """The one place a state dict becomes a log/email record -- used by node_log and by
    intraday_recheck.py (which re-runs the graph outside the normal fetch->...->log path
    and used to hand-rebuild a near-copy of this, which is exactly the kind of drift that
    lets a field show up in the digest but silently render as None in an intraday alert).
    Uses this module's NSE_SCAN_LABEL constant -- intraday_recheck.py relies on getting
    its own label here, which is why it sets os.environ["NSE_SCAN_LABEL"] *before*
    importing this module (module-level constants are computed once, at import time)."""
    return {
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
        "eps": (state.get("fundamental_snapshot") or {}).get("eps"),
        "pat": (state.get("fundamental_snapshot") or {}).get("pat"),
        "delivery_trend": state.get("delivery_trend"),
        "risk_verdict": state.get("risk_verdict"),
        "sentiment_verdict": state.get("sentiment_verdict"),
        "status": state["status"],
        "proposal": state["proposal"],
    }


def node_log(state):
    record = build_record(state)
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    log.info("status=%s proposal=%r", state["status"], state["proposal"])
    return None, state


GRAPH = {
    "fetch": node_fetch,
    "technical": node_technical,
    "fundamental": node_fundamental,
    "risk": node_risk,
    "sentiment": node_sentiment,
    "propose": node_propose,
    "flag_review": node_flag_review,
    "abort": node_abort,
    "log": node_log,
}


def run(symbol, principal, risk_pct=10.0):
    state = {
        "symbol": symbol,
        "principal": principal,
        "risk_pct": risk_pct,
        "iters": 0,
        "quote": None,
        "hist": None,
        "hist_multi": None,
        "fundamental_snapshot": None,
        "delivery_trend": None,
        "technical_indicators": None,
        "technical_verdict": None,
        "fundamental_verdict": None,
        "risk_verdict": None,
        "sentiment_verdict": None,
        "status": None,
        "proposal": None,
    }
    node = "fetch"

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
