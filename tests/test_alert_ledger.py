import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import alert_ledger
from alert_ledger import AlertLedger, AlertLedgerStateError


class AlertLedgerTests(unittest.TestCase):
    def test_unchanged_actionable_status_is_delivered_once_across_process_restarts(self):
        delivered = []

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "intraday-alerts.json"
            first_process = AlertLedger(path)
            first_process.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={"slack": lambda: delivered.append("slack")},
            )
            first_process.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={"slack": lambda: delivered.append("slack")},
            )

            restarted_process = AlertLedger(path)
            restarted_process.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={"slack": lambda: delivered.append("slack")},
            )

        self.assertEqual(delivered, ["slack"])

    def test_failed_channel_retries_without_repeating_successful_channel(self):
        delivered = []
        slack_attempts = {"count": 0}

        def send_slack():
            slack_attempts["count"] += 1
            if slack_attempts["count"] == 1:
                raise RuntimeError("temporary webhook failure")
            delivered.append("slack")

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AlertLedger(Path(temp_dir) / "intraday-alerts.json")
            first = ledger.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={
                    "email": lambda: delivered.append("email"),
                    "slack": send_slack,
                },
            )
            second = ledger.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={
                    "email": lambda: delivered.append("email"),
                    "slack": send_slack,
                },
            )

        self.assertEqual(first["email"], "delivered")
        self.assertTrue(first["slack"].startswith("failed:"))
        self.assertEqual(second["email"], "skipped_unchanged")
        self.assertEqual(second["slack"], "delivered")
        self.assertEqual(delivered, ["email", "slack"])

    def test_return_to_actionable_after_abort_creates_a_new_alert_transition(self):
        delivered = []

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AlertLedger(Path(temp_dir) / "intraday-alerts.json")
            for status in ("proposed", "aborted", "proposed"):
                ledger.observe_and_deliver(
                    run_id="overnight_20260729_0100",
                    symbol="ACE",
                    status=status,
                    channels={"slack": lambda: delivered.append("slack")},
                )

        self.assertEqual(delivered, ["slack", "slack"])

    def test_overlapping_rechecks_do_not_deliver_the_same_transition_twice(self):
        delivered = []
        start = threading.Barrier(2)

        def recheck(path):
            start.wait()

            def send_slack():
                delivered.append("slack")
                time.sleep(0.05)

            AlertLedger(path).observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={"slack": send_slack},
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "intraday-alerts.json"
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: recheck(path), range(2)))

        self.assertEqual(delivered, ["slack"])

    def test_malformed_state_fails_closed_without_replaying_alerts(self):
        delivered = []

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "intraday-alerts.json"
            path.write_text('{"version": 1, "alerts": {"broken": {}}}')

            with self.assertRaises(AlertLedgerStateError):
                AlertLedger(path).observe_and_deliver(
                    run_id="overnight_20260729_0100",
                    symbol="ACE",
                    status="proposed",
                    channels={"slack": lambda: delivered.append("slack")},
                )

        self.assertEqual(delivered, [])

    def test_enabling_a_channel_later_delivers_the_current_unchanged_transition(self):
        delivered = []

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AlertLedger(Path(temp_dir) / "intraday-alerts.json")
            ledger.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={},
            )
            ledger.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={"slack": lambda: delivered.append("slack")},
            )

        self.assertEqual(delivered, ["slack"])

    def test_post_send_persistence_failure_is_exposed_as_uncertain_delivery(self):
        delivered = []

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AlertLedger(Path(temp_dir) / "intraday-alerts.json")
            ledger.observe_and_deliver(
                run_id="overnight_20260729_0100",
                symbol="ACE",
                status="proposed",
                channels={},
            )

            with patch.object(alert_ledger.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    ledger.observe_and_deliver(
                        run_id="overnight_20260729_0100",
                        symbol="ACE",
                        status="proposed",
                        channels={"slack": lambda: delivered.append("slack")},
                    )

        self.assertEqual(delivered, ["slack"])


if __name__ == "__main__":
    unittest.main()
