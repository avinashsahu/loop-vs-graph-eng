import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import warm_shareholding
from shareholding import NseShareholdingRequestError


class WarmShareholdingTests(unittest.TestCase):
    @patch.object(warm_shareholding, "record_shareholding_universe_attempt")
    @patch.object(warm_shareholding, "warm_shareholding_history")
    @patch.object(warm_shareholding, "due_shareholding_universe_symbols")
    @patch.object(warm_shareholding, "seed_shareholding_universe")
    @patch.object(warm_shareholding, "get_index_symbols")
    @patch.object(warm_shareholding, "is_market_hours", return_value=False)
    @patch.object(warm_shareholding, "_arguments")
    def test_universe_warm_continues_after_one_symbol_request_failure(
        self,
        arguments,
        _market_hours,
        get_index_symbols,
        seed_universe,
        due_symbols,
        warm_history,
        record_attempt,
    ):
        arguments.return_value = SimpleNamespace(
            symbols=[],
            index_names=[],
            universe_index_names=["NIFTY TOTAL MKT"],
            queued=False,
            periods=5,
            limit=2,
            allow_market_hours=False,
            retry_incomplete_now=True,
        )
        get_index_symbols.return_value = ["BAD", "GOOD"]
        seed_universe.return_value = 2
        due_symbols.return_value = ["BAD", "GOOD"]
        warm_history.side_effect = [
            NseShareholdingRequestError(
                "NSE shareholding request failed repeatedly"
            ),
            SimpleNamespace(
                periods=(object(),),
                status="ready",
                complete=True,
            ),
        ]

        warm_shareholding.main()

        self.assertEqual(
            warm_history.call_args_list,
            [call("BAD", 5), call("GOOD", 5)],
        )
        due_symbols.assert_called_once_with(
            "NIFTY TOTAL MKT",
            limit=2,
            refresh_after_days=30,
            incomplete_retry_days=0,
        )
        self.assertEqual(
            record_attempt.call_args_list,
            [
                call(
                    "NIFTY TOTAL MKT",
                    "BAD",
                    complete=False,
                    periods_available=0,
                    reason_code="XBRL_DOWNLOAD_FAILED",
                    error_detail=(
                        "NSE shareholding request failed repeatedly"
                    ),
                ),
                call(
                    "NIFTY TOTAL MKT",
                    "GOOD",
                    complete=True,
                    periods_available=1,
                    reason_code=None,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
