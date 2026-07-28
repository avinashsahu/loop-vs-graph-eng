import os
import time
from datetime import datetime

from nsemine.bin.scraper import get_request

import cache
from logging_config import setup_logging

log = setup_logging("fundamentals")

FUNDAMENTALS_CACHE_TTL_HOURS = float(os.environ.get("FUNDAMENTALS_CACHE_TTL_HOURS", "24"))
FUNDAMENTALS_CALL_DELAY_SECONDS = float(os.environ.get("FUNDAMENTALS_CALL_DELAY_SECONDS", "0.5"))

# All 8 endpoints share this one path -- functionName is a query param, not a path segment.
# Confirmed live against HDFCBANK for all of: getSymbolName, getCorporateAnnouncement,
# getCorpAction, getShareholdingPattern, getYearwiseData, getPeerComparisonQuaters,
# getPeerComparisonData.
_NEXT_API_URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"


def _next_api_get(function_name, params):
    resp = get_request(_NEXT_API_URL, params={"functionName": function_name, **params})
    # Stagger every call through this one choke point (all 7 fundamentals endpoints go
    # through here, including the two sequential calls inside _get_peer_comparison) --
    # get_fundamental_snapshot fires ~7 requests back to back for one symbol, on top of
    # NSE_SCAN_DELAY_SECONDS between symbols in a batch scan.
    time.sleep(FUNDAMENTALS_CALL_DELAY_SECONDS)
    if resp is None:
        # get_request already retried internally and gave up -- treat as a real failure,
        # not "no data", so the caller doesn't cache a snapshot degraded by this field.
        raise ConnectionError(f"NSE request failed for functionName={function_name}")
    return resp.json()


def _get_symbol_name(symbol):
    data = _next_api_get("getSymbolName", {"symbol": symbol})
    return (data or {}).get("companyName")


def _get_corp_announcements(symbol):
    data = _next_api_get(
        "getCorporateAnnouncement",
        {"symbol": symbol, "marketApiType": "equities", "noOfRecords": 3},
    )
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return items[:3]


def _get_corp_actions(symbol):
    data = _next_api_get(
        "getCorpAction",
        {"symbol": symbol, "marketApiType": "equities", "noOfRecords": 3},
    )
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return items[:3]


def _get_shareholding_pattern(symbol):
    # Shape confirmed live: a dict keyed by period date (not a list), e.g.
    # {"30-Jun-2026": {"public": {...}, "Total": "100.00", ...}, "31-Mar-2026": {...}, ...}
    data = _next_api_get("getShareholdingPattern", {"symbol": symbol, "noOfRecords": 5})
    if not isinstance(data, dict):
        return []
    return [{"period": period, **fields} for period, fields in list(data.items())[:5]]


def _get_yearwise_returns(symbol):
    # NSE API quirk: this endpoint (only this one) expects the symbol suffixed with "EQN".
    data = _next_api_get("getYearwiseData", {"symbol": f"{symbol}EQN"})
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    if not items:
        return None
    row = dict(items[0])
    # NSE mislabels these fields -- despite the name, they're year-to-date change, not a
    # single day's change. Confirmed live: HDFCBANK showed index_yesterday_chng_per=-8.27
    # and ACE showed index_yesterday_chng_per=-3.32 on the same real trading day for
    # overlapping indices -- a single-day NIFTY move of that size never happened; both
    # values are plausible as year-to-date instead.
    if "yesterday_chng_per" in row:
        row["ytd_chng_per"] = row.pop("yesterday_chng_per")
    if "index_yesterday_chng_per" in row:
        row["index_ytd_chng_per"] = row.pop("index_yesterday_chng_per")
    return row


def _get_peer_comparison(symbol):
    quarters_resp = _next_api_get("getPeerComparisonQuaters", {"symbol": symbol})
    quarters = quarters_resp if isinstance(quarters_resp, list) else (quarters_resp or {}).get("data", [])
    if not quarters:
        return None, None
    latest_quarter = quarters[0]["value"]
    peer_data = _next_api_get(
        "getPeerComparisonData",
        {"symbol": symbol, "type": "S", "quarter": latest_quarter, "param": "industry", "index": ""},
    )
    return latest_quarter, peer_data


def _extract_eps_pat(symbol, peer_data):
    """EPS/PAT lookup for `symbol`'s own row in the peer-comparison table.

    Confirmed live: peer_data is a flat list of dicts, each with plain "eps"/"pat" keys.
    """
    rows = peer_data if isinstance(peer_data, list) else (peer_data or {}).get("data", [])
    row = next((r for r in rows if r.get("symbol") == symbol), None)
    if row is None:
        return None, None
    return row.get("eps"), row.get("pat")


def get_fundamental_snapshot(symbol: str) -> dict:
    key = f"fundamentals_{symbol}_{datetime.now():%Y%m%d}"
    ttl_seconds = FUNDAMENTALS_CACHE_TTL_HOURS * 3600

    hit = cache.read(key, ttl_seconds)
    if hit is not None:
        return hit

    snapshot = {
        "company_name": None,
        "corp_announcements": None,
        "corp_actions": None,
        "shareholding_pattern": None,
        "yearwise_returns": None,
        "peer_comparison_quarter": None,
        "peer_comparison": None,
        "eps": None,
        "pat": None,
    }
    all_ok = True

    for field, fetch in (
        ("company_name", lambda: _get_symbol_name(symbol)),
        ("corp_announcements", lambda: _get_corp_announcements(symbol)),
        ("corp_actions", lambda: _get_corp_actions(symbol)),
        ("shareholding_pattern", lambda: _get_shareholding_pattern(symbol)),
        ("yearwise_returns", lambda: _get_yearwise_returns(symbol)),
    ):
        try:
            snapshot[field] = fetch()
        except Exception:
            log.warning("fundamentals[%s]: %s fetch failed", symbol, field, exc_info=True)
            all_ok = False

    try:
        quarter, peer_data = _get_peer_comparison(symbol)
        snapshot["peer_comparison_quarter"] = quarter
        snapshot["peer_comparison"] = peer_data
        snapshot["eps"], snapshot["pat"] = _extract_eps_pat(symbol, peer_data)
    except Exception:
        log.warning("fundamentals[%s]: peer comparison fetch failed", symbol, exc_info=True)
        all_ok = False

    # Only cache a fully-successful fetch -- a snapshot degraded by a transient NSE
    # failure shouldn't get stuck for FUNDAMENTALS_CACHE_TTL_HOURS; retry it next run instead.
    if all_ok:
        cache.write(key, snapshot)
    return snapshot
