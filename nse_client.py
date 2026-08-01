from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import quote

import pandas as pd
import requests

import cache

log = logging.getLogger("nse")

_IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60
_TOKEN_TTL_SECONDS = 30 * 24 * 3600
_SEARCH_TOKEN_URL = "https://charting.nseindia.com/v1/exchanges/symbolsDynamic"
_CHART_URL = "https://charting.nseindia.com/v1/charts/symbolHistoricalData"
_QUOTE_URL = (
    "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
    "?functionName=getSymbolData&marketType=N&series=EQ&symbol={}"
)
_INDEX_URL = (
    "https://www.nseindia.com/api/NextApi/apiClient/indexTrackerApi"
    "?functionName=getConstituents&index={}&noofrecords=0"
)
_LANDING_URL = (
    "https://www.nseindia.com/get-quote/equity/RELIANCE/"
    "Reliance-Industries-Limited"
)
_HISTORIC_BHAVCOPY_URL = (
    "https://www.nseindia.com/api/reports?"
    "archives=%5B%7B%22name%22%3A%22Full%20Bhavcopy%20and%20Security"
    "%20Deliverable%20data%22%2C%22type%22%3A%22daily-reports%22%2C"
    "%22category%22%3A%22capital-market%22%2C%22section%22%3A"
    "%22equities%22%7D%5D&date={}&type=equities&mode=single"
)
_LATEST_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{}.csv"
)
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/",
}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    token: str
    symbol_type: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "token": self.token,
            "symbol_type": self.symbol_type,
        }


