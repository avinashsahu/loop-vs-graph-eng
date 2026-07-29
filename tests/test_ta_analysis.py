import unittest
from datetime import datetime

import numpy as np
import pandas as pd

import ta_analysis
from ta_analysis import _clip


def _history(rows=60):
    close = np.linspace(100.0, 130.0, rows)
    return pd.DataFrame(
        {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
        }
    )


def _bullish_history():
    close = np.concatenate(
        (
            np.linspace(100.0, 110.0, 40),
            np.linspace(110.0, 105.0, 10),
            np.linspace(105.0, 115.0, 10),
        )
    )
    return pd.DataFrame(
        {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
        }
    )


class ClipTests(unittest.TestCase):
    def test_nan_is_neutral(self):
        self.assertEqual(_clip(float("nan")), 0.0)
        self.assertEqual(_clip(np.nan), 0.0)

    def test_valid_values_keep_default_clipping_behavior(self):
        self.assertEqual(_clip(-2.0), -1.0)
        self.assertEqual(_clip(0.25), 0.25)
        self.assertEqual(_clip(2.0), 1.0)

    def test_valid_values_keep_custom_clipping_behavior(self):
        self.assertEqual(_clip(-1.0, lo=0.0, hi=10.0), 0.0)
        self.assertEqual(_clip(4.0, lo=0.0, hi=10.0), 4.0)
        self.assertEqual(_clip(11.0, lo=0.0, hi=10.0), 10.0)


