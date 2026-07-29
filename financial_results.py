from __future__ import annotations

import hashlib
import math
import os
import time
from datetime import datetime
from xml.etree import ElementTree

from nsemine.bin.scraper import get_request

from logging_config import setup_logging

log = setup_logging("financial_results")

FINANCIAL_RESULTS_PARSER_VERSION = "nse-integrated-xbrl-v1"
FINANCIAL_RESULTS_MAX_FILINGS = 6
FINANCIAL_RESULTS_CALL_DELAY_SECONDS = float(
    os.environ.get("FINANCIAL_RESULTS_CALL_DELAY_SECONDS", "1")
)

_MANIFEST_URL = "https://www.nseindia.com/api/integrated-filing-results"
_DATE_FORMAT = "%d-%b-%Y"


def get_financial_history(symbol: str) -> dict:
    """Fetch and normalize recent standalone NSE integrated-financial XBRL filings."""
    manifest = _get_json(
        _MANIFEST_URL,
        params={"index": "equities", "symbol": symbol},
    )
    rows = manifest.get("data", []) if isinstance(manifest, dict) else []
    selected = _select_filings(rows)
    if not selected:
        return _result("unavailable", None, None, [], [])

    profile, subtype = _profile_for_url(selected[0].get("xbrl", ""))
    periods = []
    sources = []
    for filing in selected:
        url = filing.get("xbrl")
        if not url:
            continue
        try:
            xml = _get_bytes(url)
            period = _parse_period(xml, filing["period_end"], subtype)
        except Exception:
            log.warning(
                "financial_results[%s]: XBRL parse failed for %s",
                symbol,
                filing.get("qe_Date"),
                exc_info=True,
            )
            continue
        periods.append(period)
        sources.append(
            {
                "period_end": filing["period_end"],
                "filing_type": filing.get("type_Sub"),
                "broadcast_at": filing.get("broadcast_Date"),
                "url": url,
                "sha256": hashlib.sha256(xml).hexdigest(),
            }
        )

    status = "ready" if periods else "unavailable"
    return _result(status, profile, subtype, periods, sources)


def _result(status, profile, subtype, periods, sources):
    return {
        "status": status,
        "profile": profile,
        "subtype": subtype,
        "periods": periods,
        "sources": sources,
        "parser_version": FINANCIAL_RESULTS_PARSER_VERSION,
    }


