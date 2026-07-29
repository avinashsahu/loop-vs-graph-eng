from dotenv import load_dotenv

# Must run before any of this project's own modules are imported below -- several of
# them (fundamentals, nse_data, cache) read env-configured constants at module level,
# and .env was previously only ever loaded as a side effect of importing llm.py, which
# doesn't reliably happen first. Confirmed live: FUNDAMENTALS_CACHE_TTL_HOURS from .env
# was silently ignored because `import fundamentals` (below) ran before load_dotenv().
load_dotenv()

import json
import math
import os
import time
from dataclasses import asdict

from nsemine.live import get_stock_live_quotes

import bhavcopy
import fundamentals
import nse_data
import position_risk
import ta_analysis
from evaluation import EvaluationLedger
from fundamental_evidence import (
    EVIDENCE_VERSION,
    PROMPT_VERSION,
    build_fundamental_evidence,
)
from llm import (
    FUNDAMENTAL_SCHEMA_VERSION,
    active_model_config,
    assess_fundamentals,
)
from logging_config import setup_logging
from market_time import now_ist
from shareholding import get_shareholding_history

TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
EVALUATION_DB_PATH = os.environ.get("EVALUATION_DB_PATH", "evaluation.db")
NSE_SCAN_LABEL = os.environ.get("NSE_SCAN_LABEL", "manual")
NSE_POLICY_VERSION = os.environ.get(
    "NSE_POLICY_VERSION",
    "technical-confluence-v1+risk-atr-target-v2+sentiment-volatility-v1+llm-prompts-v4",
)

log = setup_logging("nse")

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

    market_snapshot = nse_data.get_market_snapshot(state["symbol"])
    state["market_snapshot"] = market_snapshot.metadata()
    state["hist_multi"] = market_snapshot.histories
    state["hist"] = state["hist_multi"]["D"]

    try:
        state["delivery_trend"] = bhavcopy.get_delivery_trend(state["symbol"])
    except Exception:
        # bhavcopy.db may not exist/be backfilled yet -- this is additional context,
        # not a required input, so a missing/fresh DB shouldn't abort the whole run.
        log.warning("bhavcopy delivery trend unavailable for %s", state["symbol"], exc_info=True)
        state["delivery_trend"] = None

    return "technical", state


def node_technical(state):
    # Deterministic, not an LLM call -- see ta_analysis.evaluate_technical and
    # score_technical for
    # why: live testing showed this exact threshold/comparison task isn't something an
    # LLM (gemma4 or Fin-R1) applies reliably, regardless of model quality.
    assessment = ta_analysis.evaluate_technical(state["hist_multi"])
    state["technical_indicators"] = assessment.indicators
    state["technical_assessment"] = assessment.to_dict()
    log.info(
        "iter=%d technical_status=%s indicators=%s",
        state["iters"],
        assessment.status,
        assessment.indicators,
    )

    if assessment.status != "ready":
        state["technical_verdict"] = (
            f"BAD invalid_data: {', '.join(assessment.reason_codes)}"
        )
        log.warning(
            "iter=%d technical_verdict=%r",
            state["iters"],
            state["technical_verdict"],
        )
        return "abort", state

    result = assessment.evidence
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
    if state["fundamental_snapshot"] is None:
        state["fundamental_snapshot"] = fundamentals.get_fundamental_snapshot(
            state["symbol"]
        )
    snap = state["fundamental_snapshot"] or {}
    eps, pat = snap.get("eps"), snap.get("pat")

    if (isinstance(eps, (int, float)) and eps < 0) or (isinstance(pat, (int, float)) and pat < 0):
        state["fundamental_verdict"] = f"REJECT: negative EPS/PAT (eps={eps}, pat={pat})"
        state["fundamental_assessment"] = {
            "verdict": "REJECT",
            "reason_code": "PEER_OR_EARNINGS_WEAKNESS",
            "summary": f"Negative EPS/PAT (eps={eps}, pat={pat}).",
            "evidence_ids": ["EARNINGS_HARD_CHECK"],
            "missing": [],
        }
        log.warning(state["fundamental_verdict"])
        return "abort", state

    if not snap.get("complete", True):
        # One or more fetches failed (NSE rate-limit/block, transient error) -- judging a
        # prompt full of Nones isn't a real fundamental read, and silently defaulting to
        # GOOD would present an unvetted symbol as fully checked. Surface it for a human
        # to look at instead of guessing.
        state["fundamental_verdict"] = "REVIEW: fundamental data fetch was incomplete, not evaluated"
        state["fundamental_assessment"] = {
            "verdict": "REVIEW",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "summary": "Fundamental data fetch was incomplete.",
            "evidence_ids": [],
            "missing": ["fundamental_snapshot"],
        }
        log.warning(state["fundamental_verdict"])
        return "flag_review", state

    try:
        history = get_shareholding_history(state["symbol"])
    except Exception:
        log.warning(
            "shareholding[%s]: cached history unavailable",
            state["symbol"],
            exc_info=True,
        )
        state["fundamental_verdict"] = "REVIEW: cached shareholding history unavailable"
        state["fundamental_assessment"] = {
            "verdict": "REVIEW",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "summary": "Cached shareholding history is unavailable.",
            "evidence_ids": [],
            "missing": ["shareholding_history"],
        }
        return "flag_review", state

    state["shareholding_history"] = asdict(history)
    if history.status != "ready":
        state["fundamental_verdict"] = (
            "REVIEW: shareholding history is pending background XBRL warm"
        )
        state["fundamental_assessment"] = {
            "verdict": "REVIEW",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "summary": "Shareholding history is pending background XBRL warm.",
            "evidence_ids": [],
            "missing": ["shareholding_history"],
        }
        return "flag_review", state

    evidence = build_fundamental_evidence(state["symbol"], snap, history)
    state["fundamental_evidence"] = evidence.payload
    if not evidence.payload["coverage"]["complete"]:
        missing = evidence.payload["coverage"]["missing"][:3]
        state["fundamental_verdict"] = (
            f"REVIEW: required fundamental evidence is missing ({', '.join(missing)})"
        )
        state["fundamental_assessment"] = {
            "verdict": "REVIEW",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "summary": "Required fundamental evidence is missing.",
            "evidence_ids": [],
            "missing": missing,
        }
        return "flag_review", state

    prompt = evidence.prompt()
    state["fundamental_prompt"] = {
        "prompt_version": PROMPT_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "schema_version": FUNDAMENTAL_SCHEMA_VERSION,
        "prompt_hash": evidence.prompt_hash,
        "evidence_hash": evidence.evidence_hash,
    }
    assessment = assess_fundamentals(prompt, evidence.ids)
    state["fundamental_assessment"] = assessment.to_dict()
    state["fundamental_verdict"] = assessment.summary
    log.info("fundamental_assessment=%r", state["fundamental_assessment"])

    if assessment.verdict == "PASS":
        return "risk", state
    return "flag_review", state


