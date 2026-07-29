import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import timedelta

from dotenv import load_dotenv
from nsemine import archives

from logging_config import setup_logging
from market_time import now_ist

load_dotenv()

log = setup_logging("bhavcopy")

BHAVCOPY_DB_PATH = os.environ.get("BHAVCOPY_DB_PATH", "bhavcopy.db")
BHAVCOPY_BACKFILL_DAYS = int(os.environ.get("BHAVCOPY_BACKFILL_DAYS", "30"))
BHAVCOPY_REQUEST_DELAY_SECONDS = float(
    os.environ.get("BHAVCOPY_REQUEST_DELAY_SECONDS", "2")
)
BHAVCOPY_PUBLISH_HOUR_IST = int(os.environ.get("BHAVCOPY_PUBLISH_HOUR_IST", "18"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bhavcopy (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    previous_close REAL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    vwap REAL,
    volume REAL,
    turnover REAL,
    delivery_volume REAL,
    delivery_pct REAL,
    PRIMARY KEY (symbol, date)
)
"""


def _connect():
    conn = sqlite3.connect(BHAVCOPY_DB_PATH)
    conn.execute(_SCHEMA)
    return conn


@dataclass(frozen=True)
class _FetchedArchive:
    row_count: int
    dates: frozenset[str]


def _fetch_and_store_day(trade_date=None):
    df = archives.get_daily_bhavcopy_and_deliverables_data(
        series="EQ",
        trade_date=trade_date,
    )
    if df is None or df.empty:
        return None

    rows = [
        (
            row["symbol"],
            row["date"].isoformat(),
            row["previous_close"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["vwap"],
            row["volume"],
            row["turnover"],
            row["delivery_volume"],
            row["delivery_pct"],
        )
        for _, row in df.iterrows()
    ]

    with _connect() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO bhavcopy
               (symbol, date, previous_close, open, high, low, close, vwap, volume,
                turnover, delivery_volume, delivery_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return _FetchedArchive(
        row_count=len(rows),
        dates=frozenset(row[1] for row in rows),
    )


def fetch_and_store_day(trade_date=None):
    """Fetch and store one whole-market archive, returning its row count.

    NSE may return no data for a weekend, holiday, or a session that has not been
    published yet. That is represented as ``None`` rather than an error.
    """
    result = _fetch_and_store_day(trade_date)
    return result.row_count if result is not None else None


@dataclass(frozen=True)
class BackfillResult:
    requested_days: int
    available_days: int
    downloaded_days: int
    existing_days: int
    checked_days: int


def _stored_dates():
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT date FROM bhavcopy").fetchall()
    return {row[0] for row in rows}


def backfill(days=None, delay_seconds=None, start_date=None):
    """Ensure the database contains the requested number of recent trading sessions.

    Existing dates count toward the target and are never downloaded again, making the
    operation safe to resume. Missing weekdays are fetched one whole-market request at
    a time, with a delay after every NSE request. Weekends and unavailable NSE dates are
    skipped without requiring a separate exchange-holiday calendar.
    """
    days = days if days is not None else BHAVCOPY_BACKFILL_DAYS
    delay_seconds = (
        delay_seconds
        if delay_seconds is not None
        else BHAVCOPY_REQUEST_DELAY_SECONDS
    )
    if days <= 0:
        raise ValueError("days must be greater than zero")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")

    if start_date is not None:
        day = start_date
    else:
        current_time = now_ist()
        day = current_time.date()
        if current_time.hour < BHAVCOPY_PUBLISH_HOUR_IST:
            day -= timedelta(days=1)
    downloaded = 0
    existing = 0
    checked = 0
    covered_dates = set()
    stored_dates = _stored_dates()
    max_days_to_check = max(days * 3, days + 10)

    while len(covered_dates) < days and checked < max_days_to_check:
        checked += 1
        if day.weekday() < 5:
            requested_date = day.isoformat()
            if requested_date in stored_dates:
                if requested_date not in covered_dates:
                    covered_dates.add(requested_date)
                    existing += 1
                    log.info("bhavcopy already stored for %s; skipping download", day)
            else:
                try:
                    result = _fetch_and_store_day(trade_date=day)
                finally:
                    if delay_seconds:
                        time.sleep(delay_seconds)
                if result:
                    eligible_dates = {
                        archive_date
                        for archive_date in result.dates
                        if archive_date <= day.isoformat()
                    }
                    new_coverage = eligible_dates - covered_dates
                    downloaded += len(new_coverage - stored_dates)
                    existing += len(new_coverage & stored_dates)
                    covered_dates.update(new_coverage)
                    stored_dates.update(result.dates)
                    log.info(
                        "archive request for %s stored %d symbols for date(s) %s",
                        day,
                        result.row_count,
                        ", ".join(sorted(result.dates)),
                    )
        day -= timedelta(days=1)

    available = len(covered_dates)
    return BackfillResult(
        requested_days=days,
        available_days=available,
        downloaded_days=downloaded,
        existing_days=existing,
        checked_days=checked,
    )


def get_delivery_trend(symbol, recent_days=5, baseline_days=20):
    """Compare recent delivery participation with a complete preceding baseline."""
    required_days = recent_days + baseline_days
    with _connect() as conn:
        cur = conn.execute(
            """SELECT date, delivery_pct, delivery_volume, volume, previous_close,
                      close, vwap
               FROM bhavcopy
              WHERE symbol = ?
              ORDER BY date DESC
              LIMIT ?""",
            (symbol, required_days),
        )
        rows = cur.fetchall()

    if len(rows) < required_days:
        return {
            "status": "insufficient_history",
            "days_of_history": len(rows),
            "required_days": required_days,
            "latest_date": rows[0][0] if rows else None,
        }

    if any(value is None for row in rows for value in row[1:6]):
        return {
            "status": "missing_values",
            "days_of_history": len(rows),
            "required_days": required_days,
            "latest_date": rows[0][0],
        }

    recent = rows[:recent_days]
    baseline = rows[recent_days : recent_days + baseline_days]
    recent_avg = sum(r[1] for r in recent) / len(recent)
    baseline_avg = sum(r[1] for r in baseline) / len(baseline)
    recent_delivery_volume_avg = sum(r[2] for r in recent) / len(recent)
    baseline_delivery_volume_avg = sum(r[2] for r in baseline) / len(baseline)
    recent_total_volume_avg = sum(r[3] for r in recent) / len(recent)
    baseline_total_volume_avg = sum(r[3] for r in baseline) / len(baseline)

    def classify_change(recent_value, baseline_value):
        if recent_value > baseline_value * 1.1:
            return "rising"
        if recent_value < baseline_value * 0.9:
            return "falling"
        return "flat"

    delivery_pct_trend = classify_change(recent_avg, baseline_avg)
    delivery_volume_trend = classify_change(
        recent_delivery_volume_avg,
        baseline_delivery_volume_avg,
    )
    recent_price_change_pct = ((recent[0][5] / recent[-1][4]) - 1) * 100

    if delivery_volume_trend == "rising" and recent_price_change_pct >= 1:
        interpretation = "possible_accumulation"
    elif delivery_volume_trend == "rising" and recent_price_change_pct <= -1:
        interpretation = "possible_distribution"
    elif delivery_pct_trend == "rising" and delivery_volume_trend != "rising":
        interpretation = "delivery_pct_rise_unconfirmed_by_volume"
    else:
        interpretation = "mixed_or_neutral"

    return {
        "status": "ready",
        "recent_avg_delivery_pct": round(recent_avg, 2),
        "baseline_avg_delivery_pct": round(baseline_avg, 2),
        "delivery_pct_trend": delivery_pct_trend,
        "recent_avg_delivery_volume": round(recent_delivery_volume_avg, 2),
        "baseline_avg_delivery_volume": round(baseline_delivery_volume_avg, 2),
        "delivery_volume_trend": delivery_volume_trend,
        "recent_avg_total_volume": round(recent_total_volume_avg, 2),
        "baseline_avg_total_volume": round(baseline_total_volume_avg, 2),
        "recent_price_change_pct": round(recent_price_change_pct, 2),
        "interpretation": interpretation,
        # Kept for consumers of the original shape. This describes delivery
        # percentage only; it is not directional evidence of buying.
        "trend": delivery_pct_trend,
        "latest_date": rows[0][0],
        "latest_vwap": rows[0][6],
        "days_of_history": len(rows),
        "required_days": required_days,
        "window": (
            f"latest {recent_days} sessions vs preceding {baseline_days} sessions"
        ),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else BHAVCOPY_BACKFILL_DAYS
        result = backfill(days)
        print(
            f"Bhavcopy coverage: {result.available_days}/{result.requested_days} "
            f"trading sessions ({result.downloaded_days} downloaded, "
            f"{result.existing_days} already present, "
            f"{result.checked_days} calendar days checked)."
        )
        if result.available_days < result.requested_days:
            raise SystemExit(1)
    else:
        n = fetch_and_store_day()
        print(f"Stored today's bhavcopy: {n} symbols." if n else "No bhavcopy available for today (yet).")
