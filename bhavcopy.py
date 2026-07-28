import os
import sqlite3
import sys
import time
from datetime import timedelta

from nsemine import archives

from logging_config import setup_logging
from market_time import now_ist

log = setup_logging("bhavcopy")

BHAVCOPY_DB_PATH = os.environ.get("BHAVCOPY_DB_PATH", "bhavcopy.db")
BHAVCOPY_BACKFILL_DAYS = int(os.environ.get("BHAVCOPY_BACKFILL_DAYS", "25"))

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


def fetch_and_store_day(trade_date=None):
    """Fetch one day's whole-market bhavcopy (~2400 EQ symbols in one NSE request) and
    upsert it. Returns the number of rows stored, or None if NSE had nothing for that
    date (weekend/holiday) -- not an error, just skip it."""
    df = archives.get_daily_bhavcopy_and_deliverables_data(series="EQ", trade_date=trade_date)
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
    return len(rows)


def backfill(days=None):
    """One-time (or occasional) backfill -- walks back day by day, skipping weekends,
    and skipping any date NSE has nothing for (holidays) rather than tracking a holiday
    calendar separately."""
    days = days if days is not None else BHAVCOPY_BACKFILL_DAYS
    day = now_ist().date()
    fetched = 0
    checked = 0
    while fetched < days and checked < days * 2:
        checked += 1
        if day.weekday() < 5:
            n = fetch_and_store_day(trade_date=day)
            if n:
                fetched += 1
                log.info("backfilled %s: %d symbols", day, n)
            time.sleep(0.5)
        day -= timedelta(days=1)
    return fetched


def get_delivery_trend(symbol, recent_days=5, baseline_days=20):
    """Compares the average delivery_pct over the most recent `recent_days` against the
    `baseline_days` before that -- rising vs falling delivery interest, not just a single
    day's snapshot. Returns None if there's not enough history yet (run backfill() first)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT date, delivery_pct, vwap FROM bhavcopy WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, recent_days + baseline_days),
        )
        rows = cur.fetchall()

    if len(rows) < recent_days + 1:
        return None

    recent = rows[:recent_days]
    baseline = rows[recent_days : recent_days + baseline_days]
    recent_avg = sum(r[1] for r in recent) / len(recent)
    baseline_avg = sum(r[1] for r in baseline) / len(baseline) if baseline else None

    return {
        "recent_avg_delivery_pct": round(recent_avg, 2),
        "baseline_avg_delivery_pct": round(baseline_avg, 2) if baseline_avg is not None else None,
        "trend": (
            "insufficient_history"
            if baseline_avg is None
            else "rising" if recent_avg > baseline_avg * 1.1
            else "falling" if recent_avg < baseline_avg * 0.9
            else "flat"
        ),
        "latest_vwap": rows[0][2],
        "days_of_history": len(rows),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else BHAVCOPY_BACKFILL_DAYS
        fetched = backfill(days)
        print(f"Backfilled {fetched} trading day(s).")
    else:
        n = fetch_and_store_day()
        print(f"Stored today's bhavcopy: {n} symbols." if n else "No bhavcopy available for today (yet).")
