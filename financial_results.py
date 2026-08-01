from __future__ import annotations

import hashlib
import math
import os
import time
from datetime import datetime
from xml.etree import ElementTree

from logging_config import setup_logging
from nse_client import get_request

log = setup_logging("financial_results")

FINANCIAL_RESULTS_PARSER_VERSION = "nse-integrated-xbrl-v3"
FINANCIAL_RESULTS_QUARTER_PERIODS = 4
FINANCIAL_RESULTS_ANNUAL_PERIODS = 2
FINANCIAL_RESULTS_CALL_DELAY_SECONDS = float(
    os.environ.get("FINANCIAL_RESULTS_CALL_DELAY_SECONDS", "1")
)

_MANIFEST_URL = "https://www.nseindia.com/api/integrated-filing-results"
_DATE_FORMAT = "%d-%b-%Y"
_VALID_SCOPES = ("standalone", "consolidated")
_FINANCIAL_HOLDING_SYMBOLS = frozenset({"BAJAJFINSV", "JIOFIN"})
_FINANCIAL_HOLDING_NAME_MARKERS = (
    "financial services",
    "finserv",
    "holding",
)


def get_financial_history(symbol: str, *, company_name: str | None = None) -> dict:
    """Fetch scope-aware NSE integrated-financial XBRL history.

    Only the policy-selected scope is downloaded and parsed. Filing metadata for every
    available scope is retained so the decision is auditable without doubling NSE XBRL
    traffic for each symbol.
    """
    manifest = _get_json(
        _MANIFEST_URL,
        params={
            "index": "equities",
            "symbol": symbol,
            "page": 1,
            "size": 100,
        },
    )
    rows = manifest.get("data", []) if isinstance(manifest, dict) else []
    filings_by_scope = _select_filings(rows)
    if not filings_by_scope:
        return _result(
            "unavailable",
            None,
            None,
            None,
            None,
            None,
            [],
            [],
            {},
        )

    representative = next(iter(filings_by_scope.values()))[0]
    profile, subtype = _profile_for_url(representative.get("xbrl", ""))
    entity_profile = _entity_profile(
        symbol,
        company_name,
        subtype,
        tuple(filings_by_scope),
    )
    selected_scope, scope_reason = _select_scope(
        entity_profile,
        tuple(filings_by_scope),
    )
    selected = filings_by_scope[selected_scope]
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
        sha256 = hashlib.sha256(xml).hexdigest()
        _attach_funding_leverage_provenance(
            period,
            filing,
            sha256=sha256,
        )
        periods.append(period)
        sources.append(
            _source_metadata(
                filing,
                sha256=sha256,
            )
        )

    if not periods:
        status = "unavailable"
    elif len(periods) != len(selected):
        status = "partial"
    else:
        status = "ready"
    scope_histories = {
        scope: {
            "status": status if scope == selected_scope else "metadata_only",
            "profile": _profile_for_url(filings[0].get("xbrl", ""))[0],
            "subtype": _profile_for_url(filings[0].get("xbrl", ""))[1],
            "periods": periods if scope == selected_scope else [],
            "sources": (
                sources
                if scope == selected_scope
                else [_source_metadata(filing) for filing in filings]
            ),
        }
        for scope, filings in filings_by_scope.items()
    }
    return _result(
        status,
        profile,
        subtype,
        entity_profile,
        selected_scope,
        scope_reason,
        periods,
        sources,
        scope_histories,
    )


def _result(
    status,
    profile,
    subtype,
    entity_profile,
    selected_scope,
    scope_reason,
    periods,
    sources,
    scope_histories,
):
    return {
        "status": status,
        "profile": profile,
        "subtype": subtype,
        "entity_profile": entity_profile,
        "selected_scope": selected_scope,
        "scope_selection_reason": scope_reason,
        "available_scopes": list(scope_histories),
        "periods": periods,
        "sources": sources,
        "scope_histories": scope_histories,
        "parser_version": FINANCIAL_RESULTS_PARSER_VERSION,
    }


