from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable

from fundamental_evidence import FundamentalEvidence
from llm import FundamentalAssessment
from qualitative_policy import QUALITATIVE_REJECT_REASON_CODES

FUNDAMENTAL_POLICY_VERSION = "fundamental-sector-policy-v5"
PROMOTER_ENCUMBRANCE_QOQ_BPS = 200
PROMOTER_ENCUMBRANCE_4Q_BPS = 500

_NON_FINANCIAL_QUARTER_FIELDS = (
    "revenue",
    "operating_profit",
    "pat",
    "operating_margin_pct",
)
_NON_FINANCIAL_ANNUAL_FIELDS = (
    "assets",
    "equity",
    "total_debt",
    "debt_to_equity",
    "operating_cash_flow",
    "return_on_equity_pct",
)
_BANK_FIELDS = (
    "net_interest_income",
    "operating_profit",
    "provisions",
    "pat",
    "gross_npa_pct",
    "net_npa_pct",
    "return_on_assets_pct",
)
_NBFC_FIELDS = (
    "revenue",
    "finance_cost",
    "pat",
)
_INSURANCE_FIELDS = (
    "revenue",
    "operating_profit",
    "pat",
)


@dataclass(frozen=True)
class FundamentalDecision:
    verdict: str
    reason_code: str
    reason: str
    evidence_ids: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    profile: str | None = None
    subtype: str | None = None
    model_invoked: bool = False
    policy_version: str = FUNDAMENTAL_POLICY_VERSION

    @property
    def summary(self) -> str:
        return f"{self.verdict}: {self.reason}"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "summary": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "missing": list(self.missing),
            "checks": list(self.checks),
            "profile": self.profile,
            "subtype": self.subtype,
            "model_invoked": self.model_invoked,
            "policy_version": self.policy_version,
        }


