import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evaluation import EvaluationLedger


def _decision_record():
    return {
        "timestamp": "2026-07-29T10:15:00+05:30",
        "scan_label": "nifty-next-50",
        "symbol": "TECHM",
        "status": "proposed",
        "technical_assessment": {
            "evidence": {"score": 0.72},
        },
        "technical_verdict": "BULLISH",
        "fundamental_verdict": "PASS",
        "risk_verdict": "PASS",
        "sentiment_verdict": "POSITIVE",
        "policy_version": "technical-v1+risk-v1+prompts-v1",
        "model_config": {
            "backend": "openai_compatible_local",
            "name": "finance-model",
            "max_tokens": 640,
            "fundamental_max_tokens": 384,
        },
        "risk_plan": {
            "entry_price": 1540.0,
            "stop_price": 1490.0,
            "target_price": 1640.0,
            "shares": 10,
        },
    }


class EvaluationLedgerTests(unittest.TestCase):
    def test_recording_a_decision_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/evaluation.db"
            ledger = EvaluationLedger(database_path)
            record = _decision_record()

            first = ledger.record_decision(record)
            record["status"] = "aborted"
            second = ledger.record_decision(record)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.decision_id, second.decision_id)
            self.assertEqual(
                ledger.status_counts(),
                {"proposed": 1},
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE decisions
                    SET llm_max_tokens = 640,
                        fundamental_llm_max_tokens = NULL
                    """
                )
            ledger = EvaluationLedger(database_path)
            [model_config] = ledger.calibration_report()["decisions"][
                "model_configs"
            ]
            self.assertEqual(model_config["max_tokens"], 384)

    def test_scan_accounting_and_canonical_daily_signal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            first_record = _decision_record()
            first_record["risk_plan"]["stop_price"] = None
            ledger.record_decision(first_record)
            canonical_record = _decision_record()
            canonical_record["timestamp"] = "2026-07-29T11:15:00+05:30"
            canonical = ledger.record_decision(canonical_record)
            repeat_record = _decision_record()
            repeat_record["timestamp"] = "2026-07-29T14:15:00+05:30"
            repeat_record["scan_label"] = "intraday-recheck"
            ledger.record_decision(repeat_record)

            scan = ledger.start_scan_run(
                "nifty-next-50",
                ["TECHM", "TITAN", "HAL"],
                first_record["policy_version"],
                started_at="2026-07-29T10:00:00+05:30",
            )
            ledger.record_scan_symbol(
                scan.run_id,
                "TECHM",
                decision_id=canonical.decision_id,
            )
            ledger.record_scan_symbol(
                scan.run_id,
                "TITAN",
                error=RuntimeError("quote fetch failed"),
            )
            ledger.finalize_scan_run(scan.run_id)
            stale_scan = ledger.start_scan_run(
                "stale-run",
                ["SBICARD"],
                first_record["policy_version"],
                started_at="2026-07-28T10:00:00+05:30",
            )
            recovered_count = ledger.finalize_stale_scan_runs(
                "2026-07-29T10:00:00+05:30"
            )
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy (
                        symbol, date, open, high, low, close
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1545, 1600, 1520, 1580),
                        ("JUNIORBEES", "2026-07-30", 100, 102, 99, 101),
                    ],
                )

            run_summary = ledger.scan_run_summary(scan.run_id)
            stale_summary = ledger.scan_run_summary(stale_scan.run_id)
            outcome_summary = ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
            )
            decision_summary = ledger.calibration_report()["decisions"]
            self.assertEqual(
                run_summary["counts"],
                {"completed": 1, "failed": 2},
            )
            self.assertIsNotNone(run_summary["completed_at"])
            self.assertEqual(
                run_summary["symbols"][1]["error_type"],
                "RuntimeError",
            )
            self.assertEqual(
                run_summary["symbols"][2]["error_type"],
                "IncompleteScan",
            )
            self.assertEqual(recovered_count, 1)
            self.assertEqual(
                stale_summary["symbols"][0]["error_type"],
                "RecoveredIncompleteScan",
            )
            self.assertEqual(decision_summary["raw_evaluable"], 2)
            self.assertEqual(decision_summary["evaluable"], 1)
            self.assertEqual(decision_summary["repeated_evaluable"], 1)
            self.assertEqual(
                decision_summary["canonical"][0]["decision_id"],
                canonical.decision_id,
            )
            self.assertEqual(outcome_summary.completed, 1)
            self.assertEqual(outcome_summary.skipped_unevaluable, 0)

    def test_outcome_uses_only_future_sessions_and_applies_a_gap_aware_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
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
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy (
                        symbol, date, open, high, low, close
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-29", 1540, 1700, 1400, 1600),
                        ("TECHM", "2026-07-30", 1530, 1560, 1480, 1500),
                        ("TECHM", "2026-07-31", 1460, 1580, 1470, 1570),
                        ("JUNIORBEES", "2026-07-30", 100, 102, 99, 101),
                        ("JUNIORBEES", "2026-07-31", 101, 106, 100, 105),
                    ],
                )

            summary = ledger.update_outcomes(
                bhavcopy_path,
                horizons=(2,),
                benchmark_symbol="JUNIORBEES",
                round_trip_cost_bps=30,
            )
            [outcome] = ledger.decision_outcomes(receipt.decision_id)

            self.assertEqual(summary.completed, 1)
            self.assertEqual(outcome["horizon_sessions"], 2)
            self.assertEqual(outcome["horizon_date"], "2026-07-31")
            self.assertEqual(outcome["stop_hit"], 1)
            self.assertEqual(outcome["stop_hit_date"], "2026-07-30")
            self.assertAlmostEqual(outcome["exit_price"], 1490.0)
            self.assertAlmostEqual(outcome["gross_return_pct"], -3.246753, places=5)
            self.assertAlmostEqual(outcome["net_return_pct"], -3.546753, places=5)
            self.assertAlmostEqual(outcome["benchmark_return_pct"], 5.0)
            self.assertAlmostEqual(outcome["excess_return_pct"], -8.546753, places=5)
            self.assertAlmostEqual(outcome["mfe_pct"], 1.298701, places=5)
            self.assertAlmostEqual(outcome["mae_pct"], -3.896104, places=5)
            self.assertEqual(outcome["price_basis"], "raw_unadjusted_bhavcopy")

    def test_target_exits_at_the_planned_price_before_the_horizon_close(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            targetless = _decision_record()
            targetless["timestamp"] = "2026-07-29T10:16:00+05:30"
            targetless["symbol"] = "TITAN"
            targetless["risk_plan"].pop("target_price")
            ledger.record_decision(targetless)
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy (
                        symbol, date, open, high, low, close
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1550, 1650, 1530, 1630),
                        ("TECHM", "2026-07-31", 1600, 1610, 1500, 1510),
                        ("TITAN", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("TITAN", "2026-07-31", 1570, 1590, 1540, 1580),
                        ("JUNIORBEES", "2026-07-30", 100, 102, 99, 101),
                        ("JUNIORBEES", "2026-07-31", 101, 103, 100, 102),
                    ],
                )

            summary = ledger.update_outcomes(
                bhavcopy_path,
                horizons=(2,),
                benchmark_symbol="JUNIORBEES",
                round_trip_cost_bps=0,
            )
            [outcome] = ledger.decision_outcomes(receipt.decision_id)

            self.assertEqual(summary.completed, 2)
            self.assertEqual(outcome["exit_reason"], "target")
            self.assertEqual(outcome["target_hit"], 1)
            self.assertEqual(outcome["target_hit_date"], "2026-07-30")
            self.assertEqual(outcome["stop_hit"], 0)
            self.assertEqual(outcome["exit_price"], 1640.0)
            self.assertAlmostEqual(
                outcome["gross_return_pct"],
                6.493506,
                places=5,
            )
            self.assertEqual(
                ledger.calibration_report()["horizons"]["2"][
                    "target_rate_pct"
                ],
                100.0,
            )
            self.assertEqual(
                ledger.calibration_report()["horizons"]["2"][
                    "target_eligible_count"
                ],
                1,
            )

    def test_same_daily_bar_touching_stop_and_target_uses_stop_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy (
                        symbol, date, open, high, low, close
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1650, 1480, 1600),
                        ("JUNIORBEES", "2026-07-30", 100, 102, 99, 101),
                    ],
                )

            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                benchmark_symbol="JUNIORBEES",
                round_trip_cost_bps=0,
            )
            [outcome] = ledger.decision_outcomes(receipt.decision_id)

            self.assertEqual(outcome["exit_reason"], "both_hit_stop_first")
            self.assertEqual(outcome["stop_hit"], 1)
            self.assertEqual(outcome["target_hit"], 1)
            self.assertEqual(outcome["stop_hit_date"], "2026-07-30")
            self.assertEqual(outcome["target_hit_date"], "2026-07-30")
            self.assertEqual(outcome["exit_price"], 1490.0)

    def test_opening_above_target_precedes_a_later_intraday_stop_touch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy (
                        symbol, date, open, high, low, close
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1660, 1680, 1480, 1550),
                        ("JUNIORBEES", "2026-07-30", 100, 102, 99, 101),
                    ],
                )

            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                benchmark_symbol="JUNIORBEES",
                round_trip_cost_bps=0,
            )
            [outcome] = ledger.decision_outcomes(receipt.decision_id)

            self.assertEqual(outcome["exit_reason"], "target")
            self.assertEqual(outcome["target_hit"], 1)
            self.assertEqual(outcome["stop_hit"], 0)
            self.assertEqual(outcome["exit_price"], 1660.0)

    def test_report_summarizes_completed_outcomes_without_hiding_aborts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            ledger.record_decision(_decision_record())
            aborted = _decision_record()
            aborted["timestamp"] = "2026-07-29T10:16:00+05:30"
            aborted["symbol"] = "TITAN"
            aborted["status"] = "aborted"
            aborted["risk_plan"] = None
            ledger.record_decision(aborted)
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy
                    (symbol, date, open, high, low, close)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("JUNIORBEES", "2026-07-30", 100, 103, 99, 102),
                    ],
                )
            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                round_trip_cost_bps=0,
            )

            report = ledger.calibration_report()

            self.assertEqual(report["decisions"]["total"], 2)
            self.assertEqual(
                report["decisions"]["status_counts"],
                {"aborted": 1, "proposed": 1},
            )
            self.assertEqual(report["decisions"]["evaluable"], 1)
            self.assertEqual(
                report["decisions"]["policy_versions"],
                [
                    {
                        "version": "technical-v1+risk-v1+prompts-v1",
                        "count": 2,
                    }
                ],
            )
            self.assertEqual(
                report["decisions"]["model_configs"],
                [
                    {
                        "backend": "openai_compatible_local",
                        "name": "finance-model",
                        "max_tokens": 384,
                        "count": 2,
                    }
                ],
            )
            self.assertEqual(report["horizons"]["1"]["count"], 1)
            self.assertAlmostEqual(
                report["horizons"]["1"]["mean_net_return_pct"],
                1.948052,
                places=5,
            )
            self.assertEqual(report["horizons"]["1"]["win_rate_pct"], 100.0)
            self.assertEqual(report["horizons"]["1"]["stop_rate_pct"], 0.0)
            self.assertAlmostEqual(
                report["horizons"]["1"]["mean_gross_return_pct"],
                1.948052,
                places=5,
            )
            self.assertEqual(
                report["model_performance"][0]["horizons"]["1"]["count"],
                1,
            )
            self.assertEqual(
                report["model_performance"][0]["backend"],
                "openai_compatible_local",
            )
            self.assertEqual(
                report["model_performance"][0]["name"],
                "finance-model",
            )
            self.assertEqual(report["model_performance"][0]["max_tokens"], 384)
            self.assertEqual(
                report["model_performance"][0]["policy_version"],
                "technical-v1+risk-v1+prompts-v1",
            )
            self.assertEqual(
                report["methodology"]["price_basis"],
                "raw_unadjusted_bhavcopy",
            )
            self.assertEqual(
                report["methodology"]["scope"],
                "selected_candidate_evaluation",
            )
            self.assertEqual(
                report["methodology"]["target_fill"],
                "session open when above target, otherwise target price",
            )
            self.assertEqual(
                report["methodology"]["same_bar_order"],
                "both stop and target touched after open is treated as stop first",
            )
            self.assertEqual(report["methodology"]["cost_assumptions_bps"], [0.0])

    def test_jsonl_import_is_resumable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jsonl_path = Path(temp_dir) / "trade_log.jsonl"
            second = _decision_record()
            second["timestamp"] = "2026-07-29T10:16:00+05:30"
            second["symbol"] = "TITAN"
            jsonl_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (_decision_record(), second)
                )
                + "\n"
            )
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")

            first_import = ledger.import_jsonl(jsonl_path)
            second_import = ledger.import_jsonl(jsonl_path)

            self.assertEqual(first_import.imported, 2)
            self.assertEqual(first_import.existing, 0)
            self.assertEqual(first_import.invalid, 0)
            self.assertEqual(second_import.imported, 0)
            self.assertEqual(second_import.existing, 2)
            self.assertEqual(second_import.invalid, 0)
            self.assertEqual(ledger.status_counts(), {"proposed": 2})

    def test_jsonl_import_skips_a_partial_line_and_keeps_valid_decisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            jsonl_path = Path(temp_dir) / "trade_log.jsonl"
            jsonl_path.write_text(
                json.dumps(_decision_record()) + "\n" + '{"timestamp":'
            )
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")

            result = ledger.import_jsonl(jsonl_path)

            self.assertEqual(result.imported, 1)
            self.assertEqual(result.invalid, 1)
            self.assertEqual(ledger.status_counts(), {"proposed": 1})

    def test_outcome_requires_a_complete_validated_risk_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record = _decision_record()
            record["risk_plan"]["stop_price"] = None
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            ledger.record_decision(record)
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy
                    (symbol, date, open, high, low, close)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("JUNIORBEES", "2026-07-30", 100, 103, 99, 102),
                    ],
                )

            summary = ledger.update_outcomes(bhavcopy_path, horizons=(1,))

            self.assertEqual(summary.completed, 0)
            self.assertEqual(summary.skipped_unevaluable, 1)
            self.assertEqual(ledger.calibration_report()["decisions"]["evaluable"], 0)

    def test_supplied_target_must_be_finite_and_above_entry(self):
        for invalid_target in (1540.0, float("nan"), "not-a-price"):
            with self.subTest(target=invalid_target), tempfile.TemporaryDirectory() as temp_dir:
                record = _decision_record()
                record["risk_plan"]["target_price"] = invalid_target
                ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")

                ledger.record_decision(record)

                self.assertEqual(
                    ledger.calibration_report()["decisions"]["evaluable"],
                    0,
                )

    def test_invalid_benchmark_price_skips_the_horizon_without_aborting_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy
                    (symbol, date, open, high, low, close)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("JUNIORBEES", "2026-07-30", 0, 103, 99, 102),
                    ],
                )

            summary = ledger.update_outcomes(bhavcopy_path, horizons=(1,))

            self.assertEqual(summary.completed, 0)
            self.assertEqual(summary.skipped_incomplete, 1)

    def test_distinct_evaluation_methodologies_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy
                    (symbol, date, open, high, low, close)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("JUNIORBEES", "2026-07-30", 100, 103, 99, 102),
                        ("NIFTYBEES", "2026-07-30", 200, 203, 199, 201),
                    ],
                )

            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                benchmark_symbol="JUNIORBEES",
                round_trip_cost_bps=30,
            )
            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                benchmark_symbol=" juniorbees ",
                round_trip_cost_bps=30.0,
            )
            ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1,),
                benchmark_symbol="NIFTYBEES",
                round_trip_cost_bps=10,
            )

            outcomes = ledger.decision_outcomes(receipt.decision_id)
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(
                {outcome["benchmark_symbol"] for outcome in outcomes},
                {"JUNIORBEES", "NIFTYBEES"},
            )
            report = ledger.calibration_report()
            self.assertEqual(len(report["methodology_performance"]), 2)
            self.assertEqual(report["technical_score_bands"], {})
            self.assertEqual(report["model_performance"], [])
            self.assertTrue(
                all(
                    methodology["technical_score_bands"]
                    for methodology in report["methodology_performance"]
                )
            )
            self.assertTrue(
                all(
                    methodology["model_performance"]
                    for methodology in report["methodology_performance"]
                )
            )

    def test_legacy_decision_migration_backfills_valid_risk_plans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/evaluation.db"
            ledger = EvaluationLedger(database_path)
            ledger.record_decision(_decision_record())
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "ALTER TABLE decisions DROP COLUMN risk_plan_valid"
                )

            migrated = EvaluationLedger(database_path)

            self.assertEqual(
                migrated.calibration_report()["decisions"]["evaluable"],
                1,
            )

    def test_horizons_follow_benchmark_sessions_and_reject_a_missing_stock_bar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvaluationLedger(f"{temp_dir}/evaluation.db")
            receipt = ledger.record_decision(_decision_record())
            bhavcopy_path = f"{temp_dir}/bhavcopy.db"
            with sqlite3.connect(bhavcopy_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE bhavcopy (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        PRIMARY KEY (symbol, date)
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO bhavcopy
                    (symbol, date, open, high, low, close)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TECHM", "2026-07-30", 1540, 1580, 1520, 1570),
                        ("TECHM", "2026-08-01", 1570, 1600, 1560, 1590),
                        ("JUNIORBEES", "2026-07-30", 100, 103, 99, 102),
                        ("JUNIORBEES", "2026-07-31", 102, 104, 101, 103),
                        ("JUNIORBEES", "2026-08-01", 103, 105, 102, 104),
                    ],
                )

            summary = ledger.update_outcomes(
                bhavcopy_path,
                horizons=(1, 2),
            )

            self.assertEqual(summary.completed, 1)
            self.assertEqual(summary.skipped_incomplete, 1)
            [outcome] = ledger.decision_outcomes(receipt.decision_id)
            self.assertEqual(outcome["horizon_sessions"], 1)
            self.assertEqual(outcome["horizon_date"], "2026-07-30")


if __name__ == "__main__":
    unittest.main()
