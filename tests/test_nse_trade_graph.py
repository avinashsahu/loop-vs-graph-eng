import unittest
from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd

import nse_trade_graph


class FundamentalPromptTests(unittest.TestCase):
    def test_delivery_context_is_described_as_non_directional(self):
        state = {
            "symbol": "ACE",
            "fundamental_snapshot": {
                "complete": True,
                "company_name": "Action Construction Equipment",
                "eps": 10.0,
                "pat": 100.0,
            },
            "delivery_trend": {
                "status": "ready",
                "delivery_pct_trend": "rising",
                "delivery_volume_trend": "falling",
                "interpretation": "delivery_pct_rise_unconfirmed_by_volume",
            },
        }

        with patch.object(
            nse_trade_graph,
            "call_llm",
            return_value="GOOD: no fundamental red flags",
        ) as call_llm:
            route, _ = nse_trade_graph.node_fundamental(state)

        prompt = call_llm.call_args.args[0]
        self.assertEqual(route, "risk")
        self.assertIn("does not reveal buyer or seller direction", prompt)
        self.assertIn("supporting market-participation context", prompt)
        self.assertNotIn("rising means more genuine buying interest", prompt)

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
            "upper_circuit": 80.0,
            "lower_circuit": 120.0,
        }
        histories = {timeframe: _history() for timeframe in ("D", "30", "15", "5")}

        with (
            patch.object(
                nse_trade_graph,
                "get_stock_live_quotes",
                return_value=quote,
            ),
            patch.object(
                nse_trade_graph.nse_data,
                "get_multi_timeframe_history",
                return_value=histories,
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


class RiskNodeTests(unittest.TestCase):
    def test_risk_node_uses_atr_stop_and_maximum_loss_budget(self):
        state = {
            "principal": 100_000.0,
            "max_loss_pct": 1.0,
            "max_allocation_pct": 25.0,
            "atr_stop_multiple": 2.0,
            "quote": {
                "previous_close": 100.0,
                "change": 0.0,
                # nsemine exposes these two fields under swapped names.
                "upper_circuit": 80.0,
                "lower_circuit": 120.0,
            },
            "technical_indicators": {"D": {"atr14": 5.0}},
            "hist_multi": {
                "5": pd.DataFrame(
                    {
                        "datetime": [pd.Timestamp("2026-07-29 12:00:00")],
                        "low": [99.0],
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
            return_value=datetime(2026, 7, 29, 12, 30),
        ):
            route, result_state = nse_trade_graph.node_risk(state)

        self.assertEqual(route, "sentiment")
        self.assertEqual(result_state["max_shares"], 100)
        self.assertEqual(result_state["position_size"], 10_000.0)
        self.assertEqual(result_state["risk_plan"]["stop_price"], 90.0)
        self.assertEqual(result_state["risk_plan"]["max_loss_at_stop"], 1_000.0)
        self.assertIn("max loss at stop", result_state["risk_verdict"])

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
                "max_loss_at_stop": 1_000.0,
                "risk_budget": 1_000.0,
            },
        }

        route, result_state = nse_trade_graph.node_propose(state)

        self.assertEqual(route, "log")
        self.assertIn("entry ~₹100.00", result_state["proposal"])
        self.assertIn("stop ₹90.00", result_state["proposal"])
        self.assertIn("max loss ~₹1000", result_state["proposal"])
        self.assertNotIn("risk_pct", result_state["proposal"])


if __name__ == "__main__":
    unittest.main()
