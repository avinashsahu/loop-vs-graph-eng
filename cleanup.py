"""Prune local runtime artifacts (cache, logs, JSONL fallbacks, dev caches).

Dry-run by default; pass --apply to delete or rewrite files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from market_time import now_ist  # noqa: E402

CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.jsonl")
SCAN_RUN_LOG_PATH = os.environ.get("SCAN_RUN_LOG_PATH", "scan_runs.jsonl")
CRON_LOG_PATH = os.environ.get("CRON_LOG_PATH", "cron.log")
EVAL_RESULTS_DIR = Path("evals/results")
ARCHIVE_DIR = Path(os.environ.get("CLEANUP_ARCHIVE_DIR", ".archive"))


@dataclass(frozen=True)
class PathStat:
    path: str
    exists: bool
    bytes: int
    files: int | None = None


@dataclass(frozen=True)
class ActionSummary:
    removed_files: int = 0
    removed_bytes: int = 0
    kept_files: int = 0
    archived_lines: int = 0
    kept_lines: int = 0
    truncated_bytes: int = 0


def _path_stat(path: Path, *, count_files: bool = False) -> PathStat:
    if not path.exists():
        return PathStat(path=str(path), exists=False, bytes=0, files=0 if count_files else None)
    if path.is_file():
        size = path.stat().st_size
        return PathStat(path=str(path), exists=True, bytes=size, files=1 if count_files else None)
    total = 0
    files = 0
    for child in path.rglob("*"):
        if child.is_file():
            files += 1
            total += child.stat().st_size
    return PathStat(path=str(path), exists=True, bytes=total, files=files)


def audit_paths(repo_root: Path) -> dict[str, object]:
    cache = repo_root / CACHE_DIR if not Path(CACHE_DIR).is_absolute() else Path(CACHE_DIR)
    trade_path = _repo_path(repo_root, TRADE_LOG_PATH)
    scan_path = _repo_path(repo_root, SCAN_RUN_LOG_PATH)
    return {
        "generated_at": now_ist().isoformat(),
        "paths": [
            asdict(_path_stat(trade_path)),
            asdict(_path_stat(scan_path)),
            asdict(_path_stat(_repo_path(repo_root, os.environ.get("EVALUATION_DB_PATH", "evaluation.db")))),
            asdict(_path_stat(_repo_path(repo_root, os.environ.get("BHAVCOPY_DB_PATH", "bhavcopy.db")))),
            asdict(_path_stat(cache, count_files=True)),
            asdict(_path_stat(_repo_path(repo_root, CRON_LOG_PATH))),
            asdict(_path_stat(repo_root / EVAL_RESULTS_DIR, count_files=True)),
            asdict(_path_stat(repo_root / "__pycache__", count_files=True)),
            asdict(_path_stat(repo_root / ".pytest_cache", count_files=True)),
            asdict(_path_stat(repo_root / ".ruff_cache", count_files=True)),
            asdict(_path_stat(repo_root / ARCHIVE_DIR, count_files=True)),
        ],
    }


def _parse_recorded_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def trade_log_cutoff(
    *,
    retention_days: int,
    now: datetime | None = None,
) -> datetime:
    anchor = now or now_ist()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=now_ist().tzinfo)
    return anchor - timedelta(days=retention_days)


def partition_trade_log_records(
    records: list[dict],
    *,
    retention_days: int,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    cutoff = trade_log_cutoff(retention_days=retention_days, now=now)
    overnight_labels = sorted(
        {
            str(record.get("scan_label", ""))
            for record in records
            if str(record.get("scan_label", "")).startswith("overnight_")
        }
    )
    latest_overnight = overnight_labels[-1] if overnight_labels else ""

    kept: list[dict] = []
    removed: list[dict] = []
    for record in records:
        label = str(record.get("scan_label", ""))
        if label and label == latest_overnight:
            kept.append(record)
            continue
        recorded = _parse_recorded_at(record.get("timestamp"))
        if recorded is None:
            kept.append(record)
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=cutoff.tzinfo)
        if recorded >= cutoff:
            kept.append(record)
        else:
            removed.append(record)
    return kept, removed


def partition_scan_run_lines(
    lines: list[str],
    *,
    retention_days: int,
    now: datetime | None = None,
) -> tuple[list[str], list[str]]:
    cutoff = trade_log_cutoff(retention_days=retention_days, now=now)
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(line if line.endswith("\n") else f"{line}\n")
            continue
        recorded = _parse_recorded_at(record.get("recorded_at"))
        if recorded is None:
            kept.append(line if line.endswith("\n") else f"{line}\n")
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=cutoff.tzinfo)
        if recorded >= cutoff:
            kept.append(line if line.endswith("\n") else f"{line}\n")
        else:
            removed.append(line if line.endswith("\n") else f"{line}\n")
    return kept, removed


def prune_stale_cache(
    cache_dir: Path,
    *,
    max_age_seconds: float,
    apply: bool,
) -> ActionSummary:
    if not cache_dir.is_dir():
        return ActionSummary()
    removed_files = 0
    removed_bytes = 0
    kept_files = 0
    now = time.time()
    for path in cache_dir.glob("*.json"):
        try:
            with path.open() as handle:
                entry = json.load(handle)
            fetched_at = float(entry["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            kept_files += 1
            continue
        age = now - fetched_at
        if age <= max_age_seconds:
            kept_files += 1
            continue
        size = path.stat().st_size
        removed_files += 1
        removed_bytes += size
        if apply:
            path.unlink(missing_ok=True)
    return ActionSummary(
        removed_files=removed_files,
        removed_bytes=removed_bytes,
        kept_files=kept_files,
    )


def _rewrite_jsonl(
    path: Path,
    kept_lines: list[str],
    removed_lines: list[str],
    *,
    archive_dir: Path,
    apply: bool,
) -> ActionSummary:
    if not removed_lines:
        return ActionSummary(kept_lines=len(kept_lines), archived_lines=0)
    archived_lines = len(removed_lines)
    if apply:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_ist().strftime("%Y%m%d_%H%M%S")
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text("".join(kept_lines))
        temp_path.replace(path)
        if removed_lines:
            archive_path = archive_dir / f"{path.name}.{stamp}.jsonl"
            archive_path.write_text("".join(removed_lines))
    return ActionSummary(
        kept_lines=len(kept_lines),
        archived_lines=archived_lines,
    )


def _repo_path(repo_root: Path, configured: str) -> Path:
    path = Path(configured)
    if path.is_absolute():
        return path
    return repo_root / path


def compact_trade_log(
    path: Path,
    *,
    retention_days: int,
    archive_dir: Path,
    apply: bool,
) -> ActionSummary:
    if not path.exists():
        return ActionSummary()
    raw_lines = path.read_text().splitlines(keepends=True)
    kept_lines: list[str] = []
    removed_lines: list[str] = []
    overnight_labels: list[str] = []
    parsed: list[tuple[str, dict] | tuple[str, None]] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            kept_lines.append(line if line.endswith("\n") else f"{line}\n")
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            kept_lines.append(line if line.endswith("\n") else f"{line}\n")
            continue
        if not isinstance(record, dict):
            kept_lines.append(line if line.endswith("\n") else f"{line}\n")
            continue
        label = str(record.get("scan_label", ""))
        if label.startswith("overnight_"):
            overnight_labels.append(label)
        parsed.append((line if line.endswith("\n") else f"{line}\n", record))
    latest_overnight = sorted(set(overnight_labels))[-1] if overnight_labels else ""
    cutoff = trade_log_cutoff(retention_days=retention_days)
    for line, record in parsed:
        label = str(record.get("scan_label", ""))
        if label and label == latest_overnight:
            kept_lines.append(line)
            continue
        recorded = _parse_recorded_at(record.get("timestamp"))
        if recorded is None:
            kept_lines.append(line)
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=cutoff.tzinfo)
        if recorded >= cutoff:
            kept_lines.append(line)
        else:
            removed_lines.append(line)
    return _rewrite_jsonl(path, kept_lines, removed_lines, archive_dir=archive_dir, apply=apply)


def compact_scan_run_log(
    path: Path,
    *,
    retention_days: int,
    archive_dir: Path,
    apply: bool,
) -> ActionSummary:
    if not path.exists():
        return ActionSummary()
    lines = path.read_text().splitlines(keepends=True)
    kept, removed = partition_scan_run_lines(lines, retention_days=retention_days)
    return _rewrite_jsonl(path, kept, removed, archive_dir=archive_dir, apply=apply)


def truncate_cron_log(path: Path, *, max_bytes: int, apply: bool) -> ActionSummary:
    if not path.is_file():
        return ActionSummary()
    size = path.stat().st_size
    if size <= max_bytes:
        return ActionSummary(kept_files=1)
    if not apply:
        return ActionSummary(truncated_bytes=size - max_bytes)
    data = path.read_bytes()
    tail = data[-max_bytes:]
    newline = tail.find(b"\n")
    if newline != -1:
        tail = tail[newline + 1 :]
    path.write_bytes(tail)
    return ActionSummary(truncated_bytes=size - max_bytes)


def remove_dev_artifacts(repo_root: Path, *, apply: bool) -> ActionSummary:
    targets = [
        repo_root / "__pycache__",
        repo_root / ".pytest_cache",
        repo_root / ".ruff_cache",
    ]
    removed_files = 0
    removed_bytes = 0
    for root in targets:
        if not root.exists():
            continue
        for child in root.rglob("*"):
            if child.is_file():
                removed_files += 1
                removed_bytes += child.stat().st_size
        if apply:
            shutil.rmtree(root)
    return ActionSummary(removed_files=removed_files, removed_bytes=removed_bytes)


def prune_eval_results(
    results_dir: Path,
    *,
    keep: tuple[str, ...],
    apply: bool,
) -> ActionSummary:
    if not results_dir.is_dir():
        return ActionSummary()
    keep_names = {p.name for pattern in keep for p in results_dir.glob(pattern)}
    removed_files = 0
    removed_bytes = 0
    kept_files = 0
    for path in results_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in keep_names or path.name == "qualitative_candidate_pool.jsonl":
            kept_files += 1
            continue
        removed_files += 1
        removed_bytes += path.stat().st_size
        if apply:
            path.unlink(missing_ok=True)
    return ActionSummary(
        removed_files=removed_files,
        removed_bytes=removed_bytes,
        kept_files=kept_files,
    )


def _print_summary(label: str, summary: ActionSummary) -> None:
    payload = {key: value for key, value in asdict(summary).items() if value}
    if payload:
        print(f"{label}: {json.dumps(payload, sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and prune local runtime artifacts.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform deletions/rewrites (default is dry-run)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("audit", help="print sizes for known artifact paths")

    cache_parser = subparsers.add_parser("cache", help="drop expired .cache JSON files")
    cache_parser.add_argument(
        "--max-age-hours",
        type=float,
        default=float(os.environ.get("CLEANUP_CACHE_MAX_AGE_HOURS", "48")),
        help="remove cache files older than this (default: 48h)",
    )

    jsonl_parser = subparsers.add_parser(
        "jsonl",
        help="archive and compact trade_log.jsonl / scan_runs.jsonl",
    )
    jsonl_parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("CLEANUP_JSONL_RETENTION_DAYS", "90")),
    )

    cron_parser = subparsers.add_parser("cron-log", help="truncate cron.log tail")
    cron_parser.add_argument(
        "--max-mb",
        type=float,
        default=float(os.environ.get("CLEANUP_CRON_LOG_MAX_MB", "5")),
    )

    eval_parser = subparsers.add_parser("eval-results", help="remove old evals/results JSON")
    eval_parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="GLOB",
        help="glob patterns to keep (repeatable); default keeps nothing except candidate pool",
    )

    subparsers.add_parser("dev", help="remove __pycache__, .pytest_cache, .ruff_cache")

    all_parser = subparsers.add_parser("all", help="run cache, jsonl, cron-log, dev, eval-results")
    all_parser.add_argument(
        "--skip-jsonl",
        action="store_true",
        help="do not compact JSONL logs (safe if you rely on trade_log for old digests)",
    )
    all_parser.add_argument(
        "--skip-eval-results",
        action="store_true",
        help="leave evals/results untouched",
    )

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    apply = args.apply
    if args.command != "audit" and not apply:
        print("dry-run (pass --apply to make changes)", file=sys.stderr)

    if args.command == "audit":
        print(json.dumps(audit_paths(repo_root), indent=2, sort_keys=True))
        return 0

    if args.command == "cache":
        summary = prune_stale_cache(
            repo_root / CACHE_DIR
            if not Path(CACHE_DIR).is_absolute()
            else Path(CACHE_DIR),
            max_age_seconds=args.max_age_hours * 3600,
            apply=apply,
        )
        _print_summary("cache", summary)
        return 0

    if args.command == "jsonl":
        archive_dir = repo_root / ARCHIVE_DIR
        trade_path = _repo_path(repo_root, TRADE_LOG_PATH)
        scan_path = _repo_path(repo_root, SCAN_RUN_LOG_PATH)
        _print_summary(
            "trade_log",
            compact_trade_log(
                trade_path,
                retention_days=args.retention_days,
                archive_dir=archive_dir,
                apply=apply,
            ),
        )
        _print_summary(
            "scan_runs",
            compact_scan_run_log(
                scan_path,
                retention_days=args.retention_days,
                archive_dir=archive_dir,
                apply=apply,
            ),
        )
        return 0

    if args.command == "cron-log":
        summary = truncate_cron_log(
            repo_root / CRON_LOG_PATH,
            max_bytes=int(args.max_mb * 1024 * 1024),
            apply=apply,
        )
        _print_summary("cron-log", summary)
        return 0

    if args.command == "eval-results":
        keep = tuple(args.keep) if args.keep else ("*_v1.json",)
        summary = prune_eval_results(repo_root / EVAL_RESULTS_DIR, keep=keep, apply=apply)
        _print_summary("eval-results", summary)
        return 0

    if args.command == "dev":
        summary = remove_dev_artifacts(repo_root, apply=apply)
        _print_summary("dev", summary)
        return 0

    if args.command == "all":
        _print_summary(
            "cache",
            prune_stale_cache(
                repo_root / CACHE_DIR
                if not Path(CACHE_DIR).is_absolute()
                else Path(CACHE_DIR),
                max_age_seconds=float(
                    os.environ.get("CLEANUP_CACHE_MAX_AGE_HOURS", "48")
                )
                * 3600,
                apply=apply,
            ),
        )
        if not args.skip_jsonl:
            archive_dir = repo_root / ARCHIVE_DIR
            retention = int(os.environ.get("CLEANUP_JSONL_RETENTION_DAYS", "90"))
            _print_summary(
                "trade_log",
                compact_trade_log(
                    _repo_path(repo_root, TRADE_LOG_PATH),
                    retention_days=retention,
                    archive_dir=archive_dir,
                    apply=apply,
                ),
            )
            _print_summary(
                "scan_runs",
                compact_scan_run_log(
                    _repo_path(repo_root, SCAN_RUN_LOG_PATH),
                    retention_days=retention,
                    archive_dir=archive_dir,
                    apply=apply,
                ),
            )
        _print_summary(
            "cron-log",
            truncate_cron_log(
                repo_root / CRON_LOG_PATH,
                max_bytes=int(
                    float(os.environ.get("CLEANUP_CRON_LOG_MAX_MB", "5")) * 1024 * 1024
                ),
                apply=apply,
            ),
        )
        _print_summary("dev", remove_dev_artifacts(repo_root, apply=apply))
        if not args.skip_eval_results:
            _print_summary(
                "eval-results",
                prune_eval_results(
                    repo_root / EVAL_RESULTS_DIR,
                    keep=("*_v1.json",),
                    apply=apply,
                ),
            )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
