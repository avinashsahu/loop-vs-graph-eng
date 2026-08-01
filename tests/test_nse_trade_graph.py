import json
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import nse_trade_graph
from evaluation import EvaluationLedger
from fundamental_research import FundamentalDecision
from llm import FundamentalAssessment
from scan_engine import ScanEngine, ScanExecution, ScanPurpose, ScanRequest
from shareholding import ShareholdingHistory, ShareholdingPeriod


class ScanJournalTests(unittest.TestCase):
    def test_stale_run_recovery_fails_only_symbols_without_terminal_events(self):
        current_time = datetime(2026, 7, 29, 18, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        started_at = current_time - timedelta(hours=7)
        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = f"{temp_dir}/scan_runs.jsonl"
            events = [
                {
                    "recorded_at": started_at.isoformat(),
                    "event": "scan_started",
                    "run_id": "run-1",
                    "requested_symbols": ["TECHM", "TITAN"],
                },
                {
                    "recorded_at": started_at.isoformat(),
                    "event": "symbol_completed",
                    "run_id": "run-1",
                    "symbol": "TECHM",
                },
            ]
            with open(journal_path, "w") as journal:
                journal.writelines(
                    json.dumps(event) + "\n" for event in events
                )

            with (
                patch.object(nse_trade_graph, "SCAN_RUN_LOG_PATH", journal_path),
                patch.object(
                    nse_trade_graph,
                    "SCAN_RUN_STALE_AFTER_SECONDS",
                    21600,
                ),
                patch.object(nse_trade_graph, "now_ist", return_value=current_time),
            ):
                nse_trade_graph._recover_stale_scan_events()

            with open(journal_path) as journal:
                recovered = [json.loads(line) for line in journal]

            self.assertEqual(recovered[-2]["event"], "symbol_failed")
            self.assertEqual(recovered[-2]["symbol"], "TITAN")
            self.assertEqual(
                recovered[-2]["error_type"],
                "RecoveredIncompleteScan",
            )
            self.assertEqual(recovered[-1]["event"], "scan_finished")
            self.assertTrue(recovered[-1]["recovered"])


class FundamentalPromptTests(unittest.TestCase):
    def test_prompt_contains_only_grounded_qualitative_evidence(self):
        state = {
            "symbol": "ACE",
            "fundamental_snapshot": {
                "complete": True,
                "company_name": "Action Construction Equipment",
                "eps": 10.0,
                "pat": 100.0,
                "corp_actions": [],
                "corp_announcements": [
                    {
                        "an_dt": "20-Jul-2026",
                        "desc": "Outcome of board meeting",
                        "attchmntText": "Routine quarterly results approved.",
                    }
                ],
                "peer_comparison_quarter": "2026-06",
                "peer_comparison": [
                    {"symbol": "ACE", "eps": 10.0, "pat": 100.0, "pe": 20.0}
                ],
                "financial_history": {
                    "status": "ready",
                    "profile": "banking_nbfc",
                    "subtype": "bank",
                    "periods": [
                        {
                            "period_end": f"{year}-{month_day}",
                            "net_interest_income": 220.0 - index,
                            "operating_profit": 150.0 - index,
                            "provisions": 24.0 + index,
                            "pat": 92.0 - index,
                            "gross_npa_pct": 2.1 + index / 10,
                            "net_npa_pct": 0.6 + index / 10,
                            "return_on_assets_pct": 1.3 - index / 10,
                        }
                        for index, (year, month_day) in enumerate(
                            (
                                ("2026", "06-30"),
                                ("2026", "03-31"),
                                ("2025", "12-31"),
                                ("2025", "09-30"),
                            )
                        )
                    ],
                },
            },
            "delivery_trend": {
                "status": "ready",
                "delivery_pct_trend": "rising",
                "delivery_volume_trend": "falling",
                "interpretation": "delivery_pct_rise_unconfirmed_by_volume",
            },
        }

        history = ShareholdingHistory(
            symbol="ACE",
            periods=tuple(
                ShareholdingPeriod(
                    record_id=str(index),
                    period=period,
                    schema_version="2025-10-31",
                    fii_pct=12.0 + index,
                    dii_pct=18.0 - index,
                    government_pct=0.0,
                    promoter_pct=55.0,
                    other_public_pct=15.0,
                    public_shares=45,
                    component_shares=45,
                    reconciled=True,
                    checksum="abc",
                )
                for index, period in enumerate(
                    (
                        "2026-06-30",
                        "2026-03-31",
                        "2025-12-31",
                        "2025-09-30",
                        "2025-06-30",
                    )
                )
            ),
            latest_period="2026-06-30",
            latest_record_id="0",
            periods_available=5,
            complete=True,
            changes_bps={
                "fii_qoq": -100,
                "dii_qoq": 100,
                "government_qoq": 0,
                "promoter_qoq": 0,
                "other_public_qoq": 0,
                "fii_4q": -400,
                "dii_4q": 400,
                "government_4q": 0,
                "promoter_4q": 0,
                "other_public_4q": 0,
            },
            trend_labels={
                "fii": "falling",
                "dii": "rising",
                "government": "flat",
                "promoter": "flat",
                "other_public": "flat",
            },
        )

        def pass_assessment(_prompt, evidence_ids):
            return FundamentalAssessment(
                verdict="PASS",
                reason_code="NO_MATERIAL_RED_FLAG",
                reason="No qualitative red flags.",
                evidence_ids=evidence_ids[:1],
                missing=(),
            )

        with (
            patch.object(nse_trade_graph, "get_shareholding_history", return_value=history),
            patch.object(
                nse_trade_graph,
                "assess_fundamentals",
                side_effect=pass_assessment,
            ) as assess_fundamentals,
        ):
            route, result_state = nse_trade_graph.node_fundamental(state)

        prompt = assess_fundamentals.call_args.args[0]
        self.assertEqual(route, "risk")
        self.assertEqual(
            result_state["fundamental_assessment"]["reason_code"],
            "NO_MATERIAL_RED_FLAG",
        )
        self.assertIn("ANNOUNCEMENT_", prompt)
        self.assertIn("Outcome of board meeting", prompt)
        self.assertNotIn("SHAREHOLDING_", prompt)
        self.assertNotIn("government_qoq", prompt)
        self.assertNotIn("gross_npa_pct", prompt)
        self.assertNotIn("delivery", prompt.lower())
        self.assertNotIn("delivery_pct_rise_unconfirmed_by_volume", prompt)
        self.assertIn(
            "A cash dividend is a distribution, not dilution",
            prompt,
        )
        self.assertIn(
            "never put evidence IDs in `missing`",
            prompt,
        )
        self.assertIn(
            "PROMOTER_OR_DILUTION: verdict REJECT. Supplied text explicitly states",
            prompt,
        )
        self.assertNotIn("PEER_OR_EARNINGS_WEAKNESS", prompt)

    def test_model_reject_aborts_while_review_remains_visible(self):
        history = ShareholdingHistory(
            symbol="ACE",
            periods=(),
            complete=True,
        )
        evidence = SimpleNamespace(
            payload={"coverage": {"complete": True}},
            ids=("EVIDENCE_1",),
            prompt=lambda: "{}",
            prompt_hash="prompt-hash",
            evidence_hash="evidence-hash",
        )
        cases = (
            (
                "REJECT",
                "ADVERSE_CORPORATE_EVENT",
                "abort",
                nse_trade_graph.node_abort,
                "aborted",
            ),
            (
                "REVIEW",
                "INSUFFICIENT_EVIDENCE",
                "flag_review",
                nse_trade_graph.node_flag_review,
                "flagged_for_review",
            ),
        )
        for verdict, reason_code, expected_route, terminal_node, status in cases:
            with self.subTest(verdict=verdict):
                state = {
                    "symbol": "ACE",
                    "fundamental_snapshot": {
                        "complete": True,
                        "eps": 10.0,
                        "pat": 100.0,
                    },
                }
                decision = FundamentalDecision(
                    verdict=verdict,
                    reason_code=reason_code,
                    reason=f"{verdict} reason.",
                    evidence_ids=("EVIDENCE_1",),
                    missing=(),
                )
                with (
                    patch.object(
                        nse_trade_graph,
                        "get_shareholding_history",
                        return_value=history,
                    ),
                    patch.object(
                        nse_trade_graph,
                        "build_fundamental_evidence",
                        return_value=evidence,
                    ),
                    patch.object(
                        nse_trade_graph,
                        "evaluate_fundamental_research",
                        return_value=decision,
                    ),
                ):
                    route, result_state = nse_trade_graph.node_fundamental(state)

                self.assertEqual(route, expected_route)
                _, result_state = terminal_node(result_state)
                self.assertEqual(result_state["disposition"], verdict)
                self.assertEqual(result_state["status"], status)
                self.assertEqual(
                    result_state["decision_reason"],
                    {"stage": "fundamental", "code": reason_code},
                )

    def test_fetch_stage_does_not_download_fundamentals_before_technical_passes(self):
        state = {
            "symbol": "ACE",
            "iters": 0,
            "fundamental_snapshot": None,
            "delivery_trend": None,
        }
        quote = {
            "name": "Action Construction Equipment",
            "sector": "Capital Goods",
            "changepct": 1.0,
            "previous_close": 100.0,
            "change": 1.0,
            "lower_circuit": 80.0,
            "upper_circuit": 120.0,
        }
        histories = {timeframe: _history() for timeframe in ("D", "30", "15", "5")}
        snapshot = nse_trade_graph.nse_data.MarketSnapshot(
            symbol="ACE",
            observed_at="2026-07-29T13:08:00+05:30",
            histories=histories,
            provenance={
                timeframe: {
                    "source": "NSE chart API",
                    "latest_complete_bar": "2026-07-29T13:00:00",
                }
                for timeframe in histories
            },
        )

        with (
            patch.object(
                nse_trade_graph,
                "get_stock_live_quotes",
                return_value=quote,
            ),
            patch.object(
                nse_trade_graph.nse_data,
                "get_market_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                nse_trade_graph.nse_data,
                "get_multi_timeframe_history",
                side_effect=AssertionError("node_fetch must retain snapshot provenance"),
            ),
            patch.object(
                nse_trade_graph.bhavcopy,
                "get_delivery_trend",
                return_value={"status": "ready"},
            ),
            patch.object(
                nse_trade_graph.fundamentals,
                "get_fundamental_snapshot",
            ) as get_fundamentals,
            patch.object(nse_trade_graph.time, "sleep"),
        ):
            route, result_state = nse_trade_graph.node_fetch(state)

        self.assertEqual(route, "technical")
        self.assertIsNone(result_state["fundamental_snapshot"])
        self.assertEqual(
            result_state["market_snapshot"]["timeframes"]["D"]["source"],
            "NSE chart API",
        )
        get_fundamentals.assert_not_called()


def _history(rows=60):
    close = np.linspace(100.0, 130.0, rows)
    return pd.DataFrame(
        {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
        }
    )


class TechnicalNodeTests(unittest.TestCase):
    def test_invalid_market_data_aborts_with_structured_reason(self):
        state = {
            "iters": 1,
            "hist_multi": {
                "D": _history(rows=20),
                "30": _history(),
                "15": _history(),
                "5": _history(),
            },
            "technical_indicators": None,
            "technical_assessment": None,
            "technical_verdict": None,
        }

        route, result_state = nse_trade_graph.node_technical(state)

        self.assertEqual(route, "abort")
        self.assertEqual(
            result_state["technical_assessment"]["status"],
            "invalid_data",
        )
        self.assertIn(
            "INSUFFICIENT_BARS:D",
            result_state["technical_assessment"]["reason_codes"],
        )
        self.assertIn("invalid_data", result_state["technical_verdict"])

    def test_explanation_failure_does_not_change_locked_disposition(self):
        state = {
            "symbol": "ACE",
            "disposition": "PROPOSE",
            "technical_verdict": "GOOD deterministic",
            "technical_fact_ledger": {
                "schema_version": "technical-explanation-input-v2",
                "status": "ready",
            },
        }

        with patch.object(
            nse_trade_graph,
            "summarize_technical_run",
            side_effect=RuntimeError("backend unavailable"),
        ):
            route, result_state = (
                nse_trade_graph.node_technical_explanation(state)
            )

        self.assertIsNone(route)
        self.assertEqual(result_state["disposition"], "PROPOSE")
        self.assertEqual(
            result_state["technical_verdict"],
            "GOOD deterministic",
        )
        self.assertIsNone(result_state["technical_explanation"])
        self.assertEqual(
            result_state["technical_explanation_meta"]["status"],
            "backend_error",
        )


class MarketSnapshotRecordTests(unittest.TestCase):
    def test_trade_record_retains_market_snapshot_provenance(self):
        snapshot_metadata = {
            "symbol": "ACE",
            "observed_at": "2026-07-29T13:08:00+05:30",
            "timeframes": {
                "D": {
                    "source": "NSE chart API",
                    "fetched_at": "2026-07-29T13:07:59+05:30",
                    "cache_hit": False,
                    "latest_complete_bar": "2026-07-28T00:00:00",
                }
            },
        }
        state = {
            "symbol": "ACE",
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 10.0,
            "atr_stop_multiple": 2.0,
            "reward_risk_ratio": 2.0,
            "iters": 1,
            "status": "aborted",
            "disposition": "REJECT",
            "decision_reason": {
                "stage": "technical",
                "code": "INVALID_MARKET_DATA",
            },
            "proposal": None,
            "market_snapshot": snapshot_metadata,
            "technical_fact_ledger": {"schema_version": "test-v2"},
            "technical_explanation": {
                "verdict": "GOOD",
                "summary": "Deterministic evidence agrees.",
            },
            "technical_explanation_meta": {
                "status": "ready",
                "output_valid": True,
            },
        }

        record = nse_trade_graph.build_record(state)

        self.assertEqual(record["market_snapshot"], snapshot_metadata)
        self.assertEqual(
            record["technical_fact_ledger"]["schema_version"],
            "test-v2",
        )
        self.assertTrue(
            record["technical_explanation_meta"]["output_valid"]
        )
        self.assertEqual(record["reward_risk_ratio"], 2.0)
        self.assertEqual(
            record["model_config"],
            nse_trade_graph.active_model_config(),
        )
        self.assertEqual(
            record["policy_version"],
            nse_trade_graph.NSE_POLICY_VERSION,
        )

    def test_scan_engine_records_the_same_decision_in_the_evaluation_ledger(self):
        state = {
            "symbol": "ACE",
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 10.0,
            "atr_stop_multiple": 2.0,
            "reward_risk_ratio": 2.0,
            "iters": 1,
            "status": "aborted",
            "disposition": "REJECT",
            "decision_reason": {
                "stage": "technical",
                "code": "INVALID_MARKET_DATA",
            },
            "proposal": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            trade_log_path = f"{temp_dir}/trade_log.jsonl"
            evaluation_path = f"{temp_dir}/evaluation.db"
            with (
                patch.object(nse_trade_graph, "TRADE_LOG_PATH", trade_log_path),
                patch.object(nse_trade_graph, "EVALUATION_DB_PATH", evaluation_path),
            ):
                adapter = SimpleNamespace(
                    execute=lambda request: ScanExecution(
                        record=nse_trade_graph.build_record(
                            {**state, "scan_label": request.scan_label}
                        )
                    )
                )
                engine = ScanEngine(
                    adapter,
                    nse_trade_graph.ProductionDecisionStore(
                        trade_log_path,
                        evaluation_path,
                    ),
                )
                result = engine.scan(
                    ScanRequest(
                        symbol="ACE",
                        principal=100_000,
                        scan_label="fixture",
                        purpose=ScanPurpose.BATCH,
                    )
                )

            ledger = EvaluationLedger(evaluation_path)
            self.assertIsNotNone(result.decision_id)
            self.assertEqual(ledger.status_counts(), {"aborted": 1})
            self.assertEqual(
                ledger.calibration_report()["decisions"]["reason_codes"],
                [
                    {
                        "stage": "technical",
                        "code": "INVALID_MARKET_DATA",
                        "count": 1,
                    }
                ],
            )


class RiskNodeTests(unittest.TestCase):
    def test_risk_node_uses_atr_stop_and_maximum_loss_budget(self):
        state = {
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 25.0,
            "atr_stop_multiple": 2.0,
            "reward_risk_ratio": 2.0,
            "quote": {
                "previous_close": 100.0,
                "change": 0.0,
                "lower_circuit": 80.0,
                "upper_circuit": 120.0,
            },
            "technical_indicators": {"D": {"atr14": 5.0}},
            "hist_multi": {
                "5": pd.DataFrame(
                    {
                        "datetime": [pd.Timestamp("2026-07-29 12:00:00")],
                        "low": [80.5],
                        "high": [101.0],
                    }
                )
            },
            "hist": pd.DataFrame({"low": [99.0], "high": [101.0]}),
            "risk_plan": None,
            "risk_verdict": None,
        }

        with patch.object(
            nse_trade_graph,
            "now_ist",
            return_value=datetime(
                2026, 7, 29, 12, 30, tzinfo=ZoneInfo("Asia/Kolkata")
            ),
        ):
            route, result_state = nse_trade_graph.node_risk(state)

        self.assertEqual(route, "sentiment")
        self.assertEqual(result_state["max_shares"], 100)
        self.assertEqual(result_state["position_size"], 10_000.0)
        self.assertEqual(result_state["risk_plan"]["stop_price"], 90.0)
        self.assertEqual(result_state["risk_plan"]["target_price"], 120.0)
        self.assertEqual(
            result_state["risk_plan"]["planned_profit_at_target"],
            2_000.0,
        )
        self.assertEqual(result_state["risk_plan"]["max_loss_at_stop"], 1_000.0)
        self.assertIn("max loss at stop", result_state["risk_verdict"])
        self.assertIn("target=120.00", result_state["risk_verdict"])
        self.assertEqual(
            result_state["circuit_context"]["policy"],
            "current_entry_proximity",
        )
        self.assertTrue(
            result_state["circuit_context"]["lower_band_touched_today"]
        )
        self.assertFalse(
            result_state["circuit_context"]["current_near_lower_circuit"]
        )

        near_circuit_state = {
            **state,
            "quote": {**state["quote"], "change": -19.0},
            "hist_multi": {
                "5": pd.DataFrame(
                    {
                        "datetime": [
                            pd.Timestamp("2026-07-28 15:25:00")
                        ],
                        "low": [80.5],
                        "high": [101.0],
                    }
                )
            },
            "risk_plan": None,
        }
        with patch.object(
            nse_trade_graph,
            "now_ist",
            return_value=datetime(
                2026, 7, 29, 12, 30, tzinfo=ZoneInfo("Asia/Kolkata")
            ),
        ):
            route, rejected_state = nse_trade_graph.node_risk(
                near_circuit_state
            )
        self.assertEqual(route, "abort")
        self.assertEqual(
            rejected_state["decision_reason"],
            {
                "stage": "risk",
                "code": "LOWER_CIRCUIT_ENTRY_PROXIMITY",
            },
        )
        self.assertFalse(
            rejected_state["circuit_context"][
                "same_day_intraday_context_available"
            ]
        )
        self.assertFalse(
            rejected_state["circuit_context"]["lower_band_touched_today"]
        )
        self.assertIn(
            "same-day intraday context unavailable",
            rejected_state["risk_verdict"],
        )

    def test_proposal_states_entry_stop_capital_and_maximum_loss(self):
        state = {
            "symbol": "ACE",
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 25.0,
            "max_shares": 100,
            "position_size": 10_000.0,
            "risk_plan": {
                "entry_price": 100.0,
                "stop_price": 90.0,
                "target_price": 120.0,
                "planned_profit_at_target": 2_000.0,
                "reward_risk_ratio": 2.0,
                "max_loss_at_stop": 1_000.0,
                "risk_budget": 1_000.0,
            },
        }

        route, result_state = nse_trade_graph.node_propose(state)

        self.assertIsNone(route)
        self.assertIn("entry ~₹100.00", result_state["proposal"])
        self.assertIn("stop ₹90.00", result_state["proposal"])
        self.assertIn("target ₹120.00 (2.00R)", result_state["proposal"])
        self.assertIn("planned profit ~₹2000", result_state["proposal"])
        self.assertIn("max loss ~₹1000", result_state["proposal"])
        self.assertNotIn("risk_pct", result_state["proposal"])

    def test_proposal_distinguishes_actual_loss_from_policy_cap(self):
        state = {
            "symbol": "TITAN",
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 10.0,
            "max_shares": 2,
            "position_size": 9_700.0,
            "risk_plan": {
                "entry_price": 4_850.0,
                "stop_price": 4_750.0,
                "target_price": 5_050.0,
                "planned_profit_at_target": 400.0,
                "reward_risk_ratio": 2.0,
                "max_loss_at_stop": 200.0,
                "risk_budget": 1_000.0,
            },
        }

        _, result_state = nse_trade_graph.node_propose(state)

        self.assertIn("0.20% actual", result_state["proposal"])
        self.assertIn("1.0% policy cap", result_state["proposal"])
        self.assertNotIn("₹200 (1.0% of", result_state["proposal"])

    def test_sentiment_gate_uses_atr_instead_of_model_inference(self):
        base_state = {
            "quote": {"changepct": 2.0},
            "risk_plan": {"entry_price": 100.0},
            "technical_indicators": {"D": {"atr14": 2.0}},
        }

        route, state = nse_trade_graph.node_sentiment(base_state)
        self.assertEqual(route, "propose")
        self.assertIn("4.00% review threshold", state["sentiment_verdict"])

        base_state["quote"]["changepct"] = 5.0
        route, _ = nse_trade_graph.node_sentiment(base_state)
        self.assertEqual(route, "flag_review")


if __name__ == "__main__":
    unittest.main()
