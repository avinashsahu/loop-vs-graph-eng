from dotenv import load_dotenv

# Must run before `notify` is imported below -- it reads EMAIL_ENABLED/SMTP_*/etc at
# module level, and nothing else in this script's import chain was loading .env before.
load_dotenv()

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

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
    latest_by_symbol = {}
    for record in read_jsonl_records(TRADE_LOG_PATH):
        if record.get("scan_label") == run_id and record.get("symbol"):
            latest_by_symbol[record["symbol"]] = record
    return list(latest_by_symbol.values())


def _format_indicators(indicators):
    if not indicators:
        return "  (no indicators)"
    return "\n".join(
        f"  {tf}: close={ind['close']} SMA20={ind['sma20']} SMA50={ind['sma50']} "
        f"RSI14={ind['rsi14']} MACD={ind['macd']} MACD_signal={ind['macd_signal']} MACD_hist={ind['macd_hist']}"
        for tf, ind in indicators.items()
    )


def _format_technical_explanation(record):
    explanation = record.get("technical_explanation")
    if not explanation:
        return "  (model explanation unavailable; deterministic verdict retained)"
    lines = [
        f"  {explanation['verdict']}: {explanation['summary']}",
    ]
    for label, key in (
        ("Drivers", "drivers"),
        ("Conflicts", "conflicts"),
        ("Neutral context", "neutral_context"),
        ("Data notes", "data_notes"),
    ):
        values = explanation.get(key) or []
        if key == "drivers":
            values = [
                f"{item['fact_id']}: {item['statement']}"
                for item in values
            ]
        if values:
            lines.append(f"  {label}: {'; '.join(values)}")
    return "\n".join(lines)


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
            "Technical explanation:",
            _format_technical_explanation(record),
            f"Fundamental verdict: {record.get('fundamental_verdict')}",
            f"Risk verdict: {record.get('risk_verdict')}",
            f"Sentiment verdict: {record.get('sentiment_verdict')}",
            "",
            f"Proposal: {record.get('proposal')}",
        ]
    )


_SLACK_STATUS_EMOJI = {
    "proposed": ":large_blue_circle:",
    "flagged_for_review": ":warning:",
}


def _verdict_label(value, fallback="not_run"):
    if not value:
        return fallback
    return str(value).split(":", 1)[0].split(" ", 1)[0].upper()


def _technical_gate(record):
    assessment = record.get("technical_assessment") or {}
    evidence = assessment.get("evidence") or {}
    return evidence.get("verdict") or _verdict_label(
        record.get("technical_verdict"),
        "UNKNOWN",
    )


def _signed(value, digits=2):
    if value is None:
        return None
    number = float(value)
    if abs(number) < 0.5 * (10 ** -digits):
        number = 0.0
    return f"{number:+.{digits}f}"


def _short_date(value, *, month_only=False):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return parsed.strftime("%b %Y" if month_only else "%d %b %Y").lstrip("0")


def _slack_technical_summary(record):
    """Prefer the validated model summary; otherwise trim deterministic detail."""
    explanation = record.get("technical_explanation")
    if explanation:
        return explanation["summary"]
    verdict = record.get("technical_verdict")
    if not verdict:
        return verdict
    return verdict.split("; per-timeframe")[0]


