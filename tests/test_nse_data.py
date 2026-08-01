import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import nse_data


IST = ZoneInfo("Asia/Kolkata")


def _history(timestamps, *, close=None, volume=1000):
    closes = close if close is not None else [101.0] * len(timestamps)
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(timestamps),
            "open": [100.0] * len(timestamps),
            "high": [102.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": closes,
            "volume": [volume] * len(timestamps),
        }
    )


def _session_5m_bars(day: str, start_hhmm: str, count: int, *, incomplete_tail=None):
    start = pd.Timestamp(f"{day} {start_hhmm}")
    stamps = [start + pd.Timedelta(minutes=5 * index) for index in range(count)]
    if incomplete_tail is not None:
        stamps.append(pd.Timestamp(f"{day} {incomplete_tail}"))
    return _history(stamps)


class MarketSnapshotTests(unittest.TestCase):
    def test_live_snapshot_excludes_unfinished_daily_and_intraday_candles(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        # 09:15..13:00 complete 5m bars (46 bars), plus unfinished 13:05/13:08-style tail.
        five = _session_5m_bars("2026-07-29", "09:15", 46, incomplete_tail="13:05")
        calls = []

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            calls.append(interval)
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return five.copy()

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data.time, "sleep"),
            ):
                snapshot = nse_data.get_market_snapshot("ACE")

        self.assertCountEqual(calls, ["D", 5])
        self.assertEqual(
            snapshot.histories["D"]["datetime"].dt.strftime("%Y-%m-%d").tolist(),
            ["2026-07-28"],
        )
        self.assertEqual(snapshot.provenance["5"]["dropped_incomplete_bars"], 1)
        self.assertEqual(snapshot.provenance["D"]["dropped_incomplete_bars"], 1)
        self.assertEqual(snapshot.provenance["15"]["source"], "derived_from_5m")
        self.assertEqual(snapshot.provenance["30"]["source"], "derived_from_5m")
        self.assertGreaterEqual(len(snapshot.histories["5"]), 45)
        self.assertGreater(len(snapshot.histories["15"]), 0)
        self.assertGreater(len(snapshot.histories["30"]), 0)
        self.assertEqual(snapshot.observed_at, observed_at.isoformat())

    def test_fetches_only_daily_and_five_minute_from_nse(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        calls = []

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            calls.append((symbol, interval))
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return _session_5m_bars("2026-07-29", "09:15", 40)

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data.time, "sleep"),
            ):
                nse_data.get_market_snapshot("ACE")
                nse_data.get_market_snapshot("NIFTY", timeframes=("D",))

        self.assertCountEqual(
            calls,
            [("ACE", "D"), ("ACE", 5), ("NIFTY", "D")],
        )

    def test_daily_and_five_minute_pacing_sleeps_overlap(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        active = {"count": 0, "max": 0}
        lock = threading.Lock()
        real_sleep = time.sleep

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return _session_5m_bars("2026-07-29", "09:15", 40)

        def paced_sleep(seconds):
            with lock:
                active["count"] += 1
                active["max"] = max(active["max"], active["count"])
            real_sleep(0.05)
            with lock:
                active["count"] -= 1

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data, "NSE_CALL_DELAY_SECONDS", 0.05),
                patch.object(nse_data.time, "sleep", side_effect=paced_sleep),
            ):
                nse_data.get_market_snapshot("ACE")

        self.assertGreaterEqual(active["max"], 2)

    def test_parallel_hist_falls_back_to_sequential_on_failure(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        calls = []
        attempts = {"n": 0}

        def fetch_once_fails(symbol, start_datetime, end_datetime, interval):
            attempts["n"] += 1
            calls.append(interval)
            if attempts["n"] == 1:
                raise ConnectionError("boom")
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return _session_5m_bars("2026-07-29", "09:15", 40)

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data,
                    "get_stock_historical_data",
                    side_effect=fetch_once_fails,
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data.time, "sleep"),
            ):
                snapshot = nse_data.get_market_snapshot("ACE")

        self.assertIn("D", snapshot.histories)
        self.assertIn("5", snapshot.histories)
        self.assertGreaterEqual(len(calls), 3)

    def test_resample_aligns_to_nse_session_open(self):
        source = _history(
            [
                "2026-07-29 09:15",
                "2026-07-29 09:20",
                "2026-07-29 09:25",
                "2026-07-29 09:30",
                "2026-07-29 09:35",
                "2026-07-29 09:40",
            ],
            close=[101, 102, 103, 104, 105, 106],
            volume=10,
        )
        fifteen = nse_data.resample_intraday(source, 15)
        thirty = nse_data.resample_intraday(source, 30)
        self.assertEqual(
            fifteen["datetime"].dt.strftime("%H:%M").tolist(),
            ["09:15", "09:30"],
        )
        self.assertEqual(fifteen["open"].tolist(), [100.0, 100.0])
        self.assertEqual(fifteen["close"].tolist(), [103.0, 106.0])
        self.assertEqual(fifteen["volume"].tolist(), [30, 30])
        self.assertEqual(
            thirty["datetime"].dt.strftime("%H:%M").tolist(),
            ["09:15"],
        )
        self.assertEqual(thirty["close"].tolist(), [106.0])
        self.assertEqual(thirty["volume"].tolist(), [60])

    def test_post_close_snapshot_refreshes_live_cache_before_accepting_closing_candles(self):
        live_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        closed_at = datetime(2026, 7, 29, 15, 46, tzinfo=IST)
        phase = {"closed": False}

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            if interval == "D":
                close = 110.0 if phase["closed"] else 101.0
                result = _history(["2026-07-28", "2026-07-29"])
                result.loc[result.index[-1], "close"] = close
                return result
            if phase["closed"]:
                return _session_5m_bars("2026-07-29", "09:15", 75)
            return _session_5m_bars(
                "2026-07-29", "09:15", 46, incomplete_tail="13:05"
            )

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", side_effect=[live_at, closed_at]),
                patch.object(nse_data.time, "sleep"),
            ):
                live_snapshot = nse_data.get_market_snapshot("ACE")
                phase["closed"] = True
                closed_snapshot = nse_data.get_market_snapshot("ACE")

        self.assertEqual(len(live_snapshot.histories["D"]), 1)
        self.assertEqual(len(closed_snapshot.histories["D"]), 2)
        self.assertEqual(closed_snapshot.histories["D"]["close"].iloc[-1], 110.0)
        self.assertEqual(closed_snapshot.provenance["D"]["market_phase"], "closed")
        self.assertNotEqual(
            live_snapshot.provenance["D"]["cache_key"],
            closed_snapshot.provenance["D"]["cache_key"],
        )
        self.assertTrue(
            closed_snapshot.provenance["D"]["cache_key"].startswith("hist_v3_")
        )

    def test_closing_grace_period_does_not_accept_the_provider_tail_as_final(self):
        observed_at = datetime(2026, 7, 29, 15, 31, tzinfo=IST)

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return _history(["2026-07-29 15:25", "2026-07-29 15:30"])

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data.time, "sleep"),
            ):
                snapshot = nse_data.get_market_snapshot("ACE")

        self.assertEqual(snapshot.provenance["D"]["market_phase"], "closing")
        self.assertEqual(
            snapshot.provenance["D"]["cache_ttl_seconds"],
            nse_data.INTRADAY_CACHE_TTL_MINUTES * 60,
        )
        self.assertEqual(len(snapshot.histories["D"]), 1)
        self.assertEqual(len(snapshot.histories["5"]), 1)

    def test_snapshot_reports_whether_each_timeframe_came_from_cache(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            if interval == "D":
                return _history(["2026-07-28", "2026-07-29"])
            return _session_5m_bars("2026-07-29", "09:15", 40)

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(
                    nse_data, "get_stock_historical_data", side_effect=fetch_history
                ),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data.time, "sleep"),
            ):
                fetched = nse_data.get_market_snapshot("ACE")
                cached = nse_data.get_market_snapshot("ACE")

        for interval in ("D", "5"):
            self.assertFalse(fetched.provenance[interval]["cache_hit"])
            self.assertTrue(cached.provenance[interval]["cache_hit"])
            self.assertEqual(
                fetched.provenance[interval]["fetched_at"],
                cached.provenance[interval]["fetched_at"],
            )
        for interval in ("15", "30"):
            self.assertEqual(fetched.provenance[interval]["source"], "derived_from_5m")
            self.assertTrue(cached.provenance[interval]["cache_hit"])
            self.assertIn("elapsed_ms", fetched.provenance[interval])
        self.assertIn("D", fetched.metadata()["timing_ms"])
        self.assertIn("5", fetched.metadata()["timing_ms"])
        self.assertIn("15", fetched.metadata()["timing_ms"])
        self.assertGreaterEqual(fetched.metadata()["timing_total_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
