"""Paced NSE material-disclosure warm-up with a cache-only scan interface."""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Protocol

import cache
from logging_config import setup_logging
from market_time import now_ist_naive
from nse_client import get_request

log = setup_logging("material_disclosures")

DISCLOSURE_SCHEMA_VERSION = "material-disclosures-v1"
DISCLOSURE_LOOKBACK_DAYS = int(
    os.environ.get("MATERIAL_DISCLOSURE_LOOKBACK_DAYS", "120")
)
DISCLOSURE_CACHE_TTL_HOURS = float(
    os.environ.get("MATERIAL_DISCLOSURE_CACHE_TTL_HOURS", "336")
)
DISCLOSURE_CALL_DELAY_SECONDS = float(
    os.environ.get("MATERIAL_DISCLOSURE_CALL_DELAY_SECONDS", "1")
)
DISCLOSURE_MAX_EVENTS = int(
    os.environ.get("MATERIAL_DISCLOSURE_MAX_EVENTS", "20")
)
DISCLOSURE_MAX_RATINGS = int(
    os.environ.get("MATERIAL_DISCLOSURE_MAX_RATINGS", "20")
)

_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
_CREDIT_RATINGS_URL = (
    "https://www.nseindia.com/api/credit-rating-sdd-reg30"
)
_EVENT_RULES = (
    (
        "payment_default",
        re.compile(
            r"\b(default(?:ed)?|payment failure|failed to pay|"
            r"delay in payment|debt servicing)\b",
            re.I,
        ),
        "REJECT",
        "ADVERSE_CORPORATE_EVENT",
    ),
    (
        "insolvency",
        re.compile(
            r"\b(insolvency|bankruptcy|corporate insolvency|"
            r"resolution process|liquidation)\b",
            re.I,
        ),
        "REJECT",
        "ADVERSE_CORPORATE_EVENT",
    ),
    (
        "fraud_or_forensic_audit",
        re.compile(r"\b(fraud|forensic audit|misappropriation)\b", re.I),
        "REJECT",
        "ADVERSE_CORPORATE_EVENT",
    ),
    (
        "auditor_issue",
        re.compile(
            r"\b(auditor.{0,40}(resign|qualification|qualified|"
            r"adverse opinion|disclaimer)|qualified audit opinion)\b",
            re.I,
        ),
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "regulatory_action",
        re.compile(
            r"\b(show cause|regulatory action|penalt(?:y|ies)|"
            r"enforcement action|inspection finding|non-compliance|"
            r"sebi order|rbi order)\b",
            re.I,
        ),
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "material_litigation",
        re.compile(
            r"\b(material litigation|material legal|court order|"
            r"arbitration|tax demand)\b",
            re.I,
        ),
        "REVIEW",
        "MATERIAL_DISCLOSURE_CAUTION",
    ),
    (
        "management_exit",
        re.compile(
            r"\b(resignation|ceased to be).{0,80}"
            r"\b(ceo|cfo|chief executive|chief financial|"
            r"managing director|key managerial|whole.time director)\b",
            re.I,
        ),
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
    ),
    (
        "dilution_or_fund_raise",
        re.compile(
            r"\b(preferential issue|qualified institutional placement|"
            r"rights issue|issue of equity shares|equity fund rais(?:e|ing)|"
            r"convertible warrant|allotment of equity)\b",
            re.I,
        ),
        "REVIEW",
        "DILUTION_DISCLOSURE_CAUTION",
    ),
)
_ROUTINE_DISCLOSURE = re.compile(
    r"\b(investor meet|analyst meet|conference call|newspaper "
    r"publication|annual general meeting notice|board meeting "
    r"intimation|trading window|loss of share certificate)\b",
    re.I,
)
_DEFAULT_RATING = re.compile(
    r"(?:^|[\s\[\]])(?:CRISIL\s+|ICRA\s+|CARE\s+|IND\s+)?"
    r"(?:D|SD)(?:$|[\s)/\]])",
    re.I,
)


