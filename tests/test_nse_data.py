import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import nse_data


IST = ZoneInfo("Asia/Kolkata")


def _history(timestamps):
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(timestamps),
            "open": [100.0] * len(timestamps),
            "high": [102.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": [101.0] * len(timestamps),
            "volume": [1_000] * len(timestamps),
        }
    )


class MarketSnapshotTests(unittest.TestCase):
    def test_live_snapshot_excludes_unfinished_daily_and_intraday_candles(self):
        observed_at = datetime(2026, 7, 29, 13, 8, tzinfo=IST)
        histories = {
            "D": _history(["2026-07-28", "2026-07-29"]),
            30: _history(["2026-07-29 12:15", "2026-07-29 12:45", "2026-07-29 13:08"]),
            15: _history(["2026-07-29 12:30", "2026-07-29 12:45", "2026-07-29 13:08"]),
            5: _history(["2026-07-29 12:55", "2026-07-29 13:00", "2026-07-29 13:08"]),
        }

        def fetch_history(symbol, start_datetime, end_datetime, interval):
            return histories[interval].copy()

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(nse_data, "get_stock_historical_data", side_effect=fetch_history),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data, "now_host_local", return_value=observed_at.replace(tzinfo=None)),
                patch.object(nse_data.time, "sleep"),
            ):
                snapshot = nse_data.get_market_snapshot("ACE")

        self.assertEqual(
            snapshot.histories["D"]["datetime"].dt.strftime("%Y-%m-%d").tolist(),
            ["2026-07-28"],
        )
        for interval in ("30", "15", "5"):
            self.assertEqual(len(snapshot.histories[interval]), 2)
            self.assertEqual(snapshot.provenance[interval]["dropped_incomplete_bars"], 1)
        self.assertEqual(snapshot.provenance["D"]["dropped_incomplete_bars"], 1)
        self.assertEqual(snapshot.observed_at, observed_at.isoformat())

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
            tail = "2026-07-29 15:30" if phase["closed"] else "2026-07-29 13:08"
            return _history(["2026-07-29 12:45", tail])

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(nse_data, "get_stock_historical_data", side_effect=fetch_history),
                patch.object(nse_data, "now_ist", side_effect=[live_at, closed_at]),
                patch.object(nse_data, "now_host_local", return_value=live_at.replace(tzinfo=None)),
                patch.object(nse_data.time, "sleep"),
            ):
                live_snapshot = nse_data.get_market_snapshot("ACE")
                phase["closed"] = True
                closed_snapshot = nse_data.get_market_snapshot("ACE")

        self.assertEqual(len(live_snapshot.histories["D"]), 1)
        self.assertEqual(len(closed_snapshot.histories["D"]), 2)
        self.assertEqual(closed_snapshot.histories["D"]["close"].iloc[-1], 110.0)
        self.assertEqual(
            closed_snapshot.histories["5"]["datetime"].iloc[-1],
            pd.Timestamp("2026-07-29 15:30"),
        )
        self.assertEqual(closed_snapshot.provenance["D"]["market_phase"], "closed")
        self.assertNotEqual(
            live_snapshot.provenance["D"]["cache_key"],
            closed_snapshot.provenance["D"]["cache_key"],
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
                patch.object(nse_data, "get_stock_historical_data", side_effect=fetch_history),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data, "now_host_local", return_value=observed_at.replace(tzinfo=None)),
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
            return _history(["2026-07-29 12:45", "2026-07-29 13:08"])

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.object(nse_data.cache, "CACHE_DIR", cache_dir),
                patch.object(nse_data, "get_stock_historical_data", side_effect=fetch_history),
                patch.object(nse_data, "now_ist", return_value=observed_at),
                patch.object(nse_data, "now_host_local", return_value=observed_at.replace(tzinfo=None)),
                patch.object(nse_data.time, "sleep"),
            ):
                fetched = nse_data.get_market_snapshot("ACE")
                cached = nse_data.get_market_snapshot("ACE")

        for interval in ("D", "30", "15", "5"):
            self.assertFalse(fetched.provenance[interval]["cache_hit"])
            self.assertTrue(cached.provenance[interval]["cache_hit"])
            self.assertEqual(
                fetched.provenance[interval]["fetched_at"],
                cached.provenance[interval]["fetched_at"],
            )


if __name__ == "__main__":
    unittest.main()