def _select_filings(rows: object) -> dict[str, list[dict]]:
    if not isinstance(rows, list):
        return {}
    newest_by_scope_period: dict[tuple[str, str], tuple[tuple, dict]] = {}
    for manifest_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if "financial" not in str(row.get("type", "")).lower():
            continue
        scope = str(row.get("consolidated", "")).strip().lower()
        if scope not in _VALID_SCOPES:
            continue
        period_end = _parse_date(row.get("qe_Date"))
        if period_end is None or not row.get("xbrl"):
            continue
        broadcast = _parse_broadcast(row.get("broadcast_Date"))
        period_key = period_end.date().isoformat()
        key = (scope, period_key)
        revision = str(row.get("type_Sub", "")).strip().lower() == "revision"
        rank = (revision, broadcast, -manifest_index)
        current = newest_by_scope_period.get(key)
        if current is None or rank > current[0]:
            newest_by_scope_period[key] = (
                rank,
                {**row, "scope": scope, "period_end": period_key},
            )
    filings_by_scope: dict[str, list[dict]] = {}
    for scope in _VALID_SCOPES:
        newest = sorted(
            (
                item[1]
                for key, item in newest_by_scope_period.items()
                if key[0] == scope
            ),
            key=lambda filing: filing["period_end"],
            reverse=True,
        )
        if newest:
            filings_by_scope[scope] = _select_scope_periods(newest)
    return filings_by_scope


def _select_scope_periods(newest: list[dict]) -> list[dict]:
    quarterly = newest[:FINANCIAL_RESULTS_QUARTER_PERIODS]
    _, subtype = _profile_for_url(newest[0].get("xbrl", ""))
    annual = (
        [
            filing
            for filing in newest
            if filing["period_end"].endswith("-03-31")
        ][:FINANCIAL_RESULTS_ANNUAL_PERIODS]
        if subtype in {"ind_as", "nbfc"}
        else []
    )
    selected = {filing["period_end"]: filing for filing in (*quarterly, *annual)}
    return sorted(selected.values(), key=lambda filing: filing["period_end"], reverse=True)


def _entity_profile(
    symbol: str,
    company_name: str | None,
    subtype: str,
    available_scopes: tuple[str, ...],
) -> str:
    if subtype == "bank":
        return "regulated_bank"
    if subtype == "nbfc":
        normalized_name = str(company_name or "").casefold()
        if (
            symbol.upper() in _FINANCIAL_HOLDING_SYMBOLS
            or any(
                marker in normalized_name
                for marker in _FINANCIAL_HOLDING_NAME_MARKERS
            )
        ):
            return "financial_holding_group"
        return "operating_nbfc"
    if "consolidated" in available_scopes:
        return "non_financial_group"
    return "non_financial_standalone"


def _select_scope(
    entity_profile: str,
    available_scopes: tuple[str, ...],
) -> tuple[str, str]:
    preferred_scope, preferred_reason = {
        "regulated_bank": ("standalone", "regulated_entity_metrics"),
        "operating_nbfc": ("standalone", "regulated_entity_metrics"),
        "financial_holding_group": ("consolidated", "group_economics"),
        "non_financial_group": ("consolidated", "group_economics"),
        "non_financial_standalone": ("standalone", "only_reported_scope"),
    }[entity_profile]
    if preferred_scope in available_scopes:
        return preferred_scope, preferred_reason
    fallback_scope = next(
        scope for scope in _VALID_SCOPES if scope in available_scopes
    )
    return fallback_scope, f"{preferred_scope}_unavailable_fallback"


def _source_metadata(filing: dict, *, sha256: str | None = None) -> dict:
    period_end = filing["period_end"]
    metadata = {
        "scope": filing["scope"],
        "period_end": period_end,
        "period_type": (
            "year_end" if period_end.endswith("-03-31") else "interim"
        ),
        "audit_status": filing.get("audited"),
        "filing_type": filing.get("type"),
        "revision_type": filing.get("type_Sub"),
        "broadcast_at": filing.get("broadcast_Date"),
        "url": filing.get("xbrl"),
    }
    if sha256 is not None:
        metadata["sha256"] = sha256
    return metadata


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
        period = {
            "period_end": period_end,
            "revenue": _fact(root, "RevenueFromOperations", "OneD"),
            "operating_profit": _operating_profit(income, expenses, finance_cost),
            "finance_cost": finance_cost,
            "impairment": _fact(root, "ImpairmentOnFinancialInstruments", "OneD"),
            "pat": _fact(root, "ProfitLossForPeriod", "OneD"),
        }
        if period_end.endswith("-03-31"):
            period["funding_leverage"] = _nbfc_funding_leverage(root)
        return period

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