class DisclosureSource(Protocol):
    def announcements(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> list[dict]: ...

    def credit_ratings(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> list[dict]: ...


class DisclosureStore(Protocol):
    def read(self, symbol: str, ttl_seconds: float) -> dict | None: ...

    def write(self, symbol: str, payload: dict) -> None: ...


class NseDisclosureSource:
    def announcements(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> list[dict]:
        return self._get(
            _ANNOUNCEMENTS_URL,
            {
                "index": "equities",
                "symbol": symbol,
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            },
        )

    def credit_ratings(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> list[dict]:
        return self._get(
            _CREDIT_RATINGS_URL,
            {
                "index": "equities",
                "symbol": symbol,
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            },
        )

    @staticmethod
    def _get(url: str, params: dict) -> list[dict]:
        response = get_request(url, params=params)
        time.sleep(DISCLOSURE_CALL_DELAY_SECONDS)
        if response is None:
            raise ConnectionError(f"NSE disclosure request failed for {url}")
        response.raise_for_status()
        payload = response.json()
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("data", [])
            if isinstance(payload, dict)
            else []
        )
        return [row for row in rows if isinstance(row, dict)]


class CacheDisclosureStore:
    @staticmethod
    def _key(symbol: str) -> str:
        return f"material_disclosures_v1_{symbol.upper()}"

    def read(self, symbol: str, ttl_seconds: float) -> dict | None:
        return cache.read(self._key(symbol), ttl_seconds)

    def write(self, symbol: str, payload: dict) -> None:
        cache.write(self._key(symbol), payload)


class MaterialDisclosureService:
    """Deep module for paced retrieval, normalization, policy tags, and cache."""

    def __init__(
        self,
        source: DisclosureSource,
        store: DisclosureStore,
        *,
        lookback_days: int = DISCLOSURE_LOOKBACK_DAYS,
        max_events: int = DISCLOSURE_MAX_EVENTS,
        max_ratings: int = DISCLOSURE_MAX_RATINGS,
    ):
        self._source = source
        self._store = store
        self._lookback_days = lookback_days
        self._max_events = max_events
        self._max_ratings = max_ratings

    def get(self, symbol: str) -> dict:
        cached = self._store.read(
            symbol,
            DISCLOSURE_CACHE_TTL_HOURS * 3600,
        )
        if cached is not None:
            return cached
        return _empty_feed(symbol, status="pending")

    def warm(self, symbol: str, *, as_of: date | None = None) -> dict:
        symbol = symbol.upper()
        end = as_of or now_ist_naive().date()
        start = end - timedelta(days=self._lookback_days)
        errors = []
        announcements = []
        ratings = []
        sources_succeeded = 0
        try:
            announcements = self._source.announcements(symbol, start, end)
            sources_succeeded += 1
        except Exception as error:
            errors.append(f"announcements:{type(error).__name__}")
            log.warning(
                "material_disclosures[%s]: announcement fetch failed",
                symbol,
                exc_info=True,
            )
        try:
            ratings = self._source.credit_ratings(symbol, start, end)
            sources_succeeded += 1
        except Exception as error:
            errors.append(f"credit_ratings:{type(error).__name__}")
            log.warning(
                "material_disclosures[%s]: rating fetch failed",
                symbol,
                exc_info=True,
            )

        events, event_errors = _normalize_announcements(
            symbol,
            announcements,
        )
        normalized_ratings, rating_errors = _normalize_ratings(
            symbol,
            ratings,
        )
        errors.extend(event_errors)
        errors.extend(rating_errors)
        events = _prioritize(events, self._max_events)
        normalized_ratings = _prioritize_ratings(
            normalized_ratings,
            self._max_ratings,
        )
        status = (
            "ready"
            if sources_succeeded == 2
            else "partial"
            if sources_succeeded
            else "unavailable"
        )
        payload = {
            "version": DISCLOSURE_SCHEMA_VERSION,
            "status": status,
            "symbol": symbol,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "lookback_days": self._lookback_days,
            "fetched_at": now_ist_naive().isoformat(),
            "events": events,
            "credit_ratings": normalized_ratings,
            "source_counts": {
                "announcements": len(announcements),
                "credit_ratings": len(ratings),
            },
            "errors": errors,
        }
        self._store.write(symbol, payload)
        return payload


def _normalize_announcements(
    symbol: str,
    rows: list[dict],
) -> tuple[list[dict], list[str]]:
    events = []
    errors = []
    for index, row in enumerate(rows):
        try:
            event = _normalize_announcement(symbol, row)
        except Exception as error:
            errors.append(
                f"announcement_row_{index}:{type(error).__name__}"
            )
            continue
        if event is not None:
            events.append(event)
    return _dedupe(events), errors


def _normalize_announcement(symbol: str, row: dict) -> dict | None:
    category = _text(row.get("desc"), 160)
    text = _text(row.get("attchmntText"), 500)
    combined = f"{category or ''} {text or ''}".strip()
    matched = next(
        (
            (event_type, verdict, reason_code)
            for event_type, pattern, verdict, reason_code in _EVENT_RULES
            if pattern.search(combined)
        ),
        None,
    )
    if matched is None:
        return None
    event_type, verdict, reason_code = matched
    source_date = _source_date(
        row.get("an_dt") or row.get("sort_date") or row.get("dt")
    )
    source_url = _text(row.get("attchmntFile"), 800)
    identity = (
        symbol,
        event_type,
        source_date[:10] if source_date else None,
        category,
    )
    return {
        "id": _stable_id("MATERIAL", *identity),
        "kind": "material_disclosure",
        "event_type": event_type,
        "policy_verdict": verdict,
        "policy_reason_code": reason_code,
        "date": source_date,
        "category": category,
        "text": text,
        "routine": bool(_ROUTINE_DISCLOSURE.search(combined)),
        "source": {
            "kind": "nse_corporate_announcement",
            "sequence_id": _text(row.get("seq_id"), 80),
            "url": source_url,
            "attachment_status": (
                "referenced"
                if source_url and source_url.startswith("https://")
                else "invalid_or_missing"
            ),
        },
    }


def _normalize_ratings(
    symbol: str,
    rows: list[dict],
) -> tuple[list[dict], list[str]]:
    ratings = []
    errors = []
    for index, row in enumerate(rows):
        try:
            ratings.append(_normalize_rating(symbol, row))
        except Exception as error:
            errors.append(f"rating_row_{index}:{type(error).__name__}")
    return _dedupe(ratings), errors


def _normalize_rating(symbol: str, row: dict) -> dict:
    agency = _text(row.get("creditAgencyName"), 160)
    instrument = _text(row.get("ratingAssigned"), 200)
    rating = _text(row.get("creditRating"), 120)
    outlook = _text(row.get("outlook"), 120)
    action_text = " ".join(
        filter(
            None,
            (
                _text(row.get("classOfAction"), 100),
                _text(row.get("currentAction"), 100),
                _text(row.get("brieFDetails"), 240),
                outlook,
                rating,
                _text(row.get("remarks"), 240),
            ),
        )
    )
    direction, verdict, reason_code = _rating_policy(
        rating,
        outlook,
        action_text,
    )
    source_date = _source_date(
        row.get("dateOfCurrentCredit")
        or row.get("broadcastDateTime")
    )
    amount_raw = _text(row.get("amount"), 80)
    return {
        "id": _stable_id(
            "RATING",
            symbol,
            agency,
            instrument,
            source_date,
            rating,
            direction,
            row.get("isin"),
        ),
        "kind": "credit_rating_action",
        "agency": agency,
        "instrument": instrument,
        "facility": instrument,
        "amount_raw": amount_raw,
        "amount_crore": _float(amount_raw),
        "rating": rating,
        "outlook_or_watch": outlook,
        "action": _text(row.get("currentAction"), 100),
        "action_direction": direction,
        "policy_verdict": verdict,
        "policy_reason_code": reason_code,
        "date": source_date,
        "source": {
            "kind": "nse_credit_rating_sdd_reg30",
            "broadcast_at": _text(row.get("broadcastDateTime"), 80),
            "url": _text(row.get("detailsOfRatingLink"), 800),
            "isin": _text(row.get("isin"), 40),
        },
    }


def _rating_policy(
    rating: str | None,
    outlook: str | None,
    action_text: str,
) -> tuple[str, str | None, str | None]:
    normalized = action_text.casefold()
    if rating and _DEFAULT_RATING.search(rating):
        return "default", "REJECT", "ADVERSE_CORPORATE_EVENT"
    if "non-cooperat" in normalized or "not cooperat" in normalized:
        return "non_cooperation", "REVIEW", "CREDIT_RATING_CAUTION"
    if "downgrad" in normalized:
        return "downgrade", "REVIEW", "CREDIT_RATING_CAUTION"
    if (
        "negative watch" in normalized
        or "watch negative" in normalized
        or "creditwatch with negative" in normalized
        or (outlook and outlook.casefold() == "negative")
    ):
        return "negative_watch", "REVIEW", "CREDIT_RATING_CAUTION"
    if "upgrad" in normalized:
        return "upgrade", None, None
    if "withdraw" in normalized:
        return "withdrawn", None, None
    if "no rating change" in normalized or "reaffirm" in normalized:
        return "affirmed", None, None
    return "other", None, None


def _prioritize(facts: list[dict], limit: int) -> list[dict]:
    return sorted(
        facts,
        key=lambda fact: (
            fact.get("policy_verdict") == "REJECT",
            fact.get("policy_verdict") == "REVIEW",
            fact.get("date") or "",
        ),
        reverse=True,
    )[:limit]


def _prioritize_ratings(facts: list[dict], limit: int) -> list[dict]:
    ordered = _prioritize(facts, len(facts))
    actionable = [
        fact for fact in ordered if fact.get("policy_verdict") is not None
    ]
    context = [
        fact for fact in ordered if fact.get("policy_verdict") is None
    ][:3]
    return [*actionable, *context][:limit]


def _dedupe(facts: list[dict]) -> list[dict]:
    unique = {}
    for fact in facts:
        unique.setdefault(fact["id"], fact)
    return list(unique.values())


def _empty_feed(symbol: str, *, status: str) -> dict:
    return {
        "version": DISCLOSURE_SCHEMA_VERSION,
        "status": status,
        "symbol": symbol.upper(),
        "window_start": None,
        "window_end": None,
        "lookback_days": DISCLOSURE_LOOKBACK_DAYS,
        "fetched_at": None,
        "events": [],
        "credit_ratings": [],
        "source_counts": {},
        "errors": [],
    }


def _stable_id(prefix: str, *parts: object) -> str:
    source = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(source.encode()).hexdigest()[:16].upper()
    return f"{prefix}_{digest}"


def _source_date(value: object) -> str | None:
    text = str(value or "").strip()
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y",
        "%Y-%m-%d",
        "%d%m%Y%H%M%S",
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text or None


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:limit]


def _float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


_service = MaterialDisclosureService(
    NseDisclosureSource(),
    CacheDisclosureStore(),
)


def get_material_disclosures(symbol: str) -> dict:
    """Cache-only interface used by the live scan."""
    return _service.get(symbol)


def warm_material_disclosures(
    symbol: str,
    *,
    as_of: date | None = None,
) -> dict:
    """Paced off-market interface used by the background warmer."""
    return _service.warm(symbol, as_of=as_of)
