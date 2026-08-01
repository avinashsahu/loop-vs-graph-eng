import unittest
from datetime import datetime

from nse_client import NseClient


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, symbol="ACE"):
        self.headers = {}
        self.calls = []
        self.symbol = symbol

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("symbolsDynamic"):
            return _Response(
                {
                    "data": [
                        {
                            "symbol": f"{self.symbol}-EQ",
                            "scripcode": "12345",
                            "type": "Equity",
                        }
                    ]
                }
            )
        if url.endswith("symbolHistoricalData"):
            return _Response(
                {
                    "data": [
                        {
                            "time": 1_784_800_000_000,
                            "open": 100.0,
                            "high": 102.0,
                            "low": 99.0,
                            "close": 101.0,
                            "volume": 10_000,
                        }
                    ]
                }
            )
        if "functionName=getSymbolData" in url:
            return _Response(
                {
                    "equityResponse": [
                        {
                            "metaData": {
                                "symbol": "ACE",
                                "companyName": "Action Construction Equipment",
                                "previousClose": 100.0,
                                "change": 1.0,
                            },
                            "secInfo": {"sector": "Capital Goods"},
                            "priceInfo": {"priceBand": "80.0-120.0"},
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected URL: {url}")


class NseClientTests(unittest.TestCase):
    def test_four_timeframes_resolve_the_instrument_only_once(self):
        session = _Session()
        stored = {}

        def instrument_cache(key, _ttl, fetch):
            if key not in stored:
                stored[key] = fetch()
            return stored[key]

        client = NseClient(
            session=session,
            instrument_cache=instrument_cache,
            sleep=lambda _seconds: None,
        )
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 30)

        for interval in ("D", 30, 15, 5):
            client.history(
                "ACE",
                start_datetime=start,
                end_datetime=end,
                interval=interval,
            )

        token_calls = [
            call for call in session.calls if call[0].endswith("symbolsDynamic")
        ]
        chart_calls = [
            call
            for call in session.calls
            if call[0].endswith("symbolHistoricalData")
        ]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(chart_calls), 4)
        self.assertTrue(
            all(call[1]["params"]["token"] == "12345" for call in chart_calls)
        )

    def test_quote_uses_lower_then_upper_price_band_order(self):
        client = NseClient(session=_Session(), sleep=lambda _seconds: None)

        quote = client.quote("ACE")

        self.assertEqual(quote["lower_circuit"], 80.0)
        self.assertEqual(quote["upper_circuit"], 120.0)

    def test_hyphenated_symbol_keeps_its_full_name(self):
        client = NseClient(
            session=_Session("BAJAJ-AUTO"),
            instrument_cache=lambda _key, _ttl, fetch: fetch(),
            sleep=lambda _seconds: None,
        )

        instrument = client.resolve_instrument("BAJAJ-AUTO")

        self.assertEqual(instrument.symbol, "BAJAJ-AUTO")


if __name__ == "__main__":
    unittest.main()