def _technical_number_lines(record):
    facts = (record.get("technical_fact_ledger") or {}).get("facts") or {}
    decision = facts.get("TA_DECISION") or {}
    trend = facts.get("TA_TREND") or {}
    momentum = facts.get("TA_MOMENTUM") or {}
    relative_strength = facts.get("TA_RELATIVE_STRENGTH") or {}
    daily = facts.get("TA_DAILY_CONTEXT") or {}
    timeframes = (facts.get("TA_TIMEFRAMES") or {}).get("timeframes") or {}
    lines = []

    if decision:
        engaged = int(decision.get("engaged_families") or 0)
        ratio = float(decision.get("confluence_ratio") or 0.0)
        positive = int(round(engaged * ratio))
        parts = [
            f"score {_signed(decision.get('score'), 3)}",
            f"confluence {positive}/{engaged} engaged families",
        ]
        if trend.get("score") is not None:
            parts.append(f"trend {_signed(trend['score'], 3)}")
        if momentum.get("score") is not None:
            parts.append(f"momentum {_signed(momentum['score'], 3)}")
        if relative_strength.get("relative_return_pct") is not None:
            parts.append(
                f"RS20D {_signed(relative_strength['relative_return_pct'])}pp "
                f"vs {relative_strength.get('benchmark_symbol') or 'benchmark'}"
            )
        lines.append("*TA numbers:* " + " • ".join(parts))

    if daily:
        parts = []
        if daily.get("close") is not None:
            parts.append(f"Close ₹{float(daily['close']):.2f}")
        if daily.get("sma20") is not None:
            parts.append(f"SMA20 {float(daily['sma20']):.2f}")
        if daily.get("sma50") is not None:
            parts.append(f"SMA50 {float(daily['sma50']):.2f}")
        if daily.get("rsi14") is not None:
            parts.append(f"RSI14 {float(daily['rsi14']):.1f}")
        if daily.get("macd_hist") is not None:
            parts.append(f"MACD hist {_signed(daily['macd_hist'])}")
        if daily.get("atr_pct") is not None:
            parts.append(f"ATR {float(daily['atr_pct']):.2f}%")
        if parts:
            lines.append("*Daily TA bar:* " + " • ".join(parts))

    timeframe_parts = []
    for timeframe in ("D", "30", "15", "5"):
        values = timeframes.get(timeframe) or {}
        trend_score = _signed(values.get("trend_score"))
        momentum_score = _signed(values.get("momentum_score"))
        if trend_score is None or momentum_score is None:
            continue
        label = timeframe if timeframe == "D" else f"{timeframe}m"
        timeframe_parts.append(f"{label} {trend_score}/{momentum_score}")
    if timeframe_parts:
        lines.append(
            "*Trend/Momentum by timeframe:* " + " • ".join(timeframe_parts)
        )
    return lines


def _technical_context_lines(record):
    explanation = record.get("technical_explanation") or {}
    lines = []
    summary = _slack_technical_summary(record)
    if summary:
        lines.append(f"*Interpretation:* {summary}")
    conflicts = explanation.get("conflicts") or []
    if conflicts:
        lines.append("*Watch:* " + " • ".join(conflicts[:3]))
    return lines


def _participation_line(record):
    facts = (record.get("technical_fact_ledger") or {}).get("facts") or {}
    participation = facts.get("TA_PARTICIPATION") or {}
    if not participation:
        return None
    state = participation.get("participation_state")
    state_label = {
        "available_neutral": (
            "Neutral — data is present, but directional confirmation "
            "conditions were not met"
        ),
        "possible_accumulation": "Possible accumulation",
        "possible_distribution": "Possible distribution",
        "illiquid": "Illiquid by policy",
        "unavailable": "Unavailable",
    }.get(state, str(state or "Unavailable").replace("_", " ").title())
    parts = [state_label]
    recent_pct = participation.get("recent_avg_delivery_pct")
    baseline_pct = participation.get("baseline_avg_delivery_pct")
    if recent_pct is not None and baseline_pct is not None:
        parts.append(
            f"delivery {float(recent_pct):.2f}% vs "
            f"{float(baseline_pct):.2f}% baseline"
        )
    recent_volume = participation.get("recent_avg_delivery_volume")
    baseline_volume = participation.get("baseline_avg_delivery_volume")
    if recent_volume is not None and baseline_volume is not None:
        parts.append(
            f"volume {float(recent_volume):,.0f} vs "
            f"{float(baseline_volume):,.0f}"
        )
    if participation.get("total_volume_expanded") is False:
        parts.append("total volume not expanded")
    elif participation.get("total_volume_expanded") is True:
        parts.append("total volume expanded")
    return "*Delivery:* " + " • ".join(parts)