def node_risk(state):
    quote = state["quote"]
    # nsemine's upper_circuit/lower_circuit fields are swapped — correct on read.
    lower_circuit = quote["upper_circuit"]
    upper_circuit = quote["lower_circuit"]

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

    plan = position_risk.size_position(
        principal=state["principal"],
        entry_price=price,
        atr=state["technical_indicators"]["D"]["atr14"],
        max_loss_pct=state["max_loss_pct"],
        max_allocation_pct=state["max_allocation_pct"],
        atr_stop_multiple=state["atr_stop_multiple"],
        reward_risk_ratio=state["reward_risk_ratio"],
    )
    state["risk_plan"] = plan.to_dict()
    if isinstance(plan, position_risk.RiskRejection):
        state["risk_verdict"] = f"BAD: {plan.reason_code}: {plan.message}"
        log.warning(state["risk_verdict"])
        return "abort", state

    if plan.stop_price <= lower_circuit:
        state["risk_verdict"] = (
            f"BAD: ATR stop {plan.stop_price:.2f} is at/below lower circuit "
            f"{lower_circuit:.2f}"
        )
        log.warning(state["risk_verdict"])
        return "abort", state

    state["position_size"] = plan.capital_required
    state["max_shares"] = plan.shares
    state["risk_verdict"] = (
        f"GOOD: {plan.shares} shares at ~{plan.entry_price:.2f}, "
        f"ATR stop={plan.stop_price:.2f}, target={plan.target_price:.2f} "
        f"({plan.reward_risk_ratio:.2f}R), max loss at stop="
        f"{plan.max_loss_at_stop:.2f}/{plan.risk_budget:.2f} budget, "
        f"capital={plan.capital_required:.2f}/{plan.allocation_cap:.2f} cap, "
        f"binding={plan.binding_constraint}, lower_circuit={lower_circuit}, "
        f"upper_circuit={upper_circuit}"
    )
    log.info(state["risk_verdict"])
    return "sentiment", state


def node_sentiment(state):
    quote = state["quote"]
    change_pct = float(quote["changepct"])
    entry_price = float(state["risk_plan"]["entry_price"])
    atr_pct = (
        float(state["technical_indicators"]["D"]["atr14"]) / entry_price * 100
    )
    threshold_pct = min(10.0, max(3.0, 2.0 * atr_pct))
    if not all(math.isfinite(value) for value in (change_pct, atr_pct, threshold_pct)):
        verdict = "BAD: invalid daily-move or ATR context"
    elif abs(change_pct) > threshold_pct:
        verdict = (
            f"BAD: daily move {change_pct:+.2f}% exceeds volatility-aware "
            f"{threshold_pct:.2f}% review threshold"
        )
    else:
        verdict = (
            f"GOOD: daily move {change_pct:+.2f}% is within volatility-aware "
            f"{threshold_pct:.2f}% review threshold"
        )
    state["sentiment_verdict"] = verdict
    log.info("sentiment_verdict=%r", verdict)

    if verdict.startswith("GOOD"):
        return "propose", state
    return "flag_review", state


