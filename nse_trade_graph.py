from dotenv import load_dotenv

# Must run before any of this project's own modules are imported below -- several of
# them (fundamentals, nse_data, cache) read env-configured constants at module level,
# and .env was previously only ever loaded as a side effect of importing llm.py, which
# doesn't reliably happen first. Confirmed live: FUNDAMENTALS_CACHE_TTL_HOURS from .env
# was silently ignored because `import fundamentals` (below) ran before load_dotenv().
load_dotenv()

import hashlib
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from fundamental_research import (
    FUNDAMENTAL_POLICY_VERSION,
    evaluate_fundamental_research,
)
from nse_client import get_stock_live_quotes
from llm import (
    FUNDAMENTAL_SCHEMA_VERSION,
    TECHNICAL_EXPLANATION_PROMPT_VERSION,
    TECHNICAL_EXPLANATION_SCHEMA_VERSION,
    active_model_config,
    assess_fundamentals,
    summarize_technical_run,
)
from logging_config import setup_logging
from market_time import now_ist
from scan_engine import (
    ModelIdentity,
    PersistenceReceipt,
    PolicyIdentity,
    ScanEngine,
    ScanExecution,
    ScanExecutionError,
    ScanFailure,
    ScanPurpose,
    ScanRequest,
    StageTiming,
)
from shareholding import get_shareholding_history

TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
SCAN_RUN_LOG_PATH = os.environ.get("SCAN_RUN_LOG_PATH", "scan_runs.jsonl")
SCAN_RUN_STALE_AFTER_SECONDS = float(
    os.environ.get("SCAN_RUN_STALE_AFTER_SECONDS", "21600")
)
EVALUATION_DB_PATH = os.environ.get("EVALUATION_DB_PATH", "evaluation.db")
NSE_SCAN_LABEL = os.environ.get("NSE_SCAN_LABEL", "manual")
NSE_TECHNICAL_POLICY_ID = os.environ.get(
    "NSE_TECHNICAL_POLICY_ID",
    ta_analysis.REVISED_TECHNICAL_POLICY_ID,
)
TECHNICAL_POLICY = ta_analysis.select_technical_policy(NSE_TECHNICAL_POLICY_ID)
NSE_POLICY_VERSION = os.environ.get(
    "NSE_POLICY_VERSION",
    f"{NSE_TECHNICAL_POLICY_ID}+risk-atr-target-v3"
    "+sentiment-volatility-v1+fundamental-sector-v4+llm-prompts-v7",
)

log = setup_logging("nse")
_SCAN_JOURNAL_LOCK = threading.Lock()
NSE_SCAN_CONCURRENCY = max(1, int(os.environ.get("NSE_SCAN_CONCURRENCY", "2")))


def _set_decision_reason(state, stage, code):
    state["decision_reason"] = {"stage": stage, "code": code}


def _record_scan_event(event):
    record = {
        "recorded_at": now_ist().isoformat(),
        **event,
    }
    try:
        with _SCAN_JOURNAL_LOCK:
            with open(SCAN_RUN_LOG_PATH, "a") as journal:
                journal.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        log.warning("scan-run JSONL write failed", exc_info=True)


def _indicates_nse_pressure(result) -> bool:
    """True when a scan failure looks like NSE throttling / transport pressure."""
    failure = getattr(result, "failure", None)
    if failure is None:
        return False
    text = f"{failure.error_type} {failure.message}".lower()
    return any(
        token in text
        for token in ("429", "403", "401", "rate", "timeout", "timed out", "unavailable")
    )