def evaluate_fundamental_research(
    evidence: FundamentalEvidence,
    qualitative_interpreter: Callable[
        [str, tuple[str, ...]], FundamentalAssessment
    ],
) -> FundamentalDecision:
    """Apply deterministic coverage/numeric policy around a bounded LLM interpretation."""
    coverage = evidence.payload.get("coverage")
    if not isinstance(coverage, dict) or not coverage.get("complete"):
        missing = (
            coverage.get("missing", ())
            if isinstance(coverage, dict)
            else ("fundamental_coverage",)
        )
        return _review(None, None, tuple(missing) or ("fundamental_coverage",))

    history = evidence.payload.get("financial_history")
    if not isinstance(history, dict) or history.get("status") != "ready":
        return _review(
            history.get("profile") if isinstance(history, dict) else None,
            history.get("subtype") if isinstance(history, dict) else None,
            ("financial_history",),
        )

    profile = history.get("profile")
    subtype = history.get("subtype")
    freshness = evidence.payload.get("freshness")
    if not isinstance(freshness, dict):
        return _review(profile, subtype, ("financial_freshness",))
    if freshness.get("financial_stale") is not False:
        return _review(profile, subtype, ("financial_history_stale",))

    periods = history.get("periods")
    periods = periods if isinstance(periods, list) else []
    missing = _coverage_missing(profile, subtype, periods)
    if missing:
        return _review(profile, subtype, missing)

    failed_checks = _failed_numeric_checks(profile, subtype, periods, evidence)
    if failed_checks:
        reason_code = (
            "PROMOTER_OR_DILUTION"
            if "PROMOTER_DILUTION_ABOVE_POLICY" in failed_checks
            else "PEER_OR_EARNINGS_WEAKNESS"
        )
        return FundamentalDecision(
            verdict="REJECT",
            reason_code=reason_code,
            reason=(
                "Deterministic financial policy found a material red flag: "
                + ", ".join(failed_checks)
                + "."
            ),
            checks=failed_checks,
            profile=profile,
            subtype=subtype,
        )

    disclosure_rejects = _disclosure_policy_facts(evidence, "REJECT")
    if disclosure_rejects:
        reason_code = disclosure_rejects[0].get(
            "policy_reason_code",
            "ADVERSE_CORPORATE_EVENT",
        )
        return FundamentalDecision(
            verdict="REJECT",
            reason_code=reason_code,
            reason=(
                "Deterministic disclosure policy found a material red flag: "
                + ", ".join(_disclosure_labels(disclosure_rejects))
                + "."
            ),
            evidence_ids=tuple(
                fact["id"] for fact in disclosure_rejects[:3]
            ),
            checks=tuple(
                f"DISCLOSURE_{_disclosure_label(fact).upper()}"
                for fact in disclosure_rejects[:3]
            ),
            profile=profile,
            subtype=subtype,
        )

    disclosure_reviews = _disclosure_policy_facts(evidence, "REVIEW")
    if disclosure_reviews:
        return FundamentalDecision(
            verdict="REVIEW",
            reason_code=disclosure_reviews[0].get(
                "policy_reason_code",
                "MATERIAL_DISCLOSURE_CAUTION",
            ),
            reason=(
                "Structured NSE disclosure requires manual review: "
                + ", ".join(_disclosure_labels(disclosure_reviews))
                + "."
            ),
            evidence_ids=tuple(
                fact["id"] for fact in disclosure_reviews[:3]
            ),
            checks=tuple(
                f"DISCLOSURE_{_disclosure_label(fact).upper()}"
                for fact in disclosure_reviews[:3]
            ),
            profile=profile,
            subtype=subtype,
        )

    encumbrance_review = _encumbrance_policy(evidence)
    if encumbrance_review is not None:
        return encumbrance_review

    qualitative_ids = evidence.qualitative_ids
    if not qualitative_ids:
        return _pass(profile, subtype, model_invoked=False)

    try:
        assessment = qualitative_interpreter(evidence.prompt(), qualitative_ids)
    except Exception:
        return FundamentalDecision(
            verdict="REVIEW",
            reason_code="INSUFFICIENT_EVIDENCE",
            reason="Qualitative evidence interpretation failed closed.",
            missing=("qualitative_interpretation",),
            profile=profile,
            subtype=subtype,
            model_invoked=True,
        )

    if any(item not in qualitative_ids for item in assessment.evidence_ids):
        return FundamentalDecision(
            verdict="REVIEW",
            reason_code="INSUFFICIENT_EVIDENCE",
            reason="Qualitative interpretation cited evidence outside the supplied set.",
            missing=("invalid_qualitative_citation",),
            profile=profile,
            subtype=subtype,
            model_invoked=True,
        )

    if assessment.reason_code == "NO_MATERIAL_RED_FLAG":
        if assessment.verdict != "PASS":
            return _unmapped_qualitative(profile, subtype, assessment)
        return _pass(
            profile,
            subtype,
            model_invoked=True,
            evidence_ids=assessment.evidence_ids,
        )
    if assessment.reason_code == "INSUFFICIENT_EVIDENCE":
        if assessment.verdict != "REVIEW":
            return _unmapped_qualitative(profile, subtype, assessment)
        return FundamentalDecision(
            verdict="REVIEW",
            reason_code="INSUFFICIENT_EVIDENCE",
            reason="Qualitative disclosures require human interpretation.",
            evidence_ids=assessment.evidence_ids,
            missing=assessment.missing,
            checks=("QUALITATIVE_EVIDENCE_AMBIGUOUS",),
            profile=profile,
            subtype=subtype,
            model_invoked=True,
        )
    if assessment.reason_code in QUALITATIVE_REJECT_REASON_CODES:
        if assessment.verdict != "REJECT":
            return _unmapped_qualitative(profile, subtype, assessment)
        return FundamentalDecision(
            verdict="REJECT",
            reason_code=assessment.reason_code,
            reason=(
                "Supplied qualitative evidence supports a material "
                f"{assessment.reason_code.lower()} concern."
            ),
            evidence_ids=assessment.evidence_ids,
            checks=(f"QUALITATIVE_{assessment.reason_code}",),
            profile=profile,
            subtype=subtype,
            model_invoked=True,
        )
    return _unmapped_qualitative(profile, subtype, assessment)


def _unmapped_qualitative(
    profile: str | None,
    subtype: str | None,
    assessment: FundamentalAssessment,
) -> FundamentalDecision:
    return FundamentalDecision(
        verdict="REVIEW",
        reason_code="INSUFFICIENT_EVIDENCE",
        reason="Qualitative interpretation did not map to a known policy category.",
        evidence_ids=assessment.evidence_ids,
        missing=("qualitative_policy_mapping",),
        profile=profile,
        subtype=subtype,
        model_invoked=True,
    )


