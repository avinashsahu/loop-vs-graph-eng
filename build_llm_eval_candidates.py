"""Build an unlabeled, deduplicated LLM-evaluation review queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

DEFAULT_CACHE_DIR = Path(".cache")
DEFAULT_OUTPUT = Path("evals/results/qualitative_candidate_pool.jsonl")


def main(argv=None) -> int:
    args = _parse_args(argv)
    paths = sorted(args.cache_dir.glob("fundamentals_*.json"))
    candidates = build_candidates(paths)
    if not candidates:
        raise ValueError(f"no fundamental snapshots found in {args.cache_dir}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in candidates)
    )
    families = Counter(item["event_family"] for item in candidates)
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "event_families": dict(sorted(families.items())),
                "output": str(args.output),
                "status": "unlabeled_needs_human_review",
            },
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def build_candidates(paths: Iterable[Path]) -> list[dict]:
    """Return unique disclosure/action candidates from cached NSE snapshots."""
    unique: dict[tuple, dict] = {}
    for path in paths:
        payload = json.loads(path.read_text())
        data = payload.get("data", {})
        company_name = data.get("company_name")
        for announcement in data.get("corp_announcements") or ():
            candidate = _announcement_candidate(
                announcement, company_name=company_name, cache_path=path
            )
            unique.setdefault(candidate.pop("_identity"), candidate)
        for action in data.get("corp_actions") or ():
            candidate = _action_candidate(
                action, company_name=company_name, cache_path=path
            )
            unique.setdefault(candidate.pop("_identity"), candidate)
    return sorted(
        unique.values(),
        key=lambda item: (
            item["event_family"],
            item["evidence"]["symbol"],
            item["candidate_id"],
        ),
    )


def _announcement_candidate(
    item: dict, *, company_name: str | None, cache_path: Path
) -> dict:
    identity = (
        "announcement",
        item.get("symbol"),
        item.get("desc"),
        item.get("an_dt"),
        item.get("attchmntText"),
        item.get("attchmntFile"),
    )
    candidate_id, evidence_id = _ids(identity)
    return {
        "_identity": identity,
        "candidate_id": candidate_id,
        "event_family": _announcement_family(item),
        "label_status": "needs_human_review",
        "provenance": {
            "kind": "nse_cache_candidate",
            "cache_path": str(cache_path),
            "source_url": item.get("attchmntFile"),
        },
        "evidence": {
            "version": "fundamental-evidence-v3",
            "symbol": item.get("symbol"),
            "company_name": company_name or item.get("sm_name"),
            "facts": [
                {
                    "id": evidence_id,
                    "kind": "announcement",
                    "date": item.get("an_dt"),
                    "category": item.get("desc"),
                    "text": item.get("attchmntText"),
                }
            ],
        },
    }


def _action_candidate(
    item: dict, *, company_name: str | None, cache_path: Path
) -> dict:
    identity = (
        "corporate_action",
        item.get("symbol"),
        item.get("subject"),
        item.get("exDate"),
        item.get("recDate"),
    )
    candidate_id, evidence_id = _ids(identity)
    return {
        "_identity": identity,
        "candidate_id": candidate_id,
        "event_family": _action_family(item),
        "label_status": "needs_human_review",
        "provenance": {
            "kind": "nse_cache_candidate",
            "cache_path": str(cache_path),
            "source_url": None,
        },
        "evidence": {
            "version": "fundamental-evidence-v3",
            "symbol": item.get("symbol"),
            "company_name": company_name or item.get("comp"),
            "facts": [
                {
                    "id": evidence_id,
                    "kind": "corporate_action",
                    "subject": item.get("subject"),
                    "ex_date": item.get("exDate"),
                    "record_date": item.get("recDate"),
                }
            ],
        },
    }


def _announcement_family(item: dict) -> str:
    text = " ".join(
        str(item.get(field) or "") for field in ("desc", "attchmntText")
    ).lower()
    rules = (
        ("credit_rating", ("credit rating",)),
        ("audit", ("auditor",)),
        (
            "regulatory_or_legal",
            (
                "litigation",
                "orders passed",
                "regulation 30",
                "takeover regulations",
                "statement of deviation",
                "monitoring agency",
            ),
        ),
        ("operational_disruption", ("disruption of operations",)),
        (
            "management",
            ("change in management", "change in director", "appointment"),
        ),
        (
            "strategic_transaction",
            ("acquisition", "memorandum of understanding", "agreement"),
        ),
        ("capital_structure", ("allotment of securities", "allotment of equity")),
        (
            "routine_disclosure",
            (
                "analyst",
                "institutional investor",
                "newspaper publication",
                "shareholders meeting",
                "annual general meeting",
                "press release",
                "trading window",
                "certificate under sebi",
                "news verification",
                "investor presentation",
                "record date",
            ),
        ),
    )
    for family, phrases in rules:
        if any(phrase in text for phrase in phrases):
            return family
    return "unclassified_update"


def _action_family(item: dict) -> str:
    subject = str(item.get("subject") or "").lower()
    if "dividend" in subject or "annual general meeting" in subject:
        return "routine_distribution_or_meeting"
    if any(term in subject for term in ("split", "bonus", "buy back")):
        return "pro_rata_capital_action"
    if any(
        term in subject
        for term in ("merger", "demerger", "scheme", "amalgamation")
    ):
        return "restructuring"
    return "other_corporate_action"


def _ids(identity: tuple) -> tuple[str, str]:
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True).encode()
    ).hexdigest()[:16]
    return f"nse-{digest}", f"NSE_{digest.upper()}"


def _parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deduplicated, event-stratified review queue from cached "
            "NSE fundamental snapshots. The output is deliberately unlabeled."
        )
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
