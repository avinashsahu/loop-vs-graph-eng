import os
from datetime import datetime

from nsemine.bin.scraper import get_request

import cache
from logging_config import setup_logging

log = setup_logging("fundamentals")

FUNDAMENTALS_CACHE_TTL_HOURS = float(os.environ.get("FUNDAMENTALS_CACHE_TTL_HOURS", "24"))
_NEXT_API_BASE = "https://www.nseindia.com/api/NextApi/apiClient"

# Endpoint (function-name) identifiers under _NEXT_API_BASE. getPeerComparisonQuaters,
# getPeerComparisonData and getYearwiseData were confirmed against a live response; the
# remaining three are best-guess placeholders (same naming convention, not individually
# re-confirmed in this session) -- fix these against a real response before trusting the
# announcements/actions/shareholding fields.
_FN_CORP_ANNOUNCEMENTS = "GetCorpAnnouncementsApi"
_FN_CORP_ACTIONS = "GetCorpActionsApi"
_FN_SHAREHOLDING_PATTERN = "GetShareholdingPatternApi"
_FN_YEARWISE_DATA = "getYearwiseData"
_FN_PEER_COMPARISON_QUARTERS = "getPeerComparisonQuaters"
_FN_PEER_COMPARISON_DATA = "getPeerComparisonData"


def _next_api_get(function_name, params):
    resp = get_request(f"{_NEXT_API_BASE}/{function_name}", params=params)
    if resp is None:
        return None
    return resp.json()


def _get_corp_announcements(symbol):
    data = _next_api_get(_FN_CORP_ANNOUNCEMENTS, {"symbol": symbol})
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return items[:3]


def _get_corp_actions(symbol):
    data = _next_api_get(_FN_CORP_ACTIONS, {"symbol": symbol})
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return items[:3]


def _get_shareholding_pattern(symbol):
    data = _next_api_get(_FN_SHAREHOLDING_PATTERN, {"symbol": symbol})
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return items[:5]


def _get_yearwise_returns(symbol):
    # NSE API quirk: this endpoint (only this one) expects the symbol suffixed with "EQN".
    return _next_api_get(_FN_YEARWISE_DATA, {"symbol": f"{symbol}EQN"})


def _get_peer_comparison(symbol):
    quarters_resp = _next_api_get(_FN_PEER_COMPARISON_QUARTERS, {"symbol": symbol})
    quarters = quarters_resp if isinstance(quarters_resp, list) else (quarters_resp or {}).get("data", [])
    if not quarters:
        return None, None
    latest_quarter = quarters[0]
    peer_data = _next_api_get(_FN_PEER_COMPARISON_DATA, {"symbol": symbol, "quarter": latest_quarter})
    return latest_quarter, peer_data


def _extract_eps_pat(symbol, peer_data):
    """Best-effort EPS/PAT lookup for `symbol`'s own row in the peer-comparison table.

    Field names weren't confirmed against a live response in this session -- verify these
    key names locally and extend the fallback lists below if they don't match.
    """
    rows = peer_data if isinstance(peer_data, list) else (peer_data or {}).get("data", [])
    row = next((r for r in rows if r.get("symbol") == symbol), None)
    if row is None:
        return None, None
    eps = next((row[k] for k in ("eps", "EPS", "basicEPS") if k in row), None)
    pat = next((row[k] for k in ("pat", "PAT", "netProfit") if k in row), None)
    return eps, pat


def get_fundamental_snapshot(symbol: str) -> dict:
    def fetch_fn():
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

        for field, fetch in (
            ("corp_announcements", lambda: _get_corp_announcements(symbol)),
            ("corp_actions", lambda: _get_corp_actions(symbol)),
            ("shareholding_pattern", lambda: _get_shareholding_pattern(symbol)),
            ("yearwise_returns", lambda: _get_yearwise_returns(symbol)),
        ):
            try:
                snapshot[field] = fetch()
            except Exception:
                log.warning("fundamentals[%s]: %s fetch failed", symbol, field, exc_info=True)

        try:
            quarter, peer_data = _get_peer_comparison(symbol)
            snapshot["peer_comparison_quarter"] = quarter
            snapshot["peer_comparison"] = peer_data
            snapshot["eps"], snapshot["pat"] = _extract_eps_pat(symbol, peer_data)
        except Exception:
            log.warning("fundamentals[%s]: peer comparison fetch failed", symbol, exc_info=True)

        return snapshot

    key = f"fundamentals_{symbol}_{datetime.now():%Y%m%d}"
    return cache.cached(key, FUNDAMENTALS_CACHE_TTL_HOURS * 3600, fetch_fn)
