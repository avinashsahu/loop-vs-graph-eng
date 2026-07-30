"""Run the qualitative-disclosure corpus against one or more local models."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from openai import OpenAI

import llm
from fundamental_evidence import FundamentalEvidence, PROMPT_VERSION
from llm_eval import EvaluationCase, EvaluationPrediction, score_predictions
from qualitative_policy import QUALITATIVE_EXPECTED_VERDICTS

DEFAULT_CORPUS = Path("evals/qualitative_disclosures_v1.jsonl")
EVALUATION_VERSION = "qualitative-llm-eval-v1"


def main(argv=None) -> int:
    args = _parse_args(argv)
    entries = _load_corpus(args.corpus)
    if args.case_id:
        selected = set(args.case_id)
        entries = [item for item in entries if item["case_id"] in selected]
        missing = sorted(selected - {item["case_id"] for item in entries})
        if missing:
            raise ValueError(f"unknown evaluation case IDs: {missing}")
    model_specs = [
        _parse_model_spec(item, args.no_think_directive)
        for item in (args.model or [llm.LOCAL_LLM_MODEL])
    ]
    runs = [
        _evaluate_model(
            entries,
            model=model,
            base_url=args.base_url,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
            no_think_directive=directive,
        )
        for model, directive in model_specs
    ]
    rendered = {
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": {
            "path": str(args.corpus),
            "version": entries[0]["corpus_version"],
            "case_count": len(entries),
            "real_nse_case_count": sum(
                item["provenance"]["kind"] == "nse_cache_excerpt"
                for item in entries
            ),
            "synthetic_probe_count": sum(
                item["provenance"]["kind"] == "synthetic_policy_probe"
                for item in entries
            ),
        },
        "prompt_version": PROMPT_VERSION,
        "schema_version": llm.FUNDAMENTAL_SCHEMA_VERSION,
        "runs": runs,
    }
    output = json.dumps(rendered, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


def _evaluate_model(
    entries: list[dict],
    *,
    model: str,
    base_url: str,
    max_tokens: int,
    timeout_seconds: float,
    no_think_directive: str,
) -> dict:
    previous = {
        "client": llm._local_client,
        "url": llm.LOCAL_LLM_URL,
        "model": llm.LOCAL_LLM_MODEL,
        "max_tokens": llm.FUNDAMENTAL_LLM_MAX_TOKENS,
        "directive": llm.LOCAL_LLM_NO_THINK_DIRECTIVE,
        "use_local": llm.USE_LOCAL_LLM,
        "use_real": llm.USE_REAL_LLM,
    }
    llm._local_client = OpenAI(
        base_url=base_url,
        api_key="not-needed",
        timeout=timeout_seconds,
    )
    llm.LOCAL_LLM_URL = base_url
    llm.LOCAL_LLM_MODEL = model
    llm.FUNDAMENTAL_LLM_MAX_TOKENS = max_tokens
    llm.LOCAL_LLM_NO_THINK_DIRECTIVE = no_think_directive
    llm.USE_LOCAL_LLM = True
    llm.USE_REAL_LLM = False
    try:
        predictions = tuple(
            _evaluate_case(item, position=index, total=len(entries))
            for index, item in enumerate(entries, start=1)
        )
    finally:
        llm._local_client = previous["client"]
        llm.LOCAL_LLM_URL = previous["url"]
        llm.LOCAL_LLM_MODEL = previous["model"]
        llm.FUNDAMENTAL_LLM_MAX_TOKENS = previous["max_tokens"]
        llm.LOCAL_LLM_NO_THINK_DIRECTIVE = previous["directive"]
        llm.USE_LOCAL_LLM = previous["use_local"]
        llm.USE_REAL_LLM = previous["use_real"]

    scoring_cases = tuple(_scoring_case(item) for item in entries)
    report = score_predictions(scoring_cases, predictions).to_dict()
    metadata = {
        item["case_id"]: {
            "provenance": item["provenance"],
            "rationale": item["rationale"],
        }
        for item in entries
    }
    for result in report["results"]:
        result.update(metadata[result["case_id"]])
    for failure in report["failures"]:
        failure.update(metadata[failure["case_id"]])
    return {
        "model": {
            "backend": "openai_compatible_local",
            "base_url": base_url,
            "name": model,
            "max_tokens": max_tokens,
            "no_think_directive": no_think_directive,
            "timeout_seconds": timeout_seconds,
        },
        **report,
    }


def _evaluate_case(
    entry: dict,
    *,
    position: int,
    total: int,
) -> EvaluationPrediction:
    evidence = FundamentalEvidence(entry["evidence"])
    print(
        f"[{position}/{total}] {llm.LOCAL_LLM_MODEL} {entry['case_id']}",
        file=sys.stderr,
    )
    started = time.perf_counter()
    try:
        run = llm.assess_fundamentals_run(
            evidence.prompt(), evidence.qualitative_ids
        )
        assessment = run.assessment
        return EvaluationPrediction(
            case_id=entry["case_id"],
            verdict=assessment.verdict,
            reason_code=assessment.reason_code,
            evidence_ids=assessment.evidence_ids,
            output_valid=run.output_valid,
            first_pass_valid=run.first_pass_valid,
            repair_attempted=run.repair_attempted,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            response_chars=run.response_chars,
        )
    except Exception as exc:
        return EvaluationPrediction(
            case_id=entry["case_id"],
            verdict="REVIEW",
            reason_code="INSUFFICIENT_EVIDENCE",
            evidence_ids=(),
            output_valid=False,
            first_pass_valid=False,
            repair_attempted=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            response_chars=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def _scoring_case(entry: dict) -> EvaluationCase:
    facts = entry["evidence"]["facts"]
    return EvaluationCase(
        case_id=entry["case_id"],
        expected_verdict=entry["expected"]["verdict"],
        expected_reason_code=entry["expected"]["reason_code"],
        available_evidence_ids=tuple(item["id"] for item in facts),
        acceptable_evidence_ids=tuple(
            entry["expected"]["acceptable_evidence_ids"]
        ),
    )


def _load_corpus(path: Path) -> list[dict]:
    entries = [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    if not entries:
        raise ValueError("evaluation corpus is empty")
    versions = {item.get("corpus_version") for item in entries}
    if len(versions) != 1 or None in versions:
        raise ValueError(f"evaluation corpus versions disagree: {versions}")
    case_ids = [item["case_id"] for item in entries]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation corpus contains duplicate case IDs")
    for item in entries:
        _validate_entry(item)
    return entries


def _validate_entry(entry: dict) -> None:
    expected = entry["expected"]
    expected_verdict = expected["verdict"]
    expected_reason_code = expected["reason_code"]
    required_verdict = QUALITATIVE_EXPECTED_VERDICTS.get(expected_reason_code)
    if required_verdict != expected_verdict:
        raise ValueError(
            f"{entry['case_id']}: expected verdict/reason code disagree"
        )
    facts = entry["evidence"]["facts"]
    evidence_ids = {item["id"] for item in facts}
    acceptable = set(expected["acceptable_evidence_ids"])
    if not facts or not acceptable or not acceptable <= evidence_ids:
        raise ValueError(
            f"{entry['case_id']}: invalid acceptable evidence IDs"
        )
    if entry["provenance"]["kind"] not in {
        "nse_cache_excerpt",
        "synthetic_policy_probe",
    }:
        raise ValueError(f"{entry['case_id']}: unknown provenance")


def _parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the qualitative-disclosure LLM classifier."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--model",
        action="append",
        help=(
            "Local model name; repeat to compare models. Append "
            "'::DIRECTIVE' for a model-specific no-think directive, or '::' "
            "to disable it for that model."
        ),
    )
    parser.add_argument("--base-url", default=llm.LOCAL_LLM_URL)
    parser.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=llm.FUNDAMENTAL_LLM_MAX_TOKENS,
    )
    parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=120.0
    )
    parser.add_argument(
        "--no-think-directive",
        default=llm.LOCAL_LLM_NO_THINK_DIRECTIVE,
    )
    parser.add_argument(
        "--case-id",
        action="append",
        help="Run one case; repeat to select several.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def _parse_model_spec(value: str, default_directive: str) -> tuple[str, str]:
    if "::" not in value:
        return value, default_directive
    model, directive = value.split("::", 1)
    if not model:
        raise ValueError("model name cannot be empty")
    return model, directive


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
