"""Run the stock picker's recurring local jobs on IST-aware schedules.

Jobs execute sequentially so periodic automation never turns the deliberately
paced NSE clients into concurrent request sources. Successful occurrence IDs
are persisted across restarts; failures retry after a bounded cooldown.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

load_dotenv()

from logging_config import setup_logging  # noqa: E402
from market_time import MARKET_CLOSE, MARKET_OPEN, now_ist  # noqa: E402

log = setup_logging("scheduler")

REPO_ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("APP_SCHEDULER_STATE_PATH", ".app_scheduler_state.json"))
LOCK_PATH = Path(os.environ.get("APP_SCHEDULER_LOCK_PATH", ".app_scheduler.lock"))
POLL_SECONDS = float(os.environ.get("APP_SCHEDULER_POLL_SECONDS", "30"))
RETRY_DELAY = timedelta(
    minutes=float(os.environ.get("APP_SCHEDULER_RETRY_MINUTES", "30"))
)
BHAVCOPY_BACKFILL_DAYS = int(os.environ.get("BHAVCOPY_BACKFILL_DAYS", "30"))
XBRL_WARM_LIMIT = int(os.environ.get("APP_XBRL_WARM_LIMIT", "10"))
XBRL_UNIVERSE_INDEX = os.environ.get(
    "APP_XBRL_UNIVERSE_INDEX",
    "NIFTY TOTAL MKT",
).strip()
XBRL_UNIVERSE_BATCH_SIZE = int(
    os.environ.get("APP_XBRL_UNIVERSE_BATCH_SIZE", "25")
)
DISCLOSURE_WARM_LIMIT = int(
    os.environ.get("APP_DISCLOSURE_WARM_LIMIT", "100")
)

_stop_requested = False


@dataclass(frozen=True)
class JobRecord:
    occurrence: str
    status: str
    attempted_at: str
    finished_at: str | None = None
    return_code: int | None = None


@dataclass(frozen=True)
class Job:
    name: str
    command: tuple[str, ...]
    occurrence: Callable[[datetime], str | None]
    enabled: bool = True
    verify: Callable[[str, datetime], str] | None = None


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_clock(name: str, default: str) -> tuple[int, int]:
    raw = os.environ.get(name, default)
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must use HH:MM format, got {raw!r}") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{name} must be a valid 24-hour time, got {raw!r}")
    return hour, minute


def _previous_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def eligible_bhavcopy_occurrence(
    now: datetime,
    *,
    publish_hour: int,
    publish_minute: int = 0,
) -> str:
    day = now.date()
    if now.time() < wall_time(hour=publish_hour, minute=publish_minute):
        day -= timedelta(days=1)
    return _previous_weekday(day).isoformat()


def bhavcopy_completion_status(
    occurrence: str,
    *,
    now: datetime,
    expected_date_exists: bool,
) -> str:
    if expected_date_exists:
        return "success"
    occurrence_date = date.fromisoformat(occurrence)
    unavailable_cutoff = datetime.combine(
        occurrence_date,
        wall_time(hour=23),
        tzinfo=now.tzinfo,
    )
    return "unavailable" if now >= unavailable_cutoff else "failed"


def latest_daily_occurrence(
    now: datetime,
    *,
    hour: int,
    minute: int,
    max_lateness: timedelta,
    weekdays_only: bool,
) -> str | None:
    candidate = now.date()
    while True:
        if weekdays_only and candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
            continue
        scheduled = datetime.combine(
            candidate,
            wall_time(hour=hour, minute=minute),
            tzinfo=now.tzinfo,
        )
        if scheduled > now:
            candidate -= timedelta(days=1)
            continue
        if now - scheduled > max_lateness:
            return None
        return candidate.isoformat()


def intraday_occurrence(now: datetime, *, interval_minutes: int) -> str | None:
    if interval_minutes <= 0:
        raise ValueError("APP_INTRADAY_INTERVAL_MINUTES must be greater than zero")
    if now.weekday() >= 5 or not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return None

    elapsed = now.hour * 60 + now.minute
    slot_minutes = (elapsed // interval_minutes) * interval_minutes
    first_slot = (
        (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute + interval_minutes - 1)
        // interval_minutes
    ) * interval_minutes
    if slot_minutes < first_slot:
        return None
    slot = now.replace(
        hour=slot_minutes // 60,
        minute=slot_minutes % 60,
        second=0,
        microsecond=0,
    )
    return slot.strftime("%Y-%m-%dT%H:%M")


def should_run(
    record: JobRecord | None,
    occurrence: str,
    now: datetime,
    retry_delay: timedelta,
) -> bool:
    if record is None or record.occurrence != occurrence:
        return True
    if record.status in {"success", "unavailable"}:
        return False
    try:
        attempted_at = datetime.fromisoformat(record.attempted_at)
    except ValueError:
        return True
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=now.tzinfo)
    return now - attempted_at >= retry_delay


def _state_file(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def load_state(path: Path = STATE_PATH) -> dict[str, JobRecord]:
    target = _state_file(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text())
        jobs = payload.get("jobs", {})
        return {
            str(name): JobRecord(**record)
            for name, record in jobs.items()
            if isinstance(record, dict)
        }
    except (OSError, ValueError, TypeError):
        log.exception("scheduler state is invalid; refusing to discard it: %s", target)
        raise


def save_state(records: dict[str, JobRecord], path: Path = STATE_PATH) -> None:
    target = _state_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    payload = {
        "updated_at": now_ist().isoformat(),
        "jobs": {name: asdict(record) for name, record in sorted(records.items())},
    }
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(target)


def _daily_occurrence(
    env_name: str,
    default: str,
    *,
    max_lateness_hours: float,
    weekdays_only: bool,
) -> Callable[[datetime], str | None]:
    hour, minute = _parse_clock(env_name, default)
    return lambda now: latest_daily_occurrence(
        now,
        hour=hour,
        minute=minute,
        max_lateness=timedelta(hours=max_lateness_hours),
        weekdays_only=weekdays_only,
    )


def _bhavcopy_date_exists(occurrence: str) -> bool:
    configured = Path(os.environ.get("BHAVCOPY_DB_PATH", "bhavcopy.db"))
    database = configured if configured.is_absolute() else REPO_ROOT / configured
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT 1 FROM bhavcopy WHERE date = ? LIMIT 1",
                (occurrence,),
            ).fetchone()
    except sqlite3.Error:
        log.exception("could not verify bhavcopy occurrence %s", occurrence)
        return False
    return row is not None


def _verify_bhavcopy(occurrence: str, now: datetime) -> str:
    status = bhavcopy_completion_status(
        occurrence,
        now=now,
        expected_date_exists=_bhavcopy_date_exists(occurrence),
    )
    if status == "failed":
        log.warning(
            "bhavcopy date %s is not stored yet; it will be retried",
            occurrence,
        )
    elif status == "unavailable":
        log.warning(
            "bhavcopy date %s remained unavailable at the 23:00 IST cutoff; "
            "treating it as a holiday/unavailable session until the next catch-up",
            occurrence,
        )
    return status


def configured_jobs() -> tuple[Job, ...]:
    python = sys.executable
    intraday_interval = int(os.environ.get("APP_INTRADAY_INTERVAL_MINUTES", "20"))
    bhavcopy_hour, bhavcopy_minute = _parse_clock(
        "APP_BHAVCOPY_TIME_IST",
        "19:00",
    )
    return (
        Job(
            name="bhavcopy",
            command=(
                python,
                "bhavcopy.py",
                "backfill",
                str(BHAVCOPY_BACKFILL_DAYS),
            ),
            occurrence=lambda now: eligible_bhavcopy_occurrence(
                now,
                publish_hour=bhavcopy_hour,
                publish_minute=bhavcopy_minute,
            ),
            verify=_verify_bhavcopy,
        ),
        Job(
            name="shareholding_universe",
            command=(
                python,
                "warm_shareholding.py",
                "--universe-index",
                XBRL_UNIVERSE_INDEX,
                "--limit",
                str(XBRL_UNIVERSE_BATCH_SIZE),
            ),
            occurrence=_daily_occurrence(
                "APP_XBRL_UNIVERSE_TIME_IST",
                "16:00",
                max_lateness_hours=6,
                weekdays_only=True,
            ),
            enabled=(
                _env_flag("APP_ENABLE_XBRL_UNIVERSE_BACKFILL")
                and bool(XBRL_UNIVERSE_INDEX)
            ),
        ),
        Job(
            name="shareholding_warm",
            command=(
                python,
                "warm_shareholding.py",
                "--queued",
                "--limit",
                str(XBRL_WARM_LIMIT),
            ),
            occurrence=_daily_occurrence(
                "APP_XBRL_WARM_TIME_IST",
                "17:00",
                max_lateness_hours=6,
                weekdays_only=True,
            ),
            enabled=_env_flag("APP_ENABLE_XBRL_WARM"),
        ),
        Job(
            name="material_disclosures_warm",
            command=(
                python,
                "warm_disclosures.py",
                "--universe-index",
                XBRL_UNIVERSE_INDEX,
                "--limit",
                str(DISCLOSURE_WARM_LIMIT),
            ),
            occurrence=_daily_occurrence(
                "APP_DISCLOSURE_WARM_TIME_IST",
                "17:30",
                max_lateness_hours=6,
                weekdays_only=True,
            ),
            enabled=_env_flag("APP_ENABLE_DISCLOSURE_WARM"),
        ),
        Job(
            name="evaluation_update",
            command=(python, "evaluation.py", "update"),
            occurrence=_daily_occurrence(
                "APP_EVALUATION_TIME_IST",
                "18:30",
                max_lateness_hours=6,
                weekdays_only=True,
            ),
            enabled=_env_flag("APP_ENABLE_EVALUATION"),
        ),
        Job(
            name="overnight_scan",
            command=("bash", "run_overnight_scan.sh"),
            occurrence=_daily_occurrence(
                "APP_OVERNIGHT_SCAN_TIME_IST",
                "22:00",
                max_lateness_hours=11,
                weekdays_only=True,
            ),
            enabled=_env_flag("APP_ENABLE_OVERNIGHT_SCAN"),
        ),
        Job(
            name="intraday_recheck",
            command=(python, "intraday_recheck.py"),
            occurrence=lambda now: intraday_occurrence(
                now,
                interval_minutes=intraday_interval,
            ),
            enabled=_env_flag("APP_ENABLE_INTRADAY_RECHECK"),
        ),
        Job(
            name="cleanup",
            command=(
                python,
                "cleanup.py",
                "--apply",
                "all",
                "--skip-eval-results",
            ),
            occurrence=_daily_occurrence(
                "APP_CLEANUP_TIME_IST",
                "02:00",
                max_lateness_hours=4,
                weekdays_only=False,
            ),
            enabled=_env_flag("APP_ENABLE_CLEANUP"),
        ),
    )


def run_due_jobs(
    records: dict[str, JobRecord],
    *,
    jobs: tuple[Job, ...] | None = None,
    current_time: datetime | None = None,
) -> int:
    now = current_time or now_ist()
    failures = 0
    for job in jobs or configured_jobs():
        if _stop_requested or not job.enabled:
            continue
        occurrence = job.occurrence(now)
        if occurrence is None or not should_run(
            records.get(job.name),
            occurrence,
            now,
            RETRY_DELAY,
        ):
            continue

        attempted_at = now_ist()
        records[job.name] = JobRecord(
            occurrence=occurrence,
            status="running",
            attempted_at=attempted_at.isoformat(),
        )
        save_state(records)
        log.info(
            "job[%s] starting occurrence=%s command=%s",
            job.name,
            occurrence,
            " ".join(job.command),
        )
        try:
            result = subprocess.run(
                job.command,
                cwd=REPO_ROOT,
                env=os.environ.copy(),
                check=False,
            )
            return_code = result.returncode
        except OSError:
            log.exception("job[%s] could not start", job.name)
            return_code = 127

        finished_at = now_ist()
        status = "success" if return_code == 0 else "failed"
        if status == "success" and job.verify is not None:
            status = job.verify(occurrence, finished_at)
            if status == "failed":
                return_code = 3
        records[job.name] = JobRecord(
            occurrence=occurrence,
            status=status,
            attempted_at=attempted_at.isoformat(),
            finished_at=finished_at.isoformat(),
            return_code=return_code,
        )
        save_state(records)
        if status == "failed":
            failures += 1
            log.warning(
                "job[%s] failed occurrence=%s return_code=%d; retry in %.0f minutes",
                job.name,
                occurrence,
                return_code,
                RETRY_DELAY.total_seconds() / 60,
            )
        else:
            log.info(
                "job[%s] completed occurrence=%s status=%s",
                job.name,
                occurrence,
                status,
            )
    return failures


def _request_stop(signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True
    log.info("scheduler received signal %s; stopping after the current job", signum)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run periodic NSE application jobs.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run jobs due now once and exit",
    )
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="print persisted scheduler state and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.show_state:
        print(
            json.dumps(
                {name: asdict(record) for name, record in load_state().items()},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if POLL_SECONDS <= 0:
        raise ValueError("APP_SCHEDULER_POLL_SECONDS must be greater than zero")

    lock_target = _state_file(LOCK_PATH)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    with lock_target.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another app scheduler is already running")
            return 2

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        records = load_state()
        log.info(
            "scheduler started jobs=%s poll_seconds=%.0f",
            ", ".join(job.name for job in configured_jobs() if job.enabled),
            POLL_SECONDS,
        )
        exit_code = 0
        while not _stop_requested:
            try:
                failures = run_due_jobs(records)
                if failures:
                    exit_code = 1
            except Exception:
                log.exception("scheduler tick failed; state was preserved")
                exit_code = 1
            if args.once:
                break
            deadline = time.monotonic() + POLL_SECONDS
            while not _stop_requested and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        log.info("scheduler stopped")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
