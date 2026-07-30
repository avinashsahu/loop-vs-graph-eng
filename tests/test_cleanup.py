import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cleanup import (
    partition_scan_run_lines,
    partition_trade_log_records,
    prune_eval_results,
    prune_stale_cache,
    trade_log_cutoff,
)
from market_time import IST


class CleanupPartitionTests(unittest.TestCase):
    def test_trade_log_keeps_recent_and_latest_overnight(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=IST)
        old = (now - timedelta(days=120)).isoformat()
        recent = (now - timedelta(days=2)).isoformat()
        records = [
            {"timestamp": old, "scan_label": "manual", "symbol": "OLD"},
            {
                "timestamp": old,
                "scan_label": "overnight_20260301_2200",
                "symbol": "KEEP",
            },
            {"timestamp": recent, "scan_label": "manual", "symbol": "RECENT"},
        ]
        kept, removed = partition_trade_log_records(
            records, retention_days=90, now=now
        )
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["symbol"], "OLD")
        kept_symbols = {record["symbol"] for record in kept}
        self.assertEqual(kept_symbols, {"KEEP", "RECENT"})

    def test_scan_run_lines_use_recorded_at(self):
        now = datetime(2026, 7, 30, 12, 0, tzinfo=IST)
        old_line = json.dumps(
            {"recorded_at": (now - timedelta(days=100)).isoformat(), "event": "x"}
        )
        new_line = json.dumps(
            {"recorded_at": (now - timedelta(days=1)).isoformat(), "event": "y"}
        )
        kept, removed = partition_scan_run_lines(
            [old_line + "\n", new_line + "\n"],
            retention_days=90,
            now=now,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(removed), 1)
        self.assertIn("y", kept[0])

    def test_prune_stale_cache_dry_run_counts_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            stale = cache_dir / "stale.json"
            fresh = cache_dir / "fresh.json"
            stale.write_text(
                json.dumps({"fetched_at": 0, "data": {}}),
            )
            fresh.write_text(
                json.dumps({"fetched_at": datetime.now(timezone.utc).timestamp(), "data": {}}),
            )
            summary = prune_stale_cache(cache_dir, max_age_seconds=60, apply=False)
            self.assertEqual(summary.removed_files, 1)
            self.assertTrue(stale.exists())
            self.assertTrue(fresh.exists())

    def test_prune_eval_results_respects_keep_globs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            results_dir = Path(temp_dir)
            keep = results_dir / "gemma4_12b_v1.json"
            drop = results_dir / "smoke.json"
            keep.write_text("{}")
            drop.write_text("{}")
            summary = prune_eval_results(results_dir, keep=("*_v1.json",), apply=True)
            self.assertEqual(summary.removed_files, 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())

    def test_trade_log_cutoff_uses_anchor(self):
        anchor = datetime(2026, 1, 31, tzinfo=IST)
        cutoff = trade_log_cutoff(retention_days=30, now=anchor)
        self.assertEqual(cutoff, datetime(2026, 1, 1, tzinfo=IST))


if __name__ == "__main__":
    unittest.main()
