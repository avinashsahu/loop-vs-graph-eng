from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable

from fundamental_evidence import FundamentalEvidence
from llm import FundamentalAssessment

FUNDAMENTAL_POLICY_VERSION = "fundamental-sector-policy-v1"

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
    "impairment",
    "pat",
    "debt_to_equity",
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
    history = evidence.payload.get("financial_history")
    if not isinstance(history, dict) or history.get("status") != "ready":
        return _review(
            history.get("profile") if isinstance(history, dict) else None,
            history.get("subtype") if isinstance(history, dict) else None,
            ("financial_history",),
        )

    profile = history.get("profile")
    subtype = history.get("subtype")
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

    if assessment.verdict == "PASS":
        return _pass(
            profile,
            subtype,
            model_invoked=True,
            evidence_ids=assessment.evidence_ids,
        )
    return FundamentalDecision(
        verdict=assessment.verdict,
        reason_code=assessment.reason_code,
        reason=assessment.reason,
        evidence_ids=assessment.evidence_ids,
        missing=assessment.missing,
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
        return tuple(
            _period_fields_missing(periods[:4], _NBFC_FIELDS, minimum_periods=4)
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
        if _value(latest.get("debt_to_equity")) > 8:
            checks.append("LEVERAGE_ABOVE_POLICY")
        if _value(latest.get("impairment")) > _value(latest.get("revenue")) * 0.1:
            checks.append("IMPAIRMENT_ABOVE_POLICY")

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


def _value(value: object) -> float:
    return float(value) if _number(value) else 0.0
