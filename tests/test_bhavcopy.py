import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd

import bhavcopy


def _bhavcopy_frame(
    trade_date,
    *,
    previous_close=100.0,
    close=105.0,
    volume=1_000.0,
    delivery_volume=500.0,
    delivery_pct=50.0,
):
    return pd.DataFrame(
        [
            {
                "symbol": "ACE",
                "date": trade_date,
                "previous_close": previous_close,
                "open": 101.0,
                "high": 106.0,
                "low": 99.0,
                "close": close,
                "vwap": 103.0,
                "volume": volume,
                "turnover": 103_000.0,
                "delivery_volume": delivery_volume,
                "delivery_pct": delivery_pct,
            }
        ]
    )


class BackfillTests(unittest.TestCase):
    def test_backfill_does_not_request_todays_unpublished_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"
            requested_dates = []

            def fetch_archive(*, series, trade_date):
                requested_dates.append(trade_date)
                return _bhavcopy_frame(trade_date)

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
                patch.object(
                    bhavcopy,
                    "now_ist",
                    return_value=datetime(2026, 7, 29, 12, 58),
                ),
            ):
                result = bhavcopy.backfill(days=1, delay_seconds=0)

            self.assertEqual(result.available_days, 1)
            self.assertEqual(requested_dates, [date(2026, 7, 28)])

    def test_backfill_is_throttled_and_resumes_without_redownloading(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"
            fetched_dates = []

            def fetch_archive(*, series, trade_date):
                self.assertEqual(series, "EQ")
                fetched_dates.append(trade_date)
                return _bhavcopy_frame(trade_date)

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
                patch.object(bhavcopy.time, "sleep") as sleep,
            ):
                first = bhavcopy.backfill(
                    days=3,
                    delay_seconds=1.25,
                    start_date=date(2026, 7, 29),
                )
                second = bhavcopy.backfill(
                    days=3,
                    delay_seconds=1.25,
                    start_date=date(2026, 7, 29),
                )

            self.assertEqual(first.available_days, 3)
            self.assertEqual(first.downloaded_days, 3)
            self.assertEqual(first.existing_days, 0)
            self.assertEqual(second.available_days, 3)
            self.assertEqual(second.downloaded_days, 0)
            self.assertEqual(second.existing_days, 3)
            self.assertEqual(
                fetched_dates,
                [date(2026, 7, 29), date(2026, 7, 28), date(2026, 7, 27)],
            )
            self.assertEqual(sleep.call_count, 3)
            sleep.assert_called_with(1.25)

    def test_backfill_counts_unique_archive_dates_when_nse_returns_a_prior_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"
            requested_dates = []

            def fetch_archive(*, series, trade_date):
                requested_dates.append(trade_date)
                if trade_date == date(2026, 7, 29):
                    return _bhavcopy_frame(date(2026, 7, 28))
                return _bhavcopy_frame(trade_date)

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
                patch.object(bhavcopy.time, "sleep"),
            ):
                result = bhavcopy.backfill(
                    days=2,
                    delay_seconds=1,
                    start_date=date(2026, 7, 29),
                )

            self.assertEqual(result.available_days, 2)
            self.assertEqual(result.downloaded_days, 2)
            self.assertEqual(result.existing_days, 0)
            self.assertEqual(
                requested_dates,
                [date(2026, 7, 29), date(2026, 7, 27)],
            )


class DeliveryTrendTests(unittest.TestCase):
    def test_delivery_trend_requires_the_complete_recent_and_baseline_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"

            def fetch_archive(*, series, trade_date):
                return _bhavcopy_frame(trade_date)

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
            ):
                first_date = date(2026, 6, 1)
                for offset in range(24):
                    bhavcopy.fetch_and_store_day(first_date + timedelta(days=offset))

                trend = bhavcopy.get_delivery_trend("ACE")

            self.assertEqual(trend["status"], "insufficient_history")
            self.assertEqual(trend["days_of_history"], 24)
            self.assertEqual(trend["required_days"], 25)

    def test_rising_delivery_volume_with_rising_price_is_possible_accumulation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"
            first_date = date(2026, 6, 1)

            def fetch_archive(*, series, trade_date):
                offset = (trade_date - first_date).days
                if offset < 20:
                    return _bhavcopy_frame(
                        trade_date,
                        previous_close=100.0,
                        close=100.0,
                        delivery_volume=400.0,
                        delivery_pct=40.0,
                    )
                return _bhavcopy_frame(
                    trade_date,
                    previous_close=100.0,
                    close=110.0,
                    delivery_volume=700.0,
                    delivery_pct=55.0,
                )

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
            ):
                for offset in range(25):
                    bhavcopy.fetch_and_store_day(first_date + timedelta(days=offset))

                trend = bhavcopy.get_delivery_trend("ACE")

            self.assertEqual(trend["status"], "ready")
            self.assertEqual(trend["delivery_pct_trend"], "rising")
            self.assertEqual(trend["delivery_volume_trend"], "rising")
            self.assertEqual(trend["recent_price_change_pct"], 10.0)
            self.assertEqual(trend["interpretation"], "possible_accumulation")

    def test_rising_delivery_percentage_without_volume_is_not_called_accumulation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/bhavcopy.db"
            first_date = date(2026, 6, 1)

            def fetch_archive(*, series, trade_date):
                offset = (trade_date - first_date).days
                if offset < 20:
                    return _bhavcopy_frame(
                        trade_date,
                        delivery_volume=500.0,
                        delivery_pct=40.0,
                    )
                return _bhavcopy_frame(
                    trade_date,
                    previous_close=100.0,
                    close=110.0,
                    volume=800.0,
                    delivery_volume=440.0,
                    delivery_pct=55.0,
                )

            with (
                patch.object(bhavcopy, "BHAVCOPY_DB_PATH", database_path),
                patch.object(
                    bhavcopy.archives,
                    "get_daily_bhavcopy_and_deliverables_data",
                    side_effect=fetch_archive,
                ),
            ):
                for offset in range(25):
                    bhavcopy.fetch_and_store_day(first_date + timedelta(days=offset))

                trend = bhavcopy.get_delivery_trend("ACE")

            self.assertEqual(trend["delivery_pct_trend"], "rising")
            self.assertEqual(trend["delivery_volume_trend"], "falling")
            self.assertEqual(
                trend["interpretation"],
                "delivery_pct_rise_unconfirmed_by_volume",
            )


if __name__ == "__main__":
    unittest.main()
