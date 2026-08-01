import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import digest


class SlackDigestTests(unittest.TestCase):
    def test_digest_uses_latest_attempt_when_a_symbol_is_retried(self):
        failed = {
            "scan_label": "nifty-50-validation",
            "symbol": "BAJAJ-AUTO",
            "status": "aborted",
            "attempt": 1,
        }
        retried = {
            **failed,
            "attempt": 2,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trade_log.jsonl"
            path.write_text(
                json.dumps(failed) + "\n" + json.dumps(retried) + "\n"
            )
            with patch.object(digest, "TRADE_LOG_PATH", str(path)):
                records = digest._load_records("nifty-50-validation")

        self.assertEqual(records, [retried])

    def test_digest_separates_locked_gates_model_summary_and_freshness(self):
        record = {
            "scan_label": "cached-25-validation",
            "symbol": "360ONE",
            "company_name": "360 ONE WAM Limited",
            "status": "proposed",
            "disposition": "PROPOSE",
            "principal": 100_000.0,
            "max_allocation_pct": 10.0,
            "technical_assessment": {"evidence": {"verdict": "GOOD"}},
            "technical_fact_ledger": {
                "facts": {
                    "TA_DECISION": {
                        "verdict": "GOOD",
                        "score": 1.361,
                        "confluence_ratio": 1.0,
                        "engaged_families": 3,
                        "policy_id": "technical-relative-participation-v2",
                        "policy_fingerprint": "637316df8e81d2b3",
                    },
                    "TA_TREND": {"score": 0.203},
                    "TA_MOMENTUM": {"score": 0.157},
                    "TA_RELATIVE_STRENGTH": {
                        "relative_return_pct": 6.066,
                        "benchmark_symbol": "JUNIORBEES",
                    },
                    "TA_DAILY_CONTEXT": {
                        "close": 1138.0,
                        "sma20": 1115.49,
                        "sma50": 1105.682,
                        "rsi14": 55.27,
                        "macd_hist": 4.064,
                        "atr_pct": 3.02,
                    },
                    "TA_TIMEFRAMES": {
                        "timeframes": {
                            "D": {
                                "trend_score": 0.296,
                                "momentum_score": 0.237,
                            },
                            "30": {
                                "trend_score": 0.196,
                                "momentum_score": -0.172,
                            },
                        }
                    },
                    "TA_PARTICIPATION": {
                        "participation_state": "available_neutral",
                        "recent_avg_delivery_pct": 49.24,
                        "baseline_avg_delivery_pct": 58.47,
                        "recent_avg_delivery_volume": 494_494.0,
                        "baseline_avg_delivery_volume": 862_955.6,
                        "total_volume_expanded": False,
                    },
                    "TA_DATA_QUALITY": {
                        "timeframes": {
                            "D": {
                                "latest_complete_bar": (
                                    "2026-07-30T00:00:00"
                                )
                            }
                        },
                        "delivery": {
                            "latest_session": "2026-07-29",
                            "freshness": (
                                "expected_prior_completed_session"
                            ),
                        },
                        "benchmark": {"sessions_aligned": True},
                    },
                }
            },
            "shareholding_history": {
                "status": "ready",
                "periods_available": 5,
                "latest_period": "2026-06-30",
                "complete": True,
            },
            "technical_explanation": {
                "verdict": "GOOD",
                "summary": "Weighted trend and momentum agree.",
                "drivers": [
                    {
                        "fact_id": "TA_TREND",
                        "statement": (
                            "Weighted multi-timeframe trend evidence is "
                            "positive."
                        ),
                    }
                ],
                "conflicts": ["30-minute momentum is negative."],
                "neutral_context": ["RSI is neutral."],
                "data_notes": [],
            },
            "technical_verdict": "GOOD deterministic fallback",
            "fundamental_assessment": {
                "verdict": "PASS",
                "summary": "No material red flag was identified.",
            },
            "fundamental_evidence": {
                "financial_history": {
                    "selected_scope": "consolidated",
                    "scope_selection_reason": "group_economics",
                    "available_scopes": ["standalone", "consolidated"],
                }
            },
            "fundamental_verdict": "PASS",
            "risk_verdict": "GOOD: position is within policy.",
            "sentiment_verdict": "GOOD: move is within ATR threshold.",
            "risk_plan": {
                "shares": 8,
                "entry_price": 1138.0,
                "stop_price": 1069.26,
                "target_price": 1275.48,
                "reward_risk_ratio": 2.0,
                "capital_required": 9104.0,
                "max_loss_at_stop": 549.92,
                "binding_constraint": "allocation_cap",
            },
            "decision_reason": {
                "stage": "decision",
                "code": "ALL_GATES_PASSED",
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trade_log.jsonl"
            path.write_text(json.dumps(record) + "\n")
            with patch.object(digest, "TRADE_LOG_PATH", str(path)):
                message = digest.build_slack_digest(
                    "cached-25-validation"
                )

        self.assertIn("*NSE Scan — NSE EQUITIES • 30 Jul 2026*", message)
        self.assertNotIn("Overnight", message)
        self.assertIn("*CANDIDATE*", message)
        self.assertIn("TA QUALIFIED", message)
        self.assertIn("Position sizing COMPUTED", message)
        self.assertNotIn("Risk `GOOD`", message)
        self.assertNotIn("Phi-4", message)
        self.assertNotIn("637316df8e81d2b3", message)
        self.assertIn("*TA numbers:* score +1.361", message)
        self.assertIn("RS20D +6.07pp vs JUNIORBEES", message)
        self.assertIn("*Daily TA bar:* Close ₹1138.00", message)
        self.assertIn("*Trend/Momentum by timeframe:* D +0.30/+0.24", message)
        self.assertIn("directional confirmation conditions were not met", message)
        self.assertIn("delivery 29 Jul 2026 (latest prior session)", message)
        self.assertIn("shareholding 5 quarters through Jun 2026 (complete)", message)
        self.assertIn("*Position plan — manual, not placed:*", message)
        self.assertIn(
            "Scope: consolidated (group economics; standalone retained).",
            message,
        )
        self.assertIn("no order was placed", message.lower())

    def test_quarterly_missing_fields_are_not_labelled_as_fiscal_years(self):
        summary = digest._fundamental_summary(
            {
                "fundamental_assessment": {
                    "verdict": "REVIEW",
                    "missing": [
                        "revenue:2026-06-30",
                        "funding_leverage:2026-03-31",
                    ],
                }
            }
        )

        self.assertIn("revenue (Jun 2026)", summary)
        self.assertIn("funding leverage (FY2026)", summary)
        self.assertNotIn("revenue for FY2026", summary)


if __name__ == "__main__":
    unittest.main()