def _pass(
    profile: str | None,
    subtype: str | None,
    *,
    model_invoked: bool,
    evidence_ids: tuple[str, ...] = (),
) -> FundamentalDecision:
    return FundamentalDecision(
        verdict="PASS",
        reason_code="NO_MATERIAL_RED_FLAG",
        reason=(
            "No material red flag was found in the supplied policy-required evidence; "
            "this is not an assessment of overall company quality."
        ),
        evidence_ids=evidence_ids,
        profile=profile,
        subtype=subtype,
        model_invoked=model_invoked,
    )


def _review(
    profile: str | None,
    subtype: str | None,
    missing: tuple[str, ...] | list[str],
) -> FundamentalDecision:
    return FundamentalDecision(
        verdict="REVIEW",
        reason_code="INSUFFICIENT_EVIDENCE",
        reason="Required profile-specific financial evidence is missing.",
        missing=tuple(missing),
        profile=profile,
        subtype=subtype,
    )


def _coverage_missing(
    profile: str | None, subtype: str | None, periods: list[dict]
) -> tuple[str, ...]:
    if profile == "non_financial":
        missing = _period_fields_missing(
            periods[:4], _NON_FINANCIAL_QUARTER_FIELDS, minimum_periods=4
        )
        annual = [
            period
            for period in periods
            if str(period.get("period_end", "")).endswith("-03-31")
        ][:2]
        missing.extend(
            _period_fields_missing(
                annual, _NON_FINANCIAL_ANNUAL_FIELDS, minimum_periods=2
            )
        )
        return tuple(dict.fromkeys(missing))
    if profile == "banking_nbfc" and subtype == "bank":
        return tuple(
            _period_fields_missing(periods[:4], _BANK_FIELDS, minimum_periods=4)
        )
    if profile == "banking_nbfc" and subtype == "nbfc":
        missing = _period_fields_missing(
            periods[:4],
            _NBFC_FIELDS,
            minimum_periods=4,
        )
        for period in periods[:4]:
            if not (
                _number(period.get("impairment"))
                or _number(period.get("credit_cost"))
            ):
                missing.append(
                    "impairment_or_credit_cost:"
                    f"{period.get('period_end') or 'unknown'}"
                )
        annual = _annual_periods(periods)
        if not annual:
            missing.append("funding_leverage:latest_annual")
        elif not _valid_funding_leverage(annual[0].get("funding_leverage")):
            missing.append(
                f"funding_leverage:{annual[0].get('period_end') or 'latest_annual'}"
            )
        return tuple(dict.fromkeys(missing))
    if profile == "insurance" and subtype in {"life", "general"}:
        return tuple(
            _period_fields_missing(
                periods[:4],
                _INSURANCE_FIELDS,
                minimum_periods=4,
            )
        )
    return ("financial_profile",)


def _period_fields_missing(
    periods: list[dict], fields: tuple[str, ...], *, minimum_periods: int
) -> list[str]:
    missing: list[str] = []
    if len(periods) < minimum_periods:
        missing.append(f"periods:{len(periods)}/{minimum_periods}")
    for period in periods:
        period_end = period.get("period_end") or "unknown"
        for field in fields:
            if not _number(period.get(field)):
                missing.append(f"{field}:{period_end}")
    return missing


