import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app_scheduler
from app_scheduler import (
    JobRecord,
    bhavcopy_completion_status,
    eligible_bhavcopy_occurrence,
    intraday_occurrence,
    latest_daily_occurrence,
    should_run,
)


IST = ZoneInfo("Asia/Kolkata")


class SchedulerOccurrenceTests(unittest.TestCase):
    def test_bhavcopy_occurrence_tracks_latest_published_weekday(self):
        before_publish = datetime(2026, 7, 30, 14, 0, tzinfo=IST)
        after_publish = datetime(2026, 7, 30, 18, 15, tzinfo=IST)
        monday_morning = datetime(2026, 8, 3, 8, 0, tzinfo=IST)

        self.assertEqual(
            eligible_bhavcopy_occurrence(before_publish, publish_hour=18),
            "2026-07-29",
        )
        self.assertEqual(
            eligible_bhavcopy_occurrence(after_publish, publish_hour=18),
            "2026-07-30",
        )
        self.assertEqual(
            eligible_bhavcopy_occurrence(monday_morning, publish_hour=18),
            "2026-07-31",
        )

    def test_bhavcopy_completion_requires_the_expected_date_until_cutoff(self):
        before_cutoff = datetime(2026, 7, 30, 21, 0, tzinfo=IST)
        after_cutoff = datetime(2026, 7, 30, 23, 0, tzinfo=IST)

        self.assertEqual(
            bhavcopy_completion_status(
                "2026-07-30",
                now=before_cutoff,
                expected_date_exists=False,
            ),
            "failed",
        )
        self.assertEqual(
            bhavcopy_completion_status(
                "2026-07-30",
                now=after_cutoff,
                expected_date_exists=False,
            ),
            "unavailable",
        )
        self.assertEqual(
            bhavcopy_completion_status(
                "2026-07-30",
                now=before_cutoff,
                expected_date_exists=True,
            ),
            "success",
        )

    def test_daily_occurrence_catches_up_only_inside_lateness_window(self):
        self.assertEqual(
            latest_daily_occurrence(
                datetime(2026, 7, 30, 8, 0, tzinfo=IST),
                hour=22,
                minute=0,
                max_lateness=timedelta(hours=11),
                weekdays_only=True,
            ),
            "2026-07-29",
        )
        self.assertIsNone(
            latest_daily_occurrence(
                datetime(2026, 7, 30, 14, 0, tzinfo=IST),
                hour=22,
                minute=0,
                max_lateness=timedelta(hours=11),
                weekdays_only=True,
            )
        )

    def test_intraday_occurrence_is_one_slot_during_market_hours(self):
        self.assertEqual(
            intraday_occurrence(
                datetime(2026, 7, 30, 10, 47, tzinfo=IST),
                interval_minutes=20,
            ),
            "2026-07-30T10:40",
        )
        self.assertIsNone(
            intraday_occurrence(
                datetime(2026, 7, 30, 16, 0, tzinfo=IST),
                interval_minutes=20,
            )
        )


class SchedulerRetryTests(unittest.TestCase):
    def test_success_is_not_repeated_and_failure_obeys_retry_delay(self):
        now = datetime(2026, 7, 30, 18, 30, tzinfo=IST)
        success = JobRecord(
            occurrence="2026-07-30",
            status="success",
            attempted_at=now.isoformat(),
        )
        recent_failure = JobRecord(
            occurrence="2026-07-30",
            status="failed",
            attempted_at=(now - timedelta(minutes=10)).isoformat(),
        )

        self.assertFalse(
            should_run(success, "2026-07-30", now, timedelta(minutes=30))
        )
        self.assertFalse(
            should_run(recent_failure, "2026-07-30", now, timedelta(minutes=30))
        )
        self.assertTrue(
            should_run(
                recent_failure,
                "2026-07-30",
                now + timedelta(minutes=21),
                timedelta(minutes=30),
            )
        )
        unavailable = JobRecord(
            occurrence="2026-07-30",
            status="unavailable",
            attempted_at=now.isoformat(),
        )
        self.assertFalse(
            should_run(unavailable, "2026-07-30", now, timedelta(minutes=30))
        )

    def test_once_returns_nonzero_when_a_due_job_fails(self):
        with TemporaryDirectory() as temporary:
            with (
                patch.object(
                    app_scheduler,
                    "LOCK_PATH",
                    Path(temporary) / "scheduler.lock",
                ),
                patch.object(app_scheduler, "load_state", return_value={}),
                patch.object(app_scheduler, "configured_jobs", return_value=()),
                patch.object(app_scheduler, "run_due_jobs", return_value=1),
            ):
                self.assertEqual(app_scheduler.main(["--once"]), 1)


if __name__ == "__main__":
    unittest.main()
