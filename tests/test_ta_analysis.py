import unittest

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
