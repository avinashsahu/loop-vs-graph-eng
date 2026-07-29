import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