def _technical_freshness(record):
    facts = (record.get("technical_fact_ledger") or {}).get("facts") or {}
    quality = facts.get("TA_DATA_QUALITY") or {}
    daily = (quality.get("timeframes") or {}).get("D") or {}
    price_session = str(daily.get("latest_complete_bar") or "")[:10]
    delivery = quality.get("delivery") or {}
    delivery_session = delivery.get("latest_session")
    freshness = delivery.get("freshness")
    benchmark = quality.get("benchmark") or {}

    shareholding = record.get("shareholding_history") or {}
    parts = []
    if price_session:
        parts.append(f"bars {_short_date(price_session)}")
    if delivery_session:
        delivery_text = f"delivery {_short_date(delivery_session)}"
        if freshness == "expected_prior_completed_session":
            delivery_text += " (latest prior session)"
        elif freshness == "stale":
            delivery_text += " :warning: stale"
        parts.append(delivery_text)
    if benchmark.get("sessions_aligned") is True:
        parts.append("benchmark aligned")
    if shareholding.get("periods_available"):
        shareholding_text = (
            f"shareholding {shareholding['periods_available']} quarters"
        )
        if shareholding.get("latest_period"):
            shareholding_text += (
                f" through {_short_date(shareholding['latest_period'], month_only=True)}"
            )
        if shareholding.get("complete"):
            shareholding_text += " (complete)"
        parts.append(shareholding_text)
    return " • ".join(parts)


def _fundamental_summary(record):
    assessment = record.get("fundamental_assessment") or {}
    verdict = assessment.get("verdict") or _verdict_label(
        record.get("fundamental_verdict")
    )
    scope_note = _fundamental_scope_note(record)
    governance_note = _governance_coverage_note(record)
    if verdict == "PASS":
        return (
            "No policy red flag in the available required evidence "
            f"(not an overall quality rating).{scope_note}{governance_note}"
        )
    missing = assessment.get("missing") or []
    if verdict == "REVIEW" and missing:
        field_labels = {
            "assets": "assets",
            "equity": "equity",
            "total_debt": "debt",
            "debt_to_equity": "D/E",
            "funding_leverage": "funding leverage",
            "impairment_or_credit_cost": "impairment or credit cost",
            "operating_cash_flow": "operating cash flow",
            "return_on_equity_pct": "ROE",
        }
        annual_fields = {
            "assets",
            "equity",
            "total_debt",
            "debt_to_equity",
            "funding_leverage",
            "operating_cash_flow",
            "return_on_equity_pct",
        }
        descriptions = []
        for item in missing:
            field, _, period = str(item).partition(":")
            label = field_labels.get(field, field.replace("_", " "))
            if period:
                if period == "latest_annual":
                    period_label = "latest annual filing"
                elif field in annual_fields and period.endswith("-03-31"):
                    period_label = f"FY{period[:4]}"
                else:
                    period_label = _short_date(period, month_only=True)
                label = f"{label} ({period_label})"
            if label not in descriptions:
                descriptions.append(label)
        return (
            f"REVIEW — missing {', '.join(descriptions)}."
            f"{scope_note}{governance_note}"
        )
    summary = assessment.get("summary")
    actionable = _actionable_governance_note(record, assessment)
    result = f"{verdict} — {summary}" if summary else str(verdict)
    if actionable:
        result = f"{result} {actionable}"
    return f"{result}{scope_note}{governance_note}"


def _actionable_governance_note(record, assessment):
    reason = assessment.get("reason_code")
    if reason not in {
        "GOVERNANCE_DISCLOSURE_CAUTION",
        "PROMOTER_ENCUMBRANCE_CAUTION",
    }:
        return ""
    evidence_ids = assessment.get("evidence_ids") or []
    facts = (
        (record.get("fundamental_evidence") or {}).get("facts") or []
    )
    details = []
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("id") not in evidence_ids:
            continue
        if fact.get("kind") == "governance_exception":
            details.append(
                str(fact.get("code") or fact.get("detail") or "governance exception")
                .replace("_", " ")
            )
        elif fact.get("kind") == "calculated_shareholding_trend":
            changes = fact.get("changes_bps") or {}
            if changes.get("promoter_encumbered_qoq") is not None:
                details.append(
                    f"promoter encumbrance QoQ {changes['promoter_encumbered_qoq']} bps"
                )
            if changes.get("promoter_encumbered_4q") is not None:
                details.append(
                    f"promoter encumbrance 4Q {changes['promoter_encumbered_4q']} bps"
                )
    if not details:
        return f"Actionable: {reason.replace('_', ' ').title()}."
    return "Actionable: " + "; ".join(details[:3]) + "."