class TechnicalAssessmentTests(unittest.TestCase):
    def test_replay_selects_versioned_policies_once_per_symbol_day_and_applies_cost(self):
        histories = {
            timeframe: _bullish_history()
            for timeframe in ("D", "30", "15", "5")
        }
        benchmark = _history()
        observations = ta_analysis.TechnicalObservations(
            histories=histories,
            benchmark_daily=benchmark,
            delivery_trend={
                "status": "ready",
                "delivery_pct_trend": "rising",
                "delivery_volume_trend": "rising",
                "recent_avg_total_volume": 200_000.0,
                "baseline_avg_total_volume": 100_000.0,
                "latest_vwap": 110.0,
                "interpretation": "possible_accumulation",
            },
        )
        samples = [
            ta_analysis.TechnicalReplaySample(
                symbol="ACE",
                observed_at=datetime(2026, 7, 29, 10, 0),
                observations=observations,
                forward_return_pct=4.0,
            ),
            ta_analysis.TechnicalReplaySample(
                symbol="ACE",
                observed_at=datetime(2026, 7, 29, 14, 0),
                observations=observations,
                forward_return_pct=8.0,
            ),
        ]

        replay = ta_analysis.replay_technical_policies(
            samples,
            policy_ids=(
                "technical-confluence-v1",
                "technical-relative-participation-v2",
            ),
            round_trip_cost_bps=20.0,
        )

        self.assertEqual(
            set(replay),
            {
                "technical-confluence-v1",
                "technical-relative-participation-v2",
            },
        )
        revised = replay["technical-relative-participation-v2"]
        self.assertEqual(revised.sample_count, 1)
        self.assertEqual(revised.signal_count, 1)
        self.assertAlmostEqual(revised.gross_return_pct, 4.0)
        self.assertAlmostEqual(revised.net_return_pct, 3.8)
        policy = ta_analysis.select_technical_policy(revised.policy_id)
        self.assertGreaterEqual(
            policy.timeframe_weight("D"),
            sum(
                policy.timeframe_weight(timeframe)
                for timeframe in ("30", "15", "5")
            ),
        )

    def test_revised_policy_requires_volume_to_confirm_delivery_percentage(self):
        histories = {
            timeframe: _bullish_history()
            for timeframe in ("D", "30", "15", "5")
        }
        benchmark_close = np.linspace(100.0, 160.0, 60)
        benchmark = pd.DataFrame(
            {
                "close": benchmark_close,
                "high": benchmark_close + 1.0,
                "low": benchmark_close - 1.0,
            }
        )
        policy = ta_analysis.select_technical_policy(
            "technical-relative-participation-v2"
        )
        percentage_only = ta_analysis.evaluate_technical(
            ta_analysis.TechnicalObservations(
                histories=histories,
                benchmark_daily=benchmark,
                delivery_trend={
                    "status": "ready",
                    "delivery_pct_trend": "rising",
                    "delivery_volume_trend": "falling",
                    "recent_avg_total_volume": 200_000.0,
                    "baseline_avg_total_volume": 100_000.0,
                    "latest_vwap": 110.0,
                    "interpretation": "delivery_pct_rise_unconfirmed_by_volume",
                },
            ),
            policy,
        )
        volume_confirmed = ta_analysis.evaluate_technical(
            ta_analysis.TechnicalObservations(
                histories=histories,
                benchmark_daily=benchmark,
                delivery_trend={
                    "status": "ready",
                    "delivery_pct_trend": "rising",
                    "delivery_volume_trend": "rising",
                    "recent_avg_total_volume": 200_000.0,
                    "baseline_avg_total_volume": 100_000.0,
                    "latest_vwap": 110.0,
                    "interpretation": "possible_accumulation",
                },
            ),
            policy,
        )

        self.assertLess(
            percentage_only.evidence["families"]["relative_strength"],
            0,
        )
        self.assertEqual(
            percentage_only.evidence["families"]["participation"],
            0.0,
        )
        self.assertEqual(percentage_only.verdict, "BAD")
        self.assertGreater(
            volume_confirmed.evidence["families"]["participation"],
            0,
        )
        self.assertEqual(volume_confirmed.verdict, "GOOD")

    def test_insufficient_daily_history_is_rejected_before_scoring(self):
        histories = {
            "D": _history(rows=20),
            "30": _history(),
            "15": _history(),
            "5": _history(),
        }

        assessment = ta_analysis.evaluate_technical(histories)

        self.assertEqual(assessment.status, "invalid_data")
        self.assertEqual(assessment.verdict, "BAD")
        self.assertIn("INSUFFICIENT_BARS:D", assessment.reason_codes)
        self.assertEqual(assessment.indicators, {})

    def test_non_finite_primary_prices_are_rejected_before_scoring(self):
        histories = {timeframe: _history() for timeframe in ("D", "30", "15", "5")}
        histories["D"].loc[histories["D"].index[-1], "close"] = np.nan

        assessment = ta_analysis.evaluate_technical(histories)

        self.assertEqual(assessment.status, "invalid_data")
        self.assertEqual(assessment.verdict, "BAD")
        self.assertIn("NON_FINITE_BARS:D", assessment.reason_codes)
        self.assertEqual(assessment.indicators, {})

    def test_valid_assessment_keeps_indicator_precision_until_presentation(self):
        histories = {timeframe: _history() for timeframe in ("D", "30", "15", "5")}

        assessment = ta_analysis.evaluate_technical(histories)

        daily_sma20 = assessment.indicators["D"]["sma20"]
        self.assertEqual(assessment.status, "ready")
        self.assertIsInstance(daily_sma20, float)
        self.assertNotEqual(daily_sma20, round(daily_sma20, 2))
        self.assertAlmostEqual(
            assessment.evidence["breakdown"]["D"]["trend_score"],
            round(assessment.evidence["breakdown"]["D"]["trend_score"], 3),
        )

    def test_zero_atr_market_data_is_not_actionable(self):
        history = pd.DataFrame(
            {
                "close": np.full(60, 100.0),
                "high": np.full(60, 100.0),
                "low": np.full(60, 100.0),
            }
        )
        histories = {
            timeframe: history.copy() for timeframe in ("D", "30", "15", "5")
        }

        assessment = ta_analysis.evaluate_technical(histories)

        self.assertEqual(assessment.status, "invalid_data")
        self.assertEqual(assessment.verdict, "BAD")
        self.assertIn("NON_POSITIVE_ATR:D", assessment.reason_codes)


if __name__ == "__main__":
    unittest.main()