class NseClient:
    """Project-owned NSE transport and response normalization module."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        instrument_cache: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 15,
        max_attempts: int = 3,
    ):
        self._session = session or requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)
        self._instrument_cache = instrument_cache or cache.cached
        self._sleep = sleep
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts

    def request(
        self,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> requests.Response | None:
        last_response = None
        refreshed = False
        for attempt in range(self._max_attempts):
            try:
                response = self._session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout,
                )
                last_response = response
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt + 1 == self._max_attempts:
                    log.warning("NSE request failed: %s", error)
                    return None
                self._sleep(2**attempt)
                continue

            if response.status_code in {401, 403} and not refreshed:
                refreshed = True
                self._bootstrap()
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    self._sleep(2**attempt)
                    continue
            return response
        return last_response

    def resolve_instrument(self, symbol: str) -> Instrument:
        normalized = symbol.strip().upper()
        raw = self._instrument_cache(
            f"nse_instrument_v1_{normalized}",
            _TOKEN_TTL_SECONDS,
            lambda: self._fetch_instrument(normalized).to_dict(),
        )
        return Instrument(
            symbol=str(raw["symbol"]),
            token=str(raw["token"]),
            symbol_type=str(raw["symbol_type"]),
        )

    def history(
        self,
        symbol: str,
        *,
        start_datetime: datetime,
        end_datetime: datetime,
        interval: int | str,
    ) -> pd.DataFrame:
        instrument = self.resolve_instrument(symbol)
        chart_type = "I"
        time_interval = 1
        start_epoch = int(start_datetime.timestamp())
        end_epoch = int(end_datetime.timestamp())
        if interval in ("D", "W", "M"):
            chart_type = str(interval)
        else:
            start_epoch += _IST_OFFSET_SECONDS
            end_epoch += _IST_OFFSET_SECONDS
            time_interval = int(interval)

        response = self.request(
            _CHART_URL,
            headers=_DEFAULT_HEADERS,
            params={
                "chartType": chart_type,
                "fromDate": start_epoch,
                "symbol": instrument.symbol,
                "symbolType": instrument.symbol_type,
                "timeInterval": time_interval,
                "toDate": end_epoch,
                "token": instrument.token,
            },
        )
        if response is None or response.status_code >= 400:
            status = response.status_code if response is not None else "unavailable"
            raise ConnectionError(
                f"NSE chart request failed for {symbol} {interval}: HTTP {status}"
            )
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not rows:
            raise ValueError(f"NSE returned no chart data for {symbol} {interval}")
        return _normalize_history(rows, interval)

    def quote(self, symbol: str) -> dict | None:
        response = self.request(_QUOTE_URL.format(quote(symbol, safe="")))
        if response is None or response.status_code >= 400:
            return None
        return _normalize_quote(response.json())

    def index_snapshot(self, index: str) -> pd.DataFrame:
        response = self.request(_INDEX_URL.format(quote(index, safe="")))
        if response is None or response.status_code >= 400:
            raise ConnectionError(f"NSE index request failed for {index}")
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"NSE returned an invalid index response for {index}")
        frame = pd.DataFrame(rows)
        expected = [
            "change",
            "cmSymbol",
            "lasttradedPrice",
            "pchange",
            "totaltradedquantity",
            "totaltradedvalue",
            "weightage",
        ]
        if list(frame.columns) != expected:
            frame = frame.reindex(columns=expected)
        frame.columns = [
            "change",
            "symbol",
            "ltp",
            "changepct",
            "volume",
            "value",
            "weightage",
        ]
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce") * 100_000
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce") * 10_000_000
        frame["previous_close"] = frame["ltp"] - frame["change"]
        return frame[
            [
                "symbol",
                "ltp",
                "previous_close",
                "change",
                "changepct",
                "weightage",
                "volume",
                "value",
            ]
        ]

    def bhavcopy(
        self,
        *,
        series: str | None = None,
        trade_date: date | None = None,
    ) -> pd.DataFrame | None:
        if trade_date is None:
            url = _LATEST_BHAVCOPY_URL.format(date.today().strftime("%d%m%Y"))
        else:
            url = _HISTORIC_BHAVCOPY_URL.format(
                trade_date.strftime("%d-%b-%Y")
            )
        response = self.request(url)
        if response is None or response.status_code >= 400:
            return None
        frame = pd.read_csv(io.StringIO(response.text))
        frame.columns = frame.columns.str.strip()
        frame.rename(
            columns={
                "DATE1": "date",
                "SYMBOL": "symbol",
                "SERIES": "series",
                "PREV_CLOSE": "previous_close",
                "OPEN_PRICE": "open",
                "HIGH_PRICE": "high",
                "LOW_PRICE": "low",
                "CLOSE_PRICE": "close",
                "AVG_PRICE": "vwap",
                "TTL_TRD_QNTY": "volume",
                "TURNOVER_LACS": "turnover",
                "DELIV_QTY": "delivery_volume",
                "DELIV_PER": "delivery_pct",
            },
            inplace=True,
        )
        columns = [
            "date",
            "symbol",
            "series",
            "previous_close",
            "open",
            "high",
            "low",
            "close",
            "vwap",
            "volume",
            "turnover",
            "delivery_volume",
            "delivery_pct",
        ]
        frame = frame[columns].copy()
        if series:
            frame = frame[frame["series"].str.strip() == series].reset_index(
                drop=True
            )
        frame[["delivery_volume", "delivery_pct"]] = frame[
            ["delivery_volume", "delivery_pct"]
        ].apply(pd.to_numeric, errors="coerce")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["turnover"] = (
            pd.to_numeric(frame["turnover"], errors="coerce") * 100_000
        )
        return frame

    def _fetch_instrument(self, symbol: str) -> Instrument:
        response = self.request(
            _SEARCH_TOKEN_URL,
            params={"segment": "", "symbol": symbol},
        )
        if response is None or response.status_code >= 400:
            raise ConnectionError(f"NSE token lookup failed for {symbol}")
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"NSE returned an invalid token response for {symbol}")

        def base_symbol(row: dict) -> str:
            value = str(row.get("symbol") or "").upper()
            return value[:-3] if value.endswith("-EQ") else value

        exact = [row for row in rows if base_symbol(row) == symbol]
        equity = [
            row
            for row in exact
            if str(row.get("type") or "").lower() == "equity"
        ]
        candidates = equity or exact
        if not candidates:
            raise ValueError(f"NSE chart token not found for {symbol}")
        selected = candidates[0]
        return Instrument(
            symbol=base_symbol(selected),
            token=str(selected["scripcode"]),
            symbol_type=str(selected["type"]),
        )

    def _bootstrap(self) -> None:
        try:
            self._session.get(
                _LANDING_URL,
                headers={**_DEFAULT_HEADERS, "Accept": "text/html,*/*"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            log.debug("NSE session bootstrap failed", exc_info=True)


def _normalize_history(rows: list[dict], interval: int | str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)[
        ["time", "open", "high", "low", "close", "volume"]
    ].copy()
    frame.rename(columns={"time": "datetime"}, inplace=True)
    frame["datetime"] = pd.to_datetime(frame["datetime"], unit="ms")
    if interval in ("D", "W", "M"):
        return frame.drop_duplicates(subset=["datetime"]).reset_index(drop=True)

    market_open = pd.Timestamp("09:15:00").time()
    market_close = pd.Timestamp("15:30:00").time()
    frame = frame[
        (frame["datetime"].dt.time >= market_open)
        & (frame["datetime"].dt.time < market_close)
    ].copy()
    minutes = int(interval) if str(interval) == "1" else 5
    frame["datetime"] = frame["datetime"] - pd.to_timedelta(
        (minutes - 1) * 60 + 59,
        unit="s",
    )
    frame["datetime"] = frame["datetime"].apply(
        lambda value: (
            value.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if value.second > 1
            else value.replace(second=0, microsecond=0)
        )
    )
    return frame.drop_duplicates(subset=["datetime"]).reset_index(drop=True)


def _normalize_quote(payload: dict) -> dict:
    responses = payload.get("equityResponse") if isinstance(payload, dict) else None
    if not responses:
        return payload
    raw = responses[0]
    metadata = raw.get("metaData") or {}
    security = raw.get("secInfo") or {}
    price_info = raw.get("priceInfo") or {}
    result = {
        "symbol": metadata.get("symbol"),
        "name": metadata.get("companyName"),
        "series": metadata.get("series"),
        "open": metadata.get("open"),
        "high": metadata.get("dayHigh"),
        "low": metadata.get("dayLow"),
        "close": metadata.get("closePrice"),
        "previous_close": metadata.get("previousClose"),
        "change": metadata.get("change"),
        "changepct": metadata.get("pChange"),
        "sector": security.get("sector"),
        "industry": security.get("industryInfo"),
    }
    listing_date = security.get("listingDate")
    if listing_date:
        try:
            result["date_of_listing"] = datetime.strptime(
                listing_date, "%d-%b-%Y %H:%M:%S"
            ).date()
        except ValueError:
            pass
    last_updated = raw.get("lastUpdateTime")
    if last_updated:
        try:
            result["last_updated"] = datetime.strptime(
                last_updated, "%d-%b-%Y %H:%M:%S"
            )
        except ValueError:
            pass
    band = price_info.get("priceBand")
    if band:
        parts = str(band).split("-", 1)
        if len(parts) == 2:
            result["lower_circuit"] = float(parts[0])
            result["upper_circuit"] = float(parts[1])
    return result


_client = NseClient()


def get_request(url: str, headers: dict = None, params: dict = None):
    return _client.request(url, headers=headers, params=params)


def get_stock_historical_data(
    stock_symbol: str,
    start_datetime: datetime,
    end_datetime: datetime,
    interval: int | str = 1,
) -> pd.DataFrame:
    return _client.history(
        stock_symbol,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        interval=interval,
    )


def get_stock_live_quotes(stock_symbol: str) -> dict | None:
    return _client.quote(stock_symbol)


def get_index_constituents_live_snapshot(index: str = "NIFTY 50") -> pd.DataFrame:
    return _client.index_snapshot(index)


def get_daily_bhavcopy_and_deliverables_data(
    series: str | None = None,
    trade_date: date | None = None,
) -> pd.DataFrame | None:
    return _client.bhavcopy(series=series, trade_date=trade_date)