def _failed_numeric_checks(
    profile: str | None,
    subtype: str | None,
    periods: list[dict],
    evidence: FundamentalEvidence,
) -> tuple[str, ...]:
    checks: list[str] = []
    latest = periods[0]
    if _value(latest.get("pat")) < 0:
        checks.append("NEGATIVE_LATEST_PAT")

    if profile == "non_financial":
        annual = [
            period
            for period in periods
            if str(period.get("period_end", "")).endswith("-03-31")
        ][:2]
        if any(_value(period.get("debt_to_equity")) > 2.5 for period in annual):
            checks.append("LEVERAGE_ABOVE_POLICY")
        if (
            len(annual) == 2
            and all(_value(period.get("pat")) > 0 for period in annual)
            and all(_value(period.get("operating_cash_flow")) <= 0 for period in annual)
        ):
            checks.append("PROFIT_NOT_BACKED_BY_CASH_FLOW")
        margins = [_value(period.get("operating_margin_pct")) for period in periods[:4]]
        if margins[0] < 0 or margins[0] <= max(margins[1:]) - 8:
            checks.append("MARGIN_DETERIORATION")
    elif subtype == "bank":
        if _value(latest.get("net_npa_pct")) > 3:
            checks.append("NET_NPA_ABOVE_POLICY")
        gross_npa = [_value(period.get("gross_npa_pct")) for period in periods[:4]]
        if gross_npa[0] > 5 and gross_npa[0] >= gross_npa[-1] + 1:
            checks.append("GROSS_NPA_DETERIORATION")
        if _value(latest.get("return_on_assets_pct")) < 0:
            checks.append("NEGATIVE_RETURN_ON_ASSETS")
    elif subtype == "nbfc":
        annual = _annual_periods(periods)
        latest_leverage = (
            annual[0].get("funding_leverage") if annual else None
        )
        if (
            isinstance(latest_leverage, dict)
            and _value(latest_leverage.get("ratio")) > 8
        ):
            checks.append("LEVERAGE_ABOVE_POLICY")
        if _value(latest.get("impairment")) > _value(latest.get("revenue")) * 0.1:
            checks.append("IMPAIRMENT_ABOVE_POLICY")
    elif profile == "insurance":
        if _value(latest.get("revenue")) <= 0:
            checks.append("NON_POSITIVE_PREMIUM")

    trend = next(
        (
            fact
            for fact in evidence.payload.get("facts", [])
            if fact.get("kind") == "calculated_shareholding_trend"
        ),
        None,
    )
    changes = trend.get("changes_bps", {}) if trend else {}
    if _value(changes.get("promoter_4q")) <= -500:
        checks.append("PROMOTER_DILUTION_ABOVE_POLICY")
    return tuple(checks)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _annual_periods(periods: list[dict]) -> list[dict]:
    return [
        period
        for period in periods
        if str(period.get("period_end", "")).endswith("-03-31")
    ][:2]


def _valid_funding_leverage(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and _number(value.get("ratio"))
        and value.get("method")
        in {
            "reported_debt_to_equity",
            "derived_funding_liabilities_to_equity",
        }
        and value.get("balance_sheet_reconciled") is True
        and value.get("funding_components_reconciled") is True
    )


def _disclosure_policy_facts(
    evidence: FundamentalEvidence,
    verdict: str,
) -> list[dict]:
    return [
        fact
        for fact in evidence.payload.get("facts", [])
        if isinstance(fact, dict)
        and fact.get("kind")
        in {
            "material_disclosure",
            "credit_rating_action",
            "governance_exception",
            "document_research_fact",
        }
        and fact.get("policy_verdict") == verdict
    ]


def _encumbrance_policy(
    evidence: FundamentalEvidence,
) -> FundamentalDecision | None:
    trend = next(
        (
            fact
            for fact in evidence.payload.get("facts", [])
            if fact.get("kind") == "calculated_shareholding_trend"
        ),
        None,
    )
    if not isinstance(trend, dict):
        return None
    changes = trend.get("changes_bps") or {}
    qoq = changes.get("promoter_encumbered_qoq")
    four_q = changes.get("promoter_encumbered_4q")
    checks = []
    if _number(qoq) and qoq >= PROMOTER_ENCUMBRANCE_QOQ_BPS:
        checks.append("PROMOTER_ENCUMBRANCE_QOQ_ABOVE_POLICY")
    if _number(four_q) and four_q >= PROMOTER_ENCUMBRANCE_4Q_BPS:
        checks.append("PROMOTER_ENCUMBRANCE_4Q_ABOVE_POLICY")
    if not checks:
        return None
    history = evidence.payload.get("financial_history")
    profile = history.get("profile") if isinstance(history, dict) else None
    subtype = history.get("subtype") if isinstance(history, dict) else None
    return FundamentalDecision(
        verdict="REVIEW",
        reason_code="PROMOTER_ENCUMBRANCE_CAUTION",
        reason=(
            "Material promoter encumbrance increase requires review: "
            + ", ".join(checks)
            + "."
        ),
        evidence_ids=("SHAREHOLDING_TREND",),
        checks=tuple(checks),
        profile=profile,
        subtype=subtype,
    )


def _disclosure_labels(facts: list[dict]) -> list[str]:
    return list(dict.fromkeys(_disclosure_label(fact) for fact in facts[:3]))


def _disclosure_label(fact: dict) -> str:
    return str(
        fact.get("event_type")
        or fact.get("code")
        or fact.get("action_direction")
        or "material_event"
    ).replace("-", "_")


def _value(value: object) -> float:
    return float(value) if _number(value) else 0.0
