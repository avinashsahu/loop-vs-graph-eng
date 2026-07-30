"""Offline evaluation primitives for the qualitative-disclosure classifier."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Iterable


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    expected_verdict: str
    expected_reason_code: str
    available_evidence_ids: tuple[str, ...]
    acceptable_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationPrediction:
    case_id: str
    verdict: str
    reason_code: str
    evidence_ids: tuple[str, ...]
    output_valid: bool
    first_pass_valid: bool
    repair_attempted: bool
    latency_ms: float
    response_chars: int
    assessment_reason: str = ""
    reasoning: str = ""
    completion_tokens: int = 0
    reasoning_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    expected_verdict: str
    actual_verdict: str
    expected_reason_code: str
    actual_reason_code: str
    evidence_ids: tuple[str, ...]
    exact_verdict: bool
    exact_reason_code: bool
    false_pass: bool
    grounded_citations: bool
    acceptable_citations: bool
    output_valid: bool
    first_pass_valid: bool
    repair_attempted: bool
    latency_ms: float
    response_chars: int
    assessment_reason: str
    reasoning: str
    completion_tokens: int
    reasoning_tokens: int | None
    error: str | None

    @property
    def passed(self) -> bool:
        return (
            self.exact_verdict
            and self.exact_reason_code
            and self.grounded_citations
            and self.acceptable_citations
            and self.output_valid
            and self.error is None
        )

    def to_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True)
class EvaluationReport:
    metrics: dict
    results: tuple[EvaluationResult, ...]

    def to_dict(self) -> dict:
        rendered = tuple(item.to_dict() for item in self.results)
        return {
            "metrics": self.metrics,
            "results": list(rendered),
            "failures": sorted(
                (item for item in rendered if not item["passed"]),
                key=lambda item: item["case_id"],
            ),
        }


def score_predictions(
    cases: Iterable[EvaluationCase],
    predictions: Iterable[EvaluationPrediction],
) -> EvaluationReport:
    cases_by_id = _unique_by_id(cases, "evaluation case")
    predictions_by_id = _unique_by_id(predictions, "evaluation prediction")
    if set(cases_by_id) != set(predictions_by_id):
        missing = sorted(set(cases_by_id) - set(predictions_by_id))
        unexpected = sorted(set(predictions_by_id) - set(cases_by_id))
        raise ValueError(
            f"evaluation case/prediction mismatch: missing={missing}, "
            f"unexpected={unexpected}"
        )

    results = tuple(
        _score_case(cases_by_id[case_id], predictions_by_id[case_id])
        for case_id in sorted(cases_by_id)
    )
    count = len(results)
    if not count:
        raise ValueError("evaluation requires at least one case")
    non_pass_count = sum(item.expected_verdict != "PASS" for item in results)
    reject_results = tuple(
        item for item in results if item.expected_verdict == "REJECT"
    )
    review_results = tuple(
        item for item in results if item.expected_verdict == "REVIEW"
    )
    false_pass_count = _count(results, "false_pass")
    reject_false_pass_count = _count(reject_results, "false_pass")
    review_false_pass_count = _count(review_results, "false_pass")
    latencies = sorted(item.latency_ms for item in results)
    completion_tokens = sorted(item.completion_tokens for item in results)
    reasoning_char_counts = sorted(len(item.reasoning) for item in results)
    return EvaluationReport(
        metrics={
            "case_count": count,
            "exact_verdict_count": _count(results, "exact_verdict"),
            "exact_verdict_rate": _rate(results, "exact_verdict"),
            "exact_reason_code_count": _count(results, "exact_reason_code"),
            "exact_reason_code_rate": _rate(results, "exact_reason_code"),
            "false_pass_count": false_pass_count,
            "non_pass_case_count": non_pass_count,
            "false_pass_rate": (
                false_pass_count / non_pass_count
                if non_pass_count
                else 0.0
            ),
            "false_pass_wilson_95": _wilson_95(
                false_pass_count, non_pass_count
            ),
            "reject_case_count": len(reject_results),
            "reject_false_pass_count": reject_false_pass_count,
            "reject_false_pass_rate": _rate_or_zero(
                reject_results, "false_pass"
            ),
            "reject_false_pass_wilson_95": _wilson_95(
                reject_false_pass_count, len(reject_results)
            ),
            "review_case_count": len(review_results),
            "review_false_pass_count": review_false_pass_count,
            "review_false_pass_rate": _rate_or_zero(
                review_results, "false_pass"
            ),
            "review_false_pass_wilson_95": _wilson_95(
                review_false_pass_count, len(review_results)
            ),
            "grounded_citation_count": _count(results, "grounded_citations"),
            "grounded_citation_rate": _rate(results, "grounded_citations"),
            "acceptable_citation_count": _count(
                results, "acceptable_citations"
            ),
            "acceptable_citation_rate": _rate(
                results, "acceptable_citations"
            ),
            "valid_output_count": _count(results, "output_valid"),
            "valid_output_rate": _rate(results, "output_valid"),
            "runtime_error_count": sum(item.error is not None for item in results),
            "runtime_error_rate": (
                sum(item.error is not None for item in results) / count
            ),
            "first_pass_valid_count": _count(results, "first_pass_valid"),
            "first_pass_valid_rate": _rate(results, "first_pass_valid"),
            "repair_attempt_count": _count(results, "repair_attempted"),
            "repair_rate": _rate(results, "repair_attempted"),
            "latency_ms_p50": _nearest_rank(latencies, 0.50),
            "latency_ms_p95": _nearest_rank(latencies, 0.95),
            "response_chars_mean": mean(
                item.response_chars for item in results
            ),
            "completion_tokens_p50": _nearest_rank(
                completion_tokens, 0.50
            ),
            "completion_tokens_p95": _nearest_rank(
                completion_tokens, 0.95
            ),
            "reasoning_present_count": sum(
                bool(item.reasoning) for item in results
            ),
            "reasoning_present_rate": (
                sum(bool(item.reasoning) for item in results) / count
            ),
            "reasoning_chars_p50": _nearest_rank(
                reasoning_char_counts, 0.50
            ),
            "reasoning_chars_p95": _nearest_rank(
                reasoning_char_counts, 0.95
            ),
        },
        results=results,
    )


def _score_case(
    case: EvaluationCase,
    prediction: EvaluationPrediction,
) -> EvaluationResult:
    evidence_ids = tuple(prediction.evidence_ids)
    available = set(case.available_evidence_ids)
    acceptable = set(case.acceptable_evidence_ids)
    classification_valid = prediction.output_valid and prediction.error is None
    return EvaluationResult(
        case_id=case.case_id,
        expected_verdict=case.expected_verdict,
        actual_verdict=prediction.verdict,
        expected_reason_code=case.expected_reason_code,
        actual_reason_code=prediction.reason_code,
        evidence_ids=evidence_ids,
        exact_verdict=classification_valid
        and prediction.verdict == case.expected_verdict,
        exact_reason_code=classification_valid
        and prediction.reason_code == case.expected_reason_code,
        false_pass=(
            classification_valid
            and case.expected_verdict != "PASS"
            and prediction.verdict == "PASS"
        ),
        grounded_citations=bool(evidence_ids)
        and all(item in available for item in evidence_ids),
        acceptable_citations=bool(set(evidence_ids) & acceptable),
        output_valid=prediction.output_valid,
        first_pass_valid=prediction.first_pass_valid,
        repair_attempted=prediction.repair_attempted,
        latency_ms=prediction.latency_ms,
        response_chars=prediction.response_chars,
        assessment_reason=prediction.assessment_reason,
        reasoning=prediction.reasoning,
        completion_tokens=prediction.completion_tokens,
        reasoning_tokens=prediction.reasoning_tokens,
        error=prediction.error,
    )


def _unique_by_id(items: Iterable, label: str) -> dict:
    indexed = {}
    for item in items:
        if item.case_id in indexed:
            raise ValueError(f"duplicate {label} ID: {item.case_id}")
        indexed[item.case_id] = item
    return indexed


def _count(results: tuple[EvaluationResult, ...], attribute: str) -> int:
    return sum(bool(getattr(item, attribute)) for item in results)


def _rate(results: tuple[EvaluationResult, ...], attribute: str) -> float:
    return _count(results, attribute) / len(results)


def _rate_or_zero(
    results: tuple[EvaluationResult, ...], attribute: str
) -> float:
    return _rate(results, attribute) if results else 0.0


def _wilson_95(successes: int, trials: int) -> dict[str, float] | None:
    """Return a two-sided 95% Wilson score interval for a binomial rate."""
    if not trials:
        return None
    z = 1.959963984540054
    rate = successes / trials
    z_squared = z**2
    denominator = 1 + z_squared / trials
    center = (rate + z_squared / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1 - rate) / trials
            + z_squared / (4 * trials**2)
        )
        / denominator
    )
    return {
        "lower": max(0.0, center - margin),
        "upper": min(1.0, center + margin),
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    return values[max(0, math.ceil(percentile * len(values)) - 1)]