def _recover_stale_scan_events():
    path = Path(SCAN_RUN_LOG_PATH)
    if not path.exists():
        return
    runs = {}
    try:
        with path.open() as journal:
            for line in journal:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                run_id = event.get("run_id")
                if not run_id:
                    continue
                event_type = event.get("event")
                if event_type == "scan_started":
                    try:
                        recorded_at = datetime.fromisoformat(event["recorded_at"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    runs[run_id] = {
                        "recorded_at": recorded_at,
                        "pending": set(event.get("requested_symbols") or []),
                    }
                elif event_type in {"symbol_completed", "symbol_failed"}:
                    if run_id in runs:
                        runs[run_id]["pending"].discard(event.get("symbol"))
                elif event_type == "scan_finished":
                    runs.pop(run_id, None)
    except OSError:
        log.warning("scan-run JSONL recovery read failed", exc_info=True)
        return

    current_time = now_ist()
    for run_id, run in runs.items():
        recorded_at = run["recorded_at"]
        if recorded_at.tzinfo is None:
            continue
        age_seconds = (current_time - recorded_at).total_seconds()
        if age_seconds < SCAN_RUN_STALE_AFTER_SECONDS:
            continue
        for symbol in sorted(run["pending"]):
            _record_scan_event(
                {
                    "event": "symbol_failed",
                    "run_id": run_id,
                    "symbol": symbol,
                    "error_type": "RecoveredIncompleteScan",
                    "reason": "No terminal event before the stale-run timeout",
                }
            )
        _record_scan_event(
            {
                "event": "scan_finished",
                "run_id": run_id,
                "recovered": True,
            }
        )


def node_fetch(state):
    state["iters"] += 1
    symbol = state["symbol"]

    def _fetch_quote():
        quote = get_stock_live_quotes(symbol)
        # Pace the quote the same way hist misses pace; overlaps with D/5 sleeps.
        time.sleep(nse_data.NSE_CALL_DELAY_SECONDS)
        return quote

    def _fetch_quote_and_snapshot_sequential():
        quote = _fetch_quote()
        snapshot = nse_data.get_market_snapshot(symbol)
        return quote, snapshot

    # Quote and hist are independent NSE calls; overlap within one symbol only.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            quote_future = pool.submit(_fetch_quote)
            snapshot_future = pool.submit(nse_data.get_market_snapshot, symbol)
            state["quote"] = quote_future.result()
            market_snapshot = snapshot_future.result()
    except Exception:
        log.warning(
            "parallel quote/hist fetch failed for %s; falling back to sequential",
            symbol,
            exc_info=True,
        )
        state["quote"], market_snapshot = _fetch_quote_and_snapshot_sequential()

    # A failed response can be None or malformed. Checking here once avoids a raw crash
    # (and a missing log record) deeper in node_risk/node_sentiment.
    required_quote_keys = (
        "name", "sector", "changepct", "previous_close", "change", "upper_circuit", "lower_circuit",
    )
    if not state["quote"] or not all(k in state["quote"] for k in required_quote_keys):
        state["risk_verdict"] = (
            f"REJECT: quote fetch failed or malformed for {state['symbol']}"
        )
        _set_decision_reason(state, "market_data", "QUOTE_FETCH_FAILED")
        log.warning(state["risk_verdict"])
        return "abort", state

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

    # Batch scans preload benchmark_daily once on ProductionScanAdapter.
    benchmark_meta = state.pop("_benchmark_metadata", None)
    if state.get("benchmark_daily") is not None and benchmark_meta is not None:
        state["market_snapshot"]["benchmark"] = benchmark_meta
    else:
        benchmark_snapshot = nse_data.get_market_snapshot(
            TECHNICAL_POLICY.benchmark_symbol,
            timeframes=("D",),
        )
        state["benchmark_daily"] = benchmark_snapshot.histories["D"]
        state["market_snapshot"]["benchmark"] = benchmark_snapshot.metadata()

    return "technical", state


def node_technical(state):
    # Deterministic, not an LLM call -- see ta_analysis.evaluate_technical and
    # score_technical for
    # why: live testing showed this exact threshold/comparison task isn't something an
    # LLM (gemma4 or Fin-R1) applies reliably, regardless of model quality.
    snapshot_metadata = state.get("market_snapshot") or {}
    benchmark_metadata = snapshot_metadata.get("benchmark") or {}
    completed_at = benchmark_metadata.get("observed_at")
    observations = ta_analysis.TechnicalObservations(
        histories=state["hist_multi"],
        benchmark_daily=state.get("benchmark_daily"),
        benchmark_symbol=benchmark_metadata.get("symbol"),
        delivery_trend=state.get("delivery_trend"),
        completed_at=(
            datetime.fromisoformat(completed_at)
            if completed_at
            else None
        ),
        completion_policy_id=snapshot_metadata.get(
            "completion_policy_id"
        ),
    )
    assessment = ta_analysis.evaluate_technical(
        observations,
        TECHNICAL_POLICY,
    )
    state["technical_indicators"] = assessment.indicators
    state["technical_assessment"] = assessment.to_dict()
    state["technical_fact_ledger"] = ta_analysis.build_technical_fact_ledger(
        state.get("symbol", "UNKNOWN"),
        assessment,
        state.get("market_snapshot"),
        observations,
    )
    log.info(
        "iter=%d technical_status=%s indicators=%s",
        state["iters"],
        assessment.status,
        assessment.indicators,
    )

    if assessment.status != "ready":
        state["technical_verdict"] = (
            f"REJECT invalid_data: {', '.join(assessment.reason_codes)}"
        )
        _set_decision_reason(state, "technical", "INVALID_MARKET_DATA")
        log.warning(
            "iter=%d technical_verdict=%r",
            state["iters"],
            state["technical_verdict"],
        )
        return "abort", state

    result = assessment.evidence
    verdict_label = "GOOD" if result["verdict"] == "GOOD" else "REJECT"
    relative_strength = result.get("relative_strength") or {}
    participation = result.get("participation") or {}
    context = ""
    if relative_strength:
        context += (
            f"relative strength={relative_strength['relative_return_pct']}% "
            f"vs {result['benchmark_symbol']}; "
        )
    if participation:
        context += (
            f"participation={participation['participation_state']} "
            f"(score={result['families']['participation']}); "
        )
    state["technical_verdict"] = (
        f"{verdict_label} (score={result['score']}, confluence={result['confluence_ratio']} "
        f"of {result['engaged_families']} engaged families): families={result['families']}; "
        f"daily RSI14={result['daily_rsi']} {result['rsi_note']} (adaptive band={result['rsi_band']}); "
        f"{context}"
        f"per-timeframe {result['breakdown']}"
    )
    log.info("iter=%d technical_verdict=%r", state["iters"], state["technical_verdict"])

    if result["verdict"] == "GOOD":
        return "fundamental", state
    _set_decision_reason(state, "technical", "TECHNICAL_CONFLUENCE_FAILED")
    return "abort", state


def node_technical_explanation(state):
    """Explain an already locked decision; failures always preserve the decision."""
    ledger = state.get("technical_fact_ledger")
    state["technical_explanation"] = None
    state["technical_explanation_meta"] = {
        "prompt_version": TECHNICAL_EXPLANATION_PROMPT_VERSION,
        "schema_version": TECHNICAL_EXPLANATION_SCHEMA_VERSION,
        "input_schema_version": (ledger or {}).get("schema_version"),
        "input_hash": (
            hashlib.sha256(
                json.dumps(
                    ledger,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:16]
            if ledger
            else None
        ),
        "status": "missing_input",
        "output_valid": False,
        "fallback_used": True,
        "response_chars": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": None,
    }
    if not ledger:
        return None, state

    try:
        run = summarize_technical_run(ledger)
    except Exception:
        state["technical_explanation_meta"]["status"] = "backend_error"
        log.warning(
            "technical explanation failed for %s; deterministic fallback retained",
            state.get("symbol"),
            exc_info=True,
        )
        return None, state

    state["technical_explanation_meta"].update(
        {
            "status": run.status,
            "output_valid": run.output_valid,
            "fallback_used": not run.output_valid,
            "response_chars": run.response_chars,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "reasoning_tokens": run.reasoning_tokens,
        }
    )
    if run.output_valid:
        state["technical_explanation"] = run.explanation.to_dict()
    return None, state


def node_fundamental(state):
    if state["fundamental_snapshot"] is None:
        state["fundamental_snapshot"] = fundamentals.get_fundamental_snapshot(
            state["symbol"]
        )
    snap = state["fundamental_snapshot"] or {}
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
        _set_decision_reason(state, "fundamental", "INSUFFICIENT_EVIDENCE")
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
        _set_decision_reason(state, "fundamental", "INSUFFICIENT_EVIDENCE")
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
        _set_decision_reason(state, "fundamental", "INSUFFICIENT_EVIDENCE")
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
        _set_decision_reason(state, "fundamental", "INSUFFICIENT_EVIDENCE")
        return "flag_review", state

    state["fundamental_prompt"] = {
        "prompt_version": PROMPT_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "schema_version": FUNDAMENTAL_SCHEMA_VERSION,
        "policy_version": FUNDAMENTAL_POLICY_VERSION,
        "prompt_hash": evidence.prompt_hash,
        "evidence_hash": evidence.evidence_hash,
    }
    decision = evaluate_fundamental_research(
        evidence,
        lambda qualitative_prompt, evidence_ids: assess_fundamentals(
            qualitative_prompt, evidence_ids
        ),
    )
    state["fundamental_assessment"] = decision.to_dict()
    state["fundamental_verdict"] = decision.summary
    log.info("fundamental_assessment=%r", state["fundamental_assessment"])

    if decision.verdict == "PASS":
        return "risk", state
    _set_decision_reason(
        state,
        "fundamental",
        decision.reason_code,
    )
    if decision.verdict == "REJECT":
        return "abort", state
    return "flag_review", state


def node_risk(state):
    quote = state["quote"]
    lower_circuit = quote["lower_circuit"]
    upper_circuit = quote["upper_circuit"]
    price = quote["previous_close"] + quote["change"]

    # Compute today's low/high from the 5-minute bars (5 min TTL, refreshes through the
    # day) rather than the daily bar's row (cached until IST midnight -- its low/high
    # freeze at whatever they were when first fetched today). Otherwise a stock crashing
    # through its circuit limit later in the day still reads as fine on a later recheck.
    today = now_ist().date()
    intraday_5m = state["hist_multi"]["5"]
    today_bars = intraday_5m[intraday_5m["datetime"].dt.date == today]
    intraday_context_available = not today_bars.empty
    day_low = today_bars["low"].min() if intraday_context_available else None
    day_high = today_bars["high"].max() if intraday_context_available else None
    lower_band = lower_circuit * 1.02
    upper_band = upper_circuit * 0.98
    intraday_note = (
        f"same-day low={day_low}, high={day_high}"
        if intraday_context_available
        else "same-day intraday context unavailable"
    )
    state["circuit_context"] = {
        "policy": "current_entry_proximity",
        "current_price": price,
        "lower_circuit": lower_circuit,
        "upper_circuit": upper_circuit,
        "same_day_intraday_context_available": intraday_context_available,
        "current_near_lower_circuit": bool(price and price <= lower_band),
        "current_near_upper_circuit": bool(price and price >= upper_band),
        "lower_band_touched_today": bool(
            intraday_context_available and day_low <= lower_band
        ),
        "upper_band_touched_today": bool(
            intraday_context_available and day_high >= upper_band
        ),
    }

    if not price or price <= 0:
        state["risk_verdict"] = f"REJECT: no usable price for sizing (price={price})"
        _set_decision_reason(state, "risk", "INVALID_ENTRY_PRICE")
        log.warning(state["risk_verdict"])
        return "abort", state

    if price <= lower_band:
        state["risk_verdict"] = (
            "REJECT: current entry price is near the lower circuit "
            f"({price} vs {lower_circuit}); {intraday_note}"
        )
        _set_decision_reason(
            state,
            "risk",
            "LOWER_CIRCUIT_ENTRY_PROXIMITY",
        )
        log.warning(state["risk_verdict"])
        return "abort", state

    if price >= upper_band:
        state["risk_verdict"] = (
            "REJECT: current entry price is near the upper circuit "
            f"({price} vs {upper_circuit}); {intraday_note}"
        )
        _set_decision_reason(
            state,
            "risk",
            "UPPER_CIRCUIT_ENTRY_PROXIMITY",
        )
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
        state["risk_verdict"] = f"REJECT: {plan.reason_code}: {plan.message}"
        _set_decision_reason(state, "risk", plan.reason_code)
        log.warning(state["risk_verdict"])
        return "abort", state

    if plan.stop_price <= lower_circuit:
        state["risk_verdict"] = (
            f"REJECT: ATR stop {plan.stop_price:.2f} is at/below lower circuit "
            f"{lower_circuit:.2f}"
        )
        _set_decision_reason(
            state,
            "risk",
            "STOP_AT_OR_BELOW_LOWER_CIRCUIT",
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
        f"upper_circuit={upper_circuit}, circuit_policy=current_entry_proximity, "
        f"intraday_context_available={intraday_context_available}, "
        f"earlier_lower_touch={state['circuit_context']['lower_band_touched_today']}, "
        f"earlier_upper_touch={state['circuit_context']['upper_band_touched_today']}"
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
        verdict = "REVIEW: invalid daily-move or ATR context"
        reason_code = "INVALID_VOLATILITY_CONTEXT"
    elif abs(change_pct) > threshold_pct:
        verdict = (
            f"REVIEW: daily move {change_pct:+.2f}% exceeds volatility-aware "
            f"{threshold_pct:.2f}% review threshold"
        )
        reason_code = "EXCESSIVE_DAILY_MOVE"
    else:
        verdict = (
            f"GOOD: daily move {change_pct:+.2f}% is within volatility-aware "
            f"{threshold_pct:.2f}% review threshold"
        )
        reason_code = None
    state["sentiment_verdict"] = verdict
    log.info("sentiment_verdict=%r", verdict)

    if verdict.startswith("GOOD"):
        return "propose", state
    _set_decision_reason(state, "sentiment", reason_code)
    return "flag_review", state


def node_propose(state):
    state["status"] = "proposed"
    state["disposition"] = "PROPOSE"
    _set_decision_reason(state, "decision", "ALL_GATES_PASSED")
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
    return None, state


def node_flag_review(state):
    state["status"] = "flagged_for_review"
    state["disposition"] = "REVIEW"
    if not state.get("decision_reason"):
        _set_decision_reason(state, "decision", "MANUAL_REVIEW_REQUIRED")
    # Fundamental and sentiment are the only two checks that route here; surface the
    # one that explicitly requested human inspection.
    if state.get("sentiment_verdict"):
        check, verdict = "sentiment", state["sentiment_verdict"]
    else:
        check, verdict = "fundamental", state["fundamental_verdict"]
    state["proposal"] = (
        f"FLAGGED FOR MANUAL REVIEW: {check} check requires inspection for "
        f"{state['symbol']} — {verdict!r}"
    )
    return None, state


def node_abort(state):
    state["status"] = "aborted"
    state["disposition"] = "REJECT"
    if not state.get("decision_reason"):
        _set_decision_reason(state, "decision", "UNCLASSIFIED_REJECTION")
    state["proposal"] = None
    return None, state


def build_record(state):
    """The production adapter's one mutable-state to durable-record conversion.

    Callers receive the typed ScanResult; mutable workflow state stays behind that seam.
    The scan label is request state, not import-order configuration.
    """
    return {
        "timestamp": now_ist().isoformat(),
        "scan_label": state.get("scan_label", NSE_SCAN_LABEL),
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
        "technical_fact_ledger": state.get("technical_fact_ledger"),
        "technical_explanation": state.get("technical_explanation"),
        "technical_explanation_meta": state.get(
            "technical_explanation_meta"
        ),
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
        "circuit_context": state.get("circuit_context"),
        "model_config": active_model_config(),
        "policy_version": NSE_POLICY_VERSION,
        "risk_plan": state.get("risk_plan"),
        "risk_verdict": state.get("risk_verdict"),
        "sentiment_verdict": state.get("sentiment_verdict"),
        "status": state["status"],
        "disposition": state.get("disposition"),
        "decision_reason": state.get("decision_reason"),
        "proposal": state["proposal"],
    }


_STAGES = {
    "fetch": node_fetch,
    "technical": node_technical,
    "fundamental": node_fundamental,
    "risk": node_risk,
    "sentiment": node_sentiment,
    "propose": node_propose,
    "flag_review": node_flag_review,
    "abort": node_abort,
}


class ProductionScanAdapter:
    """Keep mutable stage implementation behind the typed Scan Engine seam."""

    def __init__(self):
        # One NSE daily fetch per adapter lifetime (one batch / scan engine).
        self._benchmark_snapshot = None
        self._benchmark_lock = threading.Lock()

    def _cached_benchmark(self):
        with self._benchmark_lock:
            reused = self._benchmark_snapshot is not None
            if not reused:
                self._benchmark_snapshot = nse_data.get_market_snapshot(
                    TECHNICAL_POLICY.benchmark_symbol,
                    timeframes=("D",),
                )
            metadata = self._benchmark_snapshot.metadata()
            metadata["batch_reused"] = reused
            return self._benchmark_snapshot.histories["D"], metadata

    def execute(self, request):
        state = _initial_scan_state(request)
        benchmark_daily, benchmark_metadata = self._cached_benchmark()
        state["benchmark_daily"] = benchmark_daily
        state["_benchmark_metadata"] = benchmark_metadata
        node = "fetch"
        timings = []

        while node is not None:
            log.debug("node=%s", node)
            stage_started = time.perf_counter()
            try:
                next_node, state = _STAGES[node](state)
            except Exception as error:
                timings.append(
                    StageTiming(
                        _timing_stage(node),
                        (time.perf_counter() - stage_started) * 1000,
                    )
                )
                raise ScanExecutionError(
                    node,
                    error,
                    tuple(timings),
                ) from error
            timings.append(
                StageTiming(
                    _timing_stage(node),
                    (time.perf_counter() - stage_started) * 1000,
                )
            )
            if next_node is None and not state.get("disposition"):
                raise ScanExecutionError(
                    node,
                    RuntimeError("scan ended before disposition persistence"),
                    tuple(timings),
                )
            node = next_node

        if state.get("disposition") in {"PROPOSE", "REVIEW"}:
            explanation_started = time.perf_counter()
            try:
                _, state = node_technical_explanation(state)
            except Exception:
                # The explanatory model is deliberately outside decision routing.
                # Even an implementation defect here must not erase a completed
                # deterministic disposition.
                state["technical_explanation"] = None
                state["technical_explanation_meta"] = {
                    "status": "internal_error",
                    "output_valid": False,
                    "fallback_used": True,
                }
                log.warning(
                    "technical explanation stage failed after disposition",
                    exc_info=True,
                )
            timings.append(
                StageTiming(
                    "technical_explanation",
                    (time.perf_counter() - explanation_started) * 1000,
                )
            )

        return ScanExecution(
            record=build_record(state),
            timings=tuple(timings),
        )


class ProductionDecisionStore:
    def __init__(self, trade_log_path, evaluation_db_path):
        self._trade_log_path = trade_log_path
        self._evaluation_db_path = evaluation_db_path
        self._persist_lock = threading.Lock()

    def persist(self, record):
        with self._persist_lock:
            with open(self._trade_log_path, "a") as trade_log:
                trade_log.write(json.dumps(record) + "\n")
            decision_id = None
            try:
                receipt = EvaluationLedger(self._evaluation_db_path).record_decision(
                    record
                )
                decision_id = receipt.decision_id
            except Exception:
                log.warning(
                    "evaluation ledger write failed; JSONL decision remains durable",
                    exc_info=True,
                )
            return PersistenceReceipt(decision_id=decision_id, durable=True)


def _handle_symbol_scan_result(
    *,
    symbol,
    result,
    pending_symbols,
    evaluation_ledger,
    scan_run,
    scan_journal_id,
):
    if result.failure is not None:
        error = RuntimeError(result.failure.message)
        if evaluation_ledger is not None and scan_run is not None:
            try:
                evaluation_ledger.record_scan_symbol(
                    scan_run.run_id,
                    symbol,
                    error=error,
                )
            except Exception:
                log.warning(
                    "failed to record scan failure for %s",
                    symbol,
                    exc_info=True,
                )
        log.warning(
            "run failed for %s, continuing batch",
            symbol,
            exc_info=True,
        )
        _record_scan_event(
            {
                "event": "symbol_failed",
                "run_id": scan_journal_id,
                "ledger_run_id": (
                    scan_run.run_id if scan_run is not None else None
                ),
                "symbol": symbol,
                "error_type": result.failure.error_type,
                "reason": result.failure.message,
                "failure_stage": result.failure.stage,
                "failure_durable": result.failure.durable,
            }
        )
        pending_symbols.discard(symbol)
        return

    log.info("final[%s]: %s", symbol, result.record.get("proposal"))
    if evaluation_ledger is not None and scan_run is not None:
        try:
            evaluation_ledger.record_scan_symbol(
                scan_run.run_id,
                symbol,
                decision_id=result.decision_id,
            )
        except Exception:
            log.warning(
                "failed to record scan completion for %s",
                symbol,
                exc_info=True,
            )
    _record_scan_event(
        {
            "event": "symbol_completed",
            "run_id": scan_journal_id,
            "ledger_run_id": (
                scan_run.run_id if scan_run is not None else None
            ),
            "symbol": symbol,
            "decision_id": result.decision_id,
            "elapsed_ms": round(result.elapsed_ms, 3),
        }
    )
    pending_symbols.discard(symbol)


def run_batch_symbol_scans(
    symbols,
    *,
    scan_engine,
    make_request,
    pending_symbols,
    evaluation_ledger,
    scan_run,
    scan_journal_id,
    concurrency=None,
    scan_delay_seconds=1.0,
):
    """Scan symbols with bounded concurrency; fall back to serial on NSE pressure."""
    workers = NSE_SCAN_CONCURRENCY if concurrency is None else max(1, int(concurrency))
    force_serial = workers <= 1
    remaining = list(symbols)
    first = True

    while remaining:
        if force_serial:
            chunk = [remaining.pop(0)]
            if not first:
                time.sleep(scan_delay_seconds)
            first = False
            symbol = chunk[0]
            result = scan_engine.scan(make_request(symbol))
            _handle_symbol_scan_result(
                symbol=symbol,
                result=result,
                pending_symbols=pending_symbols,
                evaluation_ledger=evaluation_ledger,
                scan_run=scan_run,
                scan_journal_id=scan_journal_id,
            )
            continue

        if not first:
            time.sleep(scan_delay_seconds)
        first = False
        chunk = remaining[:workers]
        remaining = remaining[workers:]
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            futures = {
                pool.submit(scan_engine.scan, make_request(symbol)): symbol
                for symbol in chunk
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    log.warning(
                        "parallel scan worker crashed for %s; continuing",
                        symbol,
                        exc_info=True,
                    )
                    result = SimpleNamespace(
                        failure=ScanFailure(
                            stage="scan_engine",
                            error_type=type(error).__name__,
                            message=str(error)[:500],
                            durable=False,
                        ),
                        record={},
                        decision_id=None,
                        elapsed_ms=0.0,
                    )
                    force_serial = True
                else:
                    if _indicates_nse_pressure(result):
                        force_serial = True
                        log.warning(
                            "NSE pressure while scanning %s; "
                            "falling back to serial for remaining symbols",
                            symbol,
                        )
                _handle_symbol_scan_result(
                    symbol=symbol,
                    result=result,
                    pending_symbols=pending_symbols,
                    evaluation_ledger=evaluation_ledger,
                    scan_run=scan_run,
                    scan_journal_id=scan_journal_id,
                )
        if force_serial and remaining:
            log.info(
                "batch concurrency disabled; %d symbol(s) left on serial path",
                len(remaining),
            )


def create_scan_engine(
    *,
    trade_log_path=None,
    evaluation_db_path=None,
):
    trade_log_path = trade_log_path or TRADE_LOG_PATH
    evaluation_db_path = evaluation_db_path or EVALUATION_DB_PATH
    model = active_model_config()
    return ScanEngine(
        ProductionScanAdapter(),
        ProductionDecisionStore(trade_log_path, evaluation_db_path),
        fallback_policy=PolicyIdentity(
            version=NSE_POLICY_VERSION,
            technical_policy_id=NSE_TECHNICAL_POLICY_ID,
            technical_policy_fingerprint=TECHNICAL_POLICY.fingerprint,
            fundamental_policy_version=FUNDAMENTAL_POLICY_VERSION,
        ),
        fallback_model=ModelIdentity(
            backend=str(model.get("backend") or "unknown"),
            name=model.get("name"),
            max_tokens=model.get("max_tokens"),
            fundamental_max_tokens=model.get(
                "fundamental_max_tokens",
                model.get("max_tokens"),
            ),
        ),
    )


def run(request: ScanRequest):
    """Compatibility entry point returning the typed result, not mutable workflow state."""
    return create_scan_engine().scan(request)


def _initial_scan_state(request):
    return {
        "symbol": request.symbol,
        "principal": request.principal,
        "scan_label": request.scan_label,
        "scan_purpose": request.purpose.value,
        "max_loss_pct": request.max_loss_pct,
        "max_allocation_pct": request.max_allocation_pct,
        "atr_stop_multiple": request.atr_stop_multiple,
        "reward_risk_ratio": request.reward_risk_ratio,
        "iters": 0,
        "quote": None,
        "hist": None,
        "hist_multi": None,
        "benchmark_daily": None,
        "market_snapshot": None,
        "fundamental_snapshot": None,
        "delivery_trend": None,
        "circuit_context": None,
        "technical_indicators": None,
        "technical_assessment": None,
        "technical_fact_ledger": None,
        "technical_explanation": None,
        "technical_explanation_meta": None,
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
        "disposition": None,
        "decision_reason": None,
        "proposal": None,
        "decision_id": None,
    }


def _timing_stage(node):
    return (
        "disposition"
        if node in {"propose", "flag_review", "abort"}
        else node
    )


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

    symbols = list(dict.fromkeys(symbols))
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
    _recover_stale_scan_events()
    scan_started_at_value = now_ist()
    scan_started_at = scan_started_at_value.isoformat()
    scan_journal_id = f"{NSE_SCAN_LABEL}:{scan_started_at}"
    evaluation_ledger = None
    scan_run = None
    try:
        evaluation_ledger = EvaluationLedger(EVALUATION_DB_PATH)
        evaluation_ledger.finalize_stale_scan_runs(
            (
                scan_started_at_value
                - timedelta(seconds=SCAN_RUN_STALE_AFTER_SECONDS)
            ).isoformat()
        )
        scan_run = evaluation_ledger.start_scan_run(
            NSE_SCAN_LABEL,
            symbols,
            NSE_POLICY_VERSION,
            started_at=scan_started_at,
        )
    except Exception:
        log.warning(
            "scan accounting unavailable; decisions retain the JSONL fallback",
            exc_info=True,
        )
    _record_scan_event(
        {
            "event": "scan_started",
            "run_id": scan_journal_id,
            "ledger_run_id": scan_run.run_id if scan_run is not None else None,
            "scan_label": NSE_SCAN_LABEL,
            "policy_version": NSE_POLICY_VERSION,
            "requested_symbols": symbols,
        }
    )
    pending_symbols = set(symbols)
    scan_engine = create_scan_engine()

    def make_request(symbol):
        return ScanRequest(
            symbol=symbol,
            principal=principal,
            scan_label=NSE_SCAN_LABEL,
            purpose=ScanPurpose.BATCH,
            max_allocation_pct=max_allocation_pct,
            max_loss_pct=max_loss_pct,
            atr_stop_multiple=atr_stop_multiple,
            reward_risk_ratio=reward_risk_ratio,
            run_id=scan_journal_id,
        )

    try:
        run_batch_symbol_scans(
            symbols,
            scan_engine=scan_engine,
            make_request=make_request,
            pending_symbols=pending_symbols,
            evaluation_ledger=evaluation_ledger,
            scan_run=scan_run,
            scan_journal_id=scan_journal_id,
            concurrency=NSE_SCAN_CONCURRENCY,
            scan_delay_seconds=scan_delay_seconds,
        )
    finally:
        for symbol in sorted(pending_symbols):
            _record_scan_event(
                {
                    "event": "symbol_failed",
                    "run_id": scan_journal_id,
                    "ledger_run_id": (
                        scan_run.run_id if scan_run is not None else None
                    ),
                    "symbol": symbol,
                    "error_type": "IncompleteScan",
                    "reason": "Scan ended before this symbol completed",
                }
            )
        if evaluation_ledger is not None and scan_run is not None:
            try:
                evaluation_ledger.finalize_scan_run(scan_run.run_id)
            except Exception:
                log.warning("failed to finalize scan accounting", exc_info=True)
        _record_scan_event(
            {
                "event": "scan_finished",
                "run_id": scan_journal_id,
                "ledger_run_id": (
                    scan_run.run_id if scan_run is not None else None
                ),
                "scan_concurrency": NSE_SCAN_CONCURRENCY,
            }
        )