def _governance_coverage_note(record):
    facts = (record.get("fundamental_evidence") or {}).get("facts") or []
    notes = []
    coverage = next(
        (
            fact
            for fact in facts
            if isinstance(fact, dict) and fact.get("kind") == "governance_coverage"
        ),
        None,
    )
    if coverage is not None and coverage.get("status") != "ready":
        notes.append(
            f"Governance coverage: {coverage.get('status') or 'pending'} (optional)."
        )
    research = next(
        (
            fact
            for fact in facts
            if isinstance(fact, dict)
            and fact.get("kind") == "document_research_coverage"
        ),
        None,
    )
    if research is not None and research.get("status") != "ready":
        notes.append(
            "Additional research: "
            f"{research.get('status') or 'pending'} (optional)."
        )
    elif research is not None:
        counts = research.get("document_counts") or {}
        ready = counts.get("ready")
        if ready:
            notes.append(f"Additional research: {ready} warmed document(s).")
    return (" " + " ".join(notes)) if notes else ""


def _fundamental_scope_note(record):
    history = (
        (record.get("fundamental_evidence") or {}).get("financial_history")
        or {}
    )
    scope = history.get("selected_scope")
    if not scope:
        return ""
    reason = {
        "regulated_entity_metrics": "regulated entity metrics",
        "group_economics": "group economics",
        "only_reported_scope": "only reported scope",
        "standalone_unavailable_fallback": "standalone unavailable",
        "consolidated_unavailable_fallback": "consolidated unavailable",
    }.get(
        history.get("scope_selection_reason"),
        str(history.get("scope_selection_reason") or "explicit scope policy")
        .replace("_", " "),
    )
    alternatives = [
        item
        for item in history.get("available_scopes", [])
        if item != scope
    ]
    alternative_note = (
        f"; {', '.join(alternatives)} retained"
        if alternatives
        else ""
    )
    return f" Scope: {scope} ({reason}{alternative_note})."


def _risk_plan_lines(record):
    plan = record.get("risk_plan") or {}
    if not plan:
        return []
    principal = float(record.get("principal") or 0.0)
    actual_loss_pct = (
        float(plan["max_loss_at_stop"]) / principal * 100
        if principal
        else 0.0
    )
    binding = str(plan.get("binding_constraint") or "policy limit")
    if binding == "allocation_cap":
        binding = (
            f"{float(record.get('max_allocation_pct') or 0):g}% allocation cap"
        )
    elif binding == "risk_budget":
        binding = f"{float(record.get('max_loss_pct') or 0):g}% loss budget"
    return [
        (
            "*Position plan — manual, not placed:* Max "
            f"*{plan['shares']} shares* • scan-time quote ₹{plan['entry_price']:.2f} • "
            f"SL ₹{plan['stop_price']:.2f} • "
            f"TP ₹{plan['target_price']:.2f} "
            f"({plan['reward_risk_ratio']:.2f}R)"
        ),
        (
            "*Exposure:* "
            f"₹{plan['capital_required']:.0f} capital • "
            f"₹{plan['max_loss_at_stop']:.0f} loss at SL "
            f"({actual_loss_pct:.2f}% of principal) • "
            f"sized by {binding}"
        ),
    ]


def _screen_check_line(record):
    fundamental = _verdict_label(
        (record.get("fundamental_assessment") or {}).get("verdict")
        or record.get("fundamental_verdict")
    )
    fundamental_label = {
        "PASS": "NO POLICY RED FLAG",
        "REVIEW": "REVIEW",
        "REJECT": "RED FLAG",
    }.get(fundamental, "NOT EVALUATED")
    risk_label = (
        "COMPUTED"
        if record.get("risk_plan")
        else "NOT COMPUTED"
        if not record.get("risk_verdict")
        else "BLOCKED"
    )
    daily_move = _verdict_label(record.get("sentiment_verdict"))
    daily_move_label = {
        "GOOD": "WITHIN LIMIT",
        "REVIEW": "REVIEW",
        "REJECT": "OUTSIDE LIMIT",
    }.get(daily_move, "NOT EVALUATED")
    technical = (
        "QUALIFIED" if _technical_gate(record) == "GOOD" else "FILTERED"
    )
    return (
        "*Screen checks:* "
        f"TA {technical} • Fundamentals {fundamental_label} • "
        f"Position sizing {risk_label} • Daily move {daily_move_label}"
    )