def _select_filings(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    newest_by_period: dict[str, tuple[datetime, dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "financial" not in str(row.get("type", "")).lower():
            continue
        if str(row.get("consolidated", "")).lower() != "standalone":
            continue
        period_end = _parse_date(row.get("qe_Date"))
        if period_end is None or not row.get("xbrl"):
            continue
        broadcast = _parse_broadcast(row.get("broadcast_Date"))
        key = period_end.date().isoformat()
        current = newest_by_period.get(key)
        if current is None or broadcast > current[0]:
            newest_by_period[key] = (
                broadcast,
                {**row, "period_end": key},
            )
    return [
        item[1]
        for _, item in sorted(
            newest_by_period.items(),
            key=lambda pair: pair[0],
            reverse=True,
        )[:FINANCIAL_RESULTS_MAX_FILINGS]
    ]


def _parse_period(xml: bytes, period_end: str, subtype: str) -> dict:
    root = ElementTree.fromstring(xml)
    if subtype == "bank":
        earned = _fact(root, "InterestEarned", "OneD")
        expended = _fact(root, "InterestExpended", "OneD")
        return {
            "period_end": period_end,
            "net_interest_income": _subtract(earned, expended),
            "operating_profit": _fact(
                root, "OperatingProfitBeforeProvisionAndContingencies", "OneD"
            ),
            "provisions": _fact(
                root, "ProvisionsOtherThanTaxAndContingencies", "OneD"
            ),
            "pat": _fact(root, "ProfitLossForThePeriod", "OneD"),
            "gross_npa_pct": _percent(root, "PercentageOfGrossNpa", "OneD"),
            "net_npa_pct": _percent(root, "PercentageOfNpa", "OneD"),
            "return_on_assets_pct": _percent(root, "ReturnOnAssets", "OneD"),
        }
    if subtype == "nbfc":
        income = _fact(root, "Income", "OneD")
        expenses = _fact(root, "Expenses", "OneD")
        finance_cost = _fact(root, "FinanceCosts", "OneD")
        return {
            "period_end": period_end,
            "revenue": _fact(root, "RevenueFromOperations", "OneD"),
            "operating_profit": _operating_profit(income, expenses, finance_cost),
            "finance_cost": finance_cost,
            "impairment": _fact(root, "ImpairmentOnFinancialInstruments", "OneD"),
            "pat": _fact(root, "ProfitLossForPeriod", "OneD"),
            "debt_to_equity": _percent(root, "DebtEquityRatio", "OneD"),
        }

    revenue = _fact(root, "RevenueFromOperations", "OneD")
    income = _fact(root, "Income", "OneD")
    expenses = _fact(root, "Expenses", "OneD")
    finance_cost = _fact(root, "FinanceCosts", "OneD")
    operating_profit = _operating_profit(income, expenses, finance_cost)
    period = {
        "period_end": period_end,
        "revenue": revenue,
        "operating_profit": operating_profit,
        "pat": _fact(root, "ProfitLossForPeriod", "OneD"),
        "operating_margin_pct": _divide_pct(operating_profit, revenue),
    }
    if period_end.endswith("-03-31"):
        equity = _fact(root, "Equity", "OneI")
        annual_pat = _fact(root, "ProfitLossForPeriod", "FourD")
        debt = _sum_optional(
            _fact(root, "BorrowingsCurrent", "OneI"),
            _fact(root, "BorrowingsNoncurrent", "OneI"),
        )
        period.update(
            {
                "assets": _fact(root, "Assets", "OneI"),
                "equity": equity,
                "total_debt": debt,
                "debt_to_equity": _divide(debt, equity),
                "operating_cash_flow": _fact(
                    root, "CashFlowsFromUsedInOperatingActivities", "FourD"
                ),
                "return_on_equity_pct": _divide_pct(annual_pat, equity),
            }
        )
    return period


def _fact(root, name: str, context: str) -> float | None:
    fallback = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != name:
            continue
        value = _float(element.text)
        if value is None:
            continue
        if element.attrib.get("contextRef") == context:
            return value
        if fallback is None:
            fallback = value
    return fallback


def _percent(root, name: str, context: str) -> float | None:
    value = _fact(root, name, context)
    return round(value * 100, 4) if value is not None else None


def _operating_profit(
    income: float | None,
    expenses: float | None,
    finance_cost: float | None,
) -> float | None:
    if income is None or expenses is None or finance_cost is None:
        return None
    return income - expenses + finance_cost


def _subtract(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _sum_optional(*values: float | None) -> float | None:
    return sum(values) if all(value is not None for value in values) else None


def _divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator, 4)


def _divide_pct(numerator: float | None, denominator: float | None) -> float | None:
    ratio = _divide(numerator, denominator)
    return round(ratio * 100, 2) if ratio is not None else None


def _profile_for_url(url: str) -> tuple[str, str]:
    upper = url.upper()
    if "BANKING" in upper:
        return "banking_nbfc", "bank"
    if "NBFC" in upper:
        return "banking_nbfc", "nbfc"
    return "non_financial", "ind_as"


def _parse_date(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), _DATE_FORMAT)
    except (TypeError, ValueError):
        return None


def _parse_broadcast(value: object) -> datetime:
    try:
        return datetime.strptime(str(value), "%d-%b-%Y %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.min


def _float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _get_json(url: str, *, params: dict) -> dict:
    response = _paced_get(url, params=params)
    return response.json()


def _get_bytes(url: str) -> bytes:
    return _paced_get(url).content


def _paced_get(url: str, *, params: dict | None = None):
    response = get_request(url, params=params)
    time.sleep(FINANCIAL_RESULTS_CALL_DELAY_SECONDS)
    if response is None:
        raise ConnectionError(f"NSE request failed for {url}")
    response.raise_for_status()
    return response