def node_propose(state):
    state["status"] = "proposed"
    plan = state["risk_plan"]
    actual_loss_pct = plan["max_loss_at_stop"] / state["principal"] * 100
    state["proposal"] = (
        f"PROPOSAL (not executed): BUY {state['symbol']} — up to "
        f"{state['max_shares']} shares, entry ~₹{plan['entry_price']:.2f}, "
        f"stop ₹{plan['stop_price']:.2f}, target ₹{plan['target_price']:.2f} "
        f"({plan['reward_risk_ratio']:.2f}R), "
        f"capital ~₹{state['position_size']:.0f}, "
        f"max loss ~₹{plan['max_loss_at_stop']:.0f} "
        f"and planned profit ~₹{plan['planned_profit_at_target']:.0f} "
        f"({actual_loss_pct:.2f}% actual; {state['max_loss_pct']}% policy cap "
        f"of ₹{state['principal']:.0f} principal). "
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
        "max_loss_pct": state["max_loss_pct"],
        "max_allocation_pct": state["max_allocation_pct"],
        "atr_stop_multiple": state["atr_stop_multiple"],
        "reward_risk_ratio": state["reward_risk_ratio"],
        "iters": state["iters"],
        "technical_indicators": state.get("technical_indicators"),
        "technical_assessment": state.get("technical_assessment"),
        "market_snapshot": state.get("market_snapshot"),
        "technical_verdict": state.get("technical_verdict"),
        "fundamental_verdict": state.get("fundamental_verdict"),
        "fundamental_assessment": state.get("fundamental_assessment"),
        "fundamental_evidence": state.get("fundamental_evidence"),
        "fundamental_prompt": state.get("fundamental_prompt"),
        "shareholding_history": state.get("shareholding_history"),
        "eps": (state.get("fundamental_snapshot") or {}).get("eps"),
        "pat": (state.get("fundamental_snapshot") or {}).get("pat"),
        "delivery_trend": state.get("delivery_trend"),
        "model_config": active_model_config(),
        "policy_version": NSE_POLICY_VERSION,
        "risk_plan": state.get("risk_plan"),
        "risk_verdict": state.get("risk_verdict"),
        "sentiment_verdict": state.get("sentiment_verdict"),
        "status": state["status"],
        "proposal": state["proposal"],
    }


def node_log(state):
    record = build_record(state)
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    try:
        EvaluationLedger(EVALUATION_DB_PATH).record_decision(record)
    except Exception:
        # JSONL remains the durable fallback and can be imported later. Evaluation
        # telemetry must not turn an otherwise completed scan into a failed scan.
        log.warning("evaluation ledger write failed", exc_info=True)
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


def run(
    symbol,
    principal,
    max_allocation_pct=10.0,
    max_loss_pct=1.0,
    atr_stop_multiple=2.0,
    reward_risk_ratio=2.0,
):
    state = {
        "symbol": symbol,
        "principal": principal,
        "max_loss_pct": max_loss_pct,
        "max_allocation_pct": max_allocation_pct,
        "atr_stop_multiple": atr_stop_multiple,
        "reward_risk_ratio": reward_risk_ratio,
        "iters": 0,
        "quote": None,
        "hist": None,
        "hist_multi": None,
        "market_snapshot": None,
        "fundamental_snapshot": None,
        "delivery_trend": None,
        "technical_indicators": None,
        "technical_assessment": None,
        "technical_verdict": None,
        "fundamental_verdict": None,
        "fundamental_assessment": None,
        "fundamental_evidence": None,
        "fundamental_prompt": None,
        "shareholding_history": None,
        "risk_plan": None,
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
    max_allocation_pct = float(
        os.environ.get(
            "NSE_MAX_ALLOCATION_PCT",
            os.environ.get("NSE_RISK_PCT", "10"),
        )
    )
    max_loss_pct = float(os.environ.get("NSE_MAX_LOSS_PCT", "1"))
    atr_stop_multiple = float(os.environ.get("NSE_ATR_STOP_MULTIPLE", "2"))
    reward_risk_ratio = float(os.environ.get("NSE_REWARD_RISK_RATIO", "2"))
    scan_delay_seconds = float(os.environ.get("NSE_SCAN_DELAY_SECONDS", "1"))

    for i, symbol in enumerate(symbols):
        if i > 0:
            time.sleep(scan_delay_seconds)
        try:
            final_state = run(
                symbol,
                principal,
                max_allocation_pct,
                max_loss_pct,
                atr_stop_multiple,
                reward_risk_ratio,
            )
            log.info("final[%s]: %s", symbol, final_state["proposal"])
        except Exception:
            log.warning("run failed for %s, continuing batch", symbol, exc_info=True)