def format_symbol_section_slack(record):
    emoji = _SLACK_STATUS_EMOJI.get(record["status"], "")
    reason = record.get("decision_reason") or {}
    disposition = (
        "CANDIDATE"
        if record.get("disposition") == "PROPOSE"
        else record.get("disposition") or "UNKNOWN"
    )
    lines = [
        (
            f"{emoji} *{record['symbol']}* — "
            f"*{disposition}*\n"
            f"_{record.get('company_name') or 'company name unavailable'}_"
        ),
        _screen_check_line(record),
        *_technical_number_lines(record),
        *_technical_context_lines(record),
    ]
    participation = _participation_line(record)
    if participation:
        lines.append(participation)
    freshness = _technical_freshness(record)
    if freshness:
        lines.append(f"*Data:* {freshness}")
    lines.append(f"*Fundamentals:* {_fundamental_summary(record)}")
    lines.extend(_risk_plan_lines(record))
    if record["status"] == "flagged_for_review":
        lines.append(
            "*Action:* Verify the missing evidence before considering this "
            f"candidate ({reason.get('code') or 'manual review'})."
        )
    return "\n".join(lines)


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


def _scan_scope(run_id):
    normalized = str(run_id).lower()
    for slug, title in (
        ("nifty-total-mkt", "NIFTY TOTAL MKT"),
        ("nifty-next-50", "NIFTY NEXT 50"),
        ("nifty-midcap-50", "NIFTY MIDCAP 50"),
        ("nifty-50", "NIFTY 50"),
    ):
        if slug in normalized:
            return title
    return "NSE EQUITIES"


def _scan_session(records):
    for record in records:
        facts = (record.get("technical_fact_ledger") or {}).get("facts") or {}
        daily = (
            ((facts.get("TA_DATA_QUALITY") or {}).get("timeframes") or {})
            .get("D")
            or {}
        )
        session = _short_date(daily.get("latest_complete_bar"))
        if session:
            return session
    return None


def _filtered_breakdown(records):
    counts = defaultdict(int)
    for record in records:
        if record.get("status") != "aborted":
            continue
        stage = (record.get("decision_reason") or {}).get("stage") or "other"
        counts[stage] += 1
    labels = {
        "technical": "TA",
        "fundamental": "fundamentals",
        "risk": "position",
        "sentiment": "daily move",
        "market_data": "market data",
    }
    priority = {
        "market_data": 0,
        "technical": 1,
        "fundamental": 2,
        "risk": 3,
        "sentiment": 4,
    }
    return ", ".join(
        f"{count} {labels.get(stage, stage.replace('_', ' '))}"
        for stage, count in sorted(
            counts.items(), key=lambda item: priority.get(item[0], 99)
        )
    )


def build_slack_digest(run_id):
    records = _load_records(run_id)
    proposed, flagged, aborted_count, total = _summarize(records)
    session = _scan_session(records)
    breakdown = _filtered_breakdown(records)
    session_suffix = f" • {session}" if session else ""
    breakdown_suffix = f" ({breakdown})" if breakdown else ""

    header = (
        f":bar_chart: *NSE Scan — {_scan_scope(run_id)}{session_suffix}*\n"
        f"*{total}* scanned • *{len(proposed)}* candidates • "
        f"*{len(flagged)}* review • *{aborted_count}* filtered out"
        f"{breakdown_suffix}\n"
        "_Rules determine the screen result; the written interpretation only "
        "explains it. TA scores are policy sums, not probabilities._\n"
        "_Research only. No broker connection and no order was placed._"
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