def _nbfc_funding_leverage(root) -> dict:
    assets = _fact(root, "Assets", "OneI")
    liabilities = _fact(root, "Liabilities", "OneI")
    financial_liabilities = _fact(root, "FinancialLiabilities", "OneI")
    equity = _fact(root, "Equity", "OneI")
    components = {
        "debt_securities": _fact(root, "DebtSecurities", "OneI"),
        "borrowings": _fact(root, "Borrowings", "OneI"),
        "deposits": _fact(root, "Deposits", "OneI"),
        "subordinated_liabilities": _fact(
            root,
            "SubordinatedLiabilities",
            "OneI",
        ),
    }
    non_funding_components = {
        "derivative_financial_liabilities": _fact(
            root,
            "DerivativeFinancialInstrumentsFinancialLiabilities",
            "OneI",
        ),
        "payables": _fact(root, "Payables", "OneI"),
        "trade_payables_msme": _fact(
            root,
            "TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterprise",
            "OneI",
        ),
        "trade_payables_other": _fact(
            root,
            "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterprise",
            "OneI",
        ),
        "other_payables_msme": _fact(
            root,
            "TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterpriseOtherPayables",
            "OneI",
        ),
        "other_payables_other": _fact(
            root,
            "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterpriseOtherPayables",
            "OneI",
        ),
        "other_financial_liabilities": _fact(
            root,
            "OtherFinancialLiabilities",
            "OneI",
        ),
    }
    reported_ratio = _first_fact(
        root,
        "DebtEquityRatio",
        "OneD",
        "FourD",
    )
    balance_sheet_reconciled, balance_delta = _balance_sheet_reconciliation(
        assets,
        liabilities,
        equity,
    )
    present_components = [
        value for value in components.values() if value is not None
    ]
    funding_liabilities = (
        sum(present_components) if present_components else None
    )
    non_funding_liabilities = sum(
        value
        for value in non_funding_components.values()
        if value is not None
    )
    component_delta = (
        financial_liabilities
        - funding_liabilities
        - non_funding_liabilities
        if financial_liabilities is not None
        and funding_liabilities is not None
        else None
    )
    funding_components_reconciled = bool(
        component_delta is not None
        and funding_liabilities >= 0
        and abs(component_delta)
        <= _reconciliation_tolerance(financial_liabilities)
    )

    ratio = None
    method = None
    if (
        balance_sheet_reconciled
        and funding_components_reconciled
        and reported_ratio is not None
        and reported_ratio >= 0
    ):
        ratio = round(reported_ratio, 4)
        method = "reported_debt_to_equity"
    elif balance_sheet_reconciled and funding_components_reconciled:
        ratio = _divide(funding_liabilities, equity)
        if ratio is not None:
            method = "derived_funding_liabilities_to_equity"

    return {
        "ratio": ratio,
        "method": method,
        "reported_debt_to_equity": reported_ratio,
        "funding_liabilities": funding_liabilities,
        "equity": equity,
        "components": components,
        "non_funding_components": non_funding_components,
        "non_funding_liabilities": non_funding_liabilities,
        "assets": assets,
        "liabilities": liabilities,
        "financial_liabilities": financial_liabilities,
        "balance_sheet_reconciled": balance_sheet_reconciled,
        "balance_sheet_reconciliation_delta": balance_delta,
        "funding_components_reconciled": funding_components_reconciled,
        "financial_liabilities_reconciliation_delta": component_delta,
    }


def _attach_funding_leverage_provenance(
    period: dict,
    filing: dict,
    *,
    sha256: str,
) -> None:
    leverage = period.get("funding_leverage")
    if not isinstance(leverage, dict):
        return
    leverage.update(
        {
            "scope": filing["scope"],
            "period_end": filing["period_end"],
            "source_url": filing.get("xbrl"),
            "source_sha256": sha256,
        }
    )


def _first_fact(root, name: str, *contexts: str) -> float | None:
    for context in contexts:
        value = _fact(root, name, context)
        if value is not None:
            return value
    return None


def _balance_sheet_reconciliation(
    assets: float | None,
    liabilities: float | None,
    equity: float | None,
) -> tuple[bool, float | None]:
    if assets is None or liabilities is None or equity is None:
        return False, None
    delta = assets - liabilities - equity
    return abs(delta) <= _reconciliation_tolerance(assets), delta


def _reconciliation_tolerance(reference: float | None) -> float:
    return max(1.0, abs(reference or 0.0) * 0.000001)


def _fact(root, name: str, context: str) -> float | None:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != name:
            continue
        if element.attrib.get("contextRef") == context:
            return _float(element.text)
    return None


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
