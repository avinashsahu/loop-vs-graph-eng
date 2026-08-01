"""Warm Integrated Filing - Governance XBRL outside live scans."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from typing import Protocol
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import cache
from logging_config import setup_logging
from nse_client import get_request

log = setup_logging("governance_filings")

GOVERNANCE_PARSER_VERSION = "nse-integrated-governance-v1"
GOVERNANCE_SCHEMA_VERSION = "governance_filings_v1"
GOVERNANCE_CACHE_TTL_HOURS = float(
    os.environ.get("GOVERNANCE_CACHE_TTL_HOURS", "168")
)
GOVERNANCE_CALL_DELAY_SECONDS = float(
    os.environ.get("GOVERNANCE_CALL_DELAY_SECONDS", "1")
)
GOVERNANCE_MAX_PERIODS = int(os.environ.get("GOVERNANCE_MAX_PERIODS", "4"))
_MANIFEST_URL = "https://www.nseindia.com/api/integrated-filing-results"
_DATE_FORMAT = "%d-%b-%Y"
_BROADCAST_FORMAT = "%d-%b-%Y %H:%M:%S"
_IST = ZoneInfo("Asia/Kolkata")
_XLINK = "http://www.w3.org/1999/xlink"
_XBRLI = "http://www.xbrl.org/2003/instance"

_COMPLIANCE_TRUE_FACTS = (
    (
        "TheCompositionOfBoardOfDirectorsIsInTermsOfSebiRegulations2015",
        "board_composition_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheCompositionOfAuditCommitteeIsInTermsOfSebiRegulations2015",
        "audit_committee_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheCompositionOfTheNominationAndRemunerationCommitteeIsInTermsOfSebiRegulations2015",
        "nomination_committee_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheCompositionOfTheStakeholdersRelationshipCommitteeCommitteeIsInTermsOfSebiRegulations2015",
        "stakeholders_committee_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheCompositionOfTheRiskManagementCommitteeIsInTermsOfSebiRegulations2015",
        "risk_committee_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheMeetingsOfTheBoardOfDirectorsAndTheAboveCommitteesHaveBeenConductedInTheMannerAsSpecifiedInSebiRegulations2015",
        "board_meeting_conduct_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "TheCommitteeMembersHaveBeenMadeAwareOfTheirPowersRoleAndResponsibilitiesAsSpecifiedInSebiRegulations2015",
        "committee_role_awareness_non_compliance",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
)

_TRUE_ADVERSE_FACTS = (
    (
        "WhetherAsPerSubRegulation2baOfRegulation27OfSEBILODRThereHasBeenCyberSecurityIncidentsDuringTheQuarter",
        "cyber_security_incident",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "WhetherTheDirectorIsDisqualified",
        "director_disqualified",
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
)

# Routine director appointment/retirement fields — never policy exceptions alone.
_INFORMATIONAL_LOCAL_NAMES = frozenset(
    {
        "DateOfAppointmentOfDirector",
        "DateOfReappointmentOfDirector",
        "DateOfAppointmentOfDirectorInCommittee",
        "CurrentStatusDirector",
        "NameOftheDirector",
        "DirectorIdentificationNumberOfDirector",
        "TenureOfDirector",
        "DateOfBirth",
        "PositionOfDirectorInBoardOne",
        "PositionOfDirectorInBoardTwo",
        "PositionOfDirectorInBoardThree",
        "PositionOfDirectorInCommitteeOne",
        "PositionOfDirectorInCommitteeTwo",
    }
)

_live_service = None
_warm_service = None


class GovernanceError(ValueError):
    pass


class GovernanceSource(Protocol):
    def list_filings(self, symbol: str) -> list[dict]: ...

    def download(self, filing: dict) -> bytes: ...


class GovernanceStore(Protocol):
    def read(self, symbol: str, ttl_seconds: float) -> dict | None: ...

    def write(self, symbol: str, payload: dict) -> None: ...


class NseGovernanceSource:
    def __init__(self, *, request_delay_seconds: float | None = None):
        self._delay = (
            GOVERNANCE_CALL_DELAY_SECONDS
            if request_delay_seconds is None
            else request_delay_seconds
        )

    def list_filings(self, symbol: str) -> list[dict]:
        response = get_request(
            _MANIFEST_URL,
            params={
                "index": "equities",
                "symbol": symbol,
                "page": 1,
                "size": 100,
            },
        )
        time.sleep(self._delay)
        rows = response.json().get("data", []) if response is not None else []
        if not isinstance(rows, list):
            raise GovernanceError(
                f"unexpected NSE governance manifest for {symbol}"
            )
        return _select_governance_filings(rows)

    def download(self, filing: dict) -> bytes:
        response = get_request(filing["url"])
        time.sleep(self._delay)
        if response is None or response.status_code >= 400:
            raise GovernanceError(
                f"governance XBRL download failed for {filing.get('record_id')}"
            )
        return response.content


class CacheGovernanceStore:
    @staticmethod
    def _key(symbol: str) -> str:
        return f"governance_v1_{symbol.upper()}"

    def read(self, symbol: str, ttl_seconds: float) -> dict | None:
        return cache.read(self._key(symbol), ttl_seconds)

    def write(self, symbol: str, payload: dict) -> None:
        cache.write(self._key(symbol), payload)


class GovernanceHistoryService:
    def __init__(
        self,
        source: GovernanceSource,
        store: GovernanceStore,
        *,
        download_missing: bool = True,
        max_periods: int = GOVERNANCE_MAX_PERIODS,
    ):
        self._source = source
        self._store = store
        self._download_missing = download_missing
        self._max_periods = max_periods

    def get(self, symbol: str) -> dict:
        symbol = symbol.upper()
        cached = self._store.read(
            symbol,
            GOVERNANCE_CACHE_TTL_HOURS * 3600,
        )
        if cached is not None and not self._download_missing:
            return cached
        if not self._download_missing:
            return _empty_history(symbol, status="pending")
        return self.warm(symbol)

    def warm(self, symbol: str) -> dict:
        symbol = symbol.upper()
        errors: list[str] = []
        try:
            filings = self._source.list_filings(symbol)[: self._max_periods]
        except Exception as error:
            log.warning(
                "governance[%s]: manifest fetch failed",
                symbol,
                exc_info=True,
            )
            payload = _empty_history(
                symbol,
                status="unavailable",
                errors=[f"manifest:{type(error).__name__}"],
            )
            self._store.write(symbol, payload)
            return payload

        periods = []
        for filing in filings:
            try:
                payload_bytes = self._source.download(filing)
                period = _parse_governance_xbrl(
                    record_id=filing["record_id"],
                    period=filing["period"],
                    payload=payload_bytes,
                    source_url=filing["url"],
                    revised=filing.get("revised", False),
                    published_at=filing.get("published_at"),
                )
                periods.append(period)
            except Exception as error:
                errors.append(
                    f"{filing.get('record_id')}:{type(error).__name__}"
                )
                log.warning(
                    "governance[%s]: filing %s failed; continuing",
                    symbol,
                    filing.get("record_id"),
                    exc_info=True,
                )

        status = (
            "ready"
            if periods
            else "unavailable"
            if errors
            else "ready"
        )
        payload = {
            "version": GOVERNANCE_SCHEMA_VERSION,
            "parser_version": GOVERNANCE_PARSER_VERSION,
            "status": status,
            "symbol": symbol,
            "fetched_at": datetime.now(_IST).replace(tzinfo=None).isoformat(),
            "periods": periods,
            "exceptions": [
                exception
                for period in periods
                for exception in period.get("exceptions", [])
            ],
            "errors": errors,
            "periods_available": len(periods),
        }
        self._store.write(symbol, payload)
        return payload


def get_governance_history(symbol: str) -> dict:
    """Cache-only read for live scans."""
    global _live_service
    if _live_service is None:
        _live_service = GovernanceHistoryService(
            NseGovernanceSource(),
            CacheGovernanceStore(),
            download_missing=False,
        )
    return _live_service.get(symbol)


def warm_governance_history(symbol: str) -> dict:
    """Paced off-market warm used by the dedicated CLI/scheduler."""
    global _warm_service
    if _warm_service is None:
        _warm_service = GovernanceHistoryService(
            NseGovernanceSource(),
            CacheGovernanceStore(),
            download_missing=True,
        )
    return _warm_service.warm(symbol)


def _empty_history(
    symbol: str,
    *,
    status: str,
    errors: list[str] | None = None,
) -> dict:
    return {
        "version": GOVERNANCE_SCHEMA_VERSION,
        "parser_version": GOVERNANCE_PARSER_VERSION,
        "status": status,
        "symbol": symbol.upper(),
        "fetched_at": None,
        "periods": [],
        "exceptions": [],
        "errors": list(errors or []),
        "periods_available": 0,
    }


def _select_governance_filings(rows: list) -> list[dict]:
    newest_by_period: dict[str, tuple[tuple, dict]] = {}
    for manifest_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if "governance" not in str(row.get("type", "")).lower():
            continue
        period = _parse_date(row.get("qe_Date"))
        url = row.get("xbrl")
        if period is None or not _is_usable_xbrl_url(url):
            continue
        period_key = period.date().isoformat()
        broadcast = _parse_broadcast(row.get("broadcast_Date"))
        revised = str(row.get("type_Sub", "")).strip().lower() in {
            "revision",
            "revised",
        } or bool(row.get("revised_Date"))
        rank = (revised, broadcast, -manifest_index)
        current = newest_by_period.get(period_key)
        if current is None or rank > current[0]:
            newest_by_period[period_key] = (
                rank,
                {
                    "record_id": str(row.get("seq_Id") or period_key),
                    "period": period_key,
                    "url": url,
                    "revised": revised,
                    "published_at": row.get("broadcast_Date"),
                },
            )
    return sorted(
        (item[1] for item in newest_by_period.values()),
        key=lambda filing: filing["period"],
        reverse=True,
    )


def _parse_governance_xbrl(
    *,
    record_id: str,
    period: str,
    payload: bytes,
    source_url: str | None,
    revised: bool,
    published_at: str | None,
) -> dict:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise GovernanceError(f"invalid governance XBRL for {record_id}") from error

    schema_ref = root.find(".//{http://www.xbrl.org/2003/linkbase}schemaRef")
    href = (
        schema_ref.get(f"{{{_XLINK}}}href", "")
        if schema_ref is not None
        else ""
    )
    facts: dict[str, list[str]] = {}
    for element in root:
        local_name = _local_name(element.tag)
        if local_name in {"context", "unit", "schemaRef"}:
            continue
        if local_name in _INFORMATIONAL_LOCAL_NAMES:
            continue
        text = (element.text or "").strip()
        if not text:
            continue
        facts.setdefault(local_name, []).append(text)

    exceptions = []
    for local_name, code, verdict, reason_code in _COMPLIANCE_TRUE_FACTS:
        values = facts.get(local_name, [])
        if values and any(_as_bool(value) is False for value in values):
            exceptions.append(
                _exception(
                    symbol_period=period,
                    code=code,
                    verdict=verdict,
                    reason_code=reason_code,
                    local_name=local_name,
                    detail="Reported as not compliant with SEBI LODR composition/conduct requirement.",
                    evidence_values=values,
                )
            )

    for local_name, code, verdict, reason_code in _TRUE_ADVERSE_FACTS:
        values = facts.get(local_name, [])
        if values and any(_as_bool(value) is True for value in values):
            exceptions.append(
                _exception(
                    symbol_period=period,
                    code=code,
                    verdict=verdict,
                    reason_code=reason_code,
                    local_name=local_name,
                    detail="Adverse governance indicator reported as true.",
                    evidence_values=values,
                )
            )

    violations = facts.get(
        "DetailsOfTheViolationOrContraventionCommittedOrAllegedToBeCommitted",
        [],
    )
    for index, detail in enumerate(violations):
        if not detail or detail.lower() in {"na", "n/a", "nil", "-"}:
            continue
        exceptions.append(
            _exception(
                symbol_period=period,
                code="governance_violation_or_contravention",
                verdict="REVIEW",
                reason_code="GOVERNANCE_DISCLOSURE_CAUTION",
                local_name="DetailsOfTheViolationOrContraventionCommittedOrAllegedToBeCommitted",
                detail=detail[:400],
                evidence_values=[detail],
                suffix=str(index + 1),
            )
        )

    pending_complaints = _max_int(
        facts.get("NoOfInvestorComplaints", [])
        + facts.get("NoOfInvestorComplaintsDuringThePeriod", [])
    )
    if pending_complaints is not None and pending_complaints > 0:
        exceptions.append(
            _exception(
                symbol_period=period,
                code="investor_grievance_pending",
                verdict="REVIEW",
                reason_code="GOVERNANCE_DISCLOSURE_CAUTION",
                local_name="NoOfInvestorComplaints",
                detail=f"{pending_complaints} investor complaint(s) pending at period end.",
                evidence_values=[str(pending_complaints)],
            )
        )

    return {
        "record_id": record_id,
        "period": period,
        "schema_ref": href or None,
        "source_url": source_url,
        "checksum": hashlib.sha256(payload).hexdigest(),
        "parser_version": GOVERNANCE_PARSER_VERSION,
        "revised": revised,
        "published_at": published_at,
        "exceptions": exceptions,
        "investor_complaints_pending": pending_complaints,
        "coverage": {
            "compliance_facts_present": any(
                local_name in facts for local_name, *_ in _COMPLIANCE_TRUE_FACTS
            ),
            "grievance_facts_present": bool(
                facts.get("NoOfInvestorComplaints")
                or facts.get("NoOfInvestorComplaintsDuringThePeriod")
                or facts.get("NoOfInvestorComplaintsReceivedDuringThePeriod")
            ),
            "violation_facts_present": bool(violations),
        },
    }


def _exception(
    *,
    symbol_period: str,
    code: str,
    verdict: str,
    reason_code: str,
    local_name: str,
    detail: str,
    evidence_values: list[str],
    suffix: str | None = None,
) -> dict:
    evidence_id = _stable_id(
        "GOVERNANCE",
        symbol_period,
        code,
        suffix or "",
        local_name,
        detail[:80],
    )
    return {
        "id": evidence_id,
        "kind": "governance_exception",
        "period": symbol_period,
        "code": code,
        "policy_verdict": verdict,
        "policy_reason_code": reason_code,
        "local_name": local_name,
        "detail": detail,
        "values": evidence_values[:5],
    }


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256(
        "|".join(part.strip() for part in parts if part).encode()
    ).hexdigest()
    return f"GOVERNANCE_{digest[:16].upper()}"


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].split(":", 1)[-1]


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _max_int(values: list[str]) -> int | None:
    parsed = []
    for value in values:
        try:
            parsed.append(int(float(value)))
        except (TypeError, ValueError):
            continue
    return max(parsed) if parsed else None


def _parse_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _DATE_FORMAT).replace(tzinfo=_IST)
    except ValueError:
        return None


def _parse_broadcast(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=_IST)
    try:
        return datetime.strptime(text, _BROADCAST_FORMAT).replace(tzinfo=_IST)
    except ValueError:
        return datetime.min.replace(tzinfo=_IST)


def _is_usable_xbrl_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    path = normalized.split("?", 1)[0].split("#", 1)[0]
    return normalized.startswith(("https://", "http://")) and path.endswith(
        ".xml"
    )
