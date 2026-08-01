"""Paced warm-up of annual reports and earnings materials for research facts."""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Protocol

import cache
from logging_config import setup_logging
from market_time import now_ist_naive
from nse_client import get_request

log = setup_logging("document_research")

DOCUMENT_RESEARCH_VERSION = "document-research-v1"
DOCUMENT_CACHE_TTL_HOURS = float(
    os.environ.get("DOCUMENT_RESEARCH_CACHE_TTL_HOURS", "720")
)
DOCUMENT_LOOKBACK_DAYS = int(
    os.environ.get("DOCUMENT_RESEARCH_LOOKBACK_DAYS", "400")
)
DOCUMENT_CALL_DELAY_SECONDS = float(
    os.environ.get("DOCUMENT_RESEARCH_CALL_DELAY_SECONDS", "1")
)
DOCUMENT_MAX_CHARS = int(os.environ.get("DOCUMENT_RESEARCH_MAX_CHARS", "120000"))
DOCUMENT_EXCERPT_CHARS = int(
    os.environ.get("DOCUMENT_RESEARCH_EXCERPT_CHARS", "700")
)

_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"

_DOC_RULES = (
    (
        "annual_report",
        re.compile(
            r"\b(annual report|integrated annual report|integrated report|"
            r"brsr)\b",
            re.I,
        ),
    ),
    (
        "investor_presentation",
        re.compile(
            r"\b(investor presentation|analyst presentation|"
            r"earnings presentation)\b",
            re.I,
        ),
    ),
    (
        "earnings_transcript",
        re.compile(
            r"\b(transcript|earnings call|conference call|concall)\b",
            re.I,
        ),
    ),
)

_FACT_RULES = (
    (
        "auditor_qualification",
        re.compile(
            r"\b(qualified(?:\s+audit)?\s+opinion|adverse(?:\s+audit)?\s+opinion|"
            r"disclaimer of opinion|auditor(?:'s)?\s+qualification)\b",
            re.I,
        ),
        "REVIEW",
        "GOVERNANCE_DISCLOSURE_CAUTION",
        False,
    ),
    (
        "contingent_liability",
        re.compile(
            r"\bcontingent liabilities?\b.{0,80}?"
            r"(?:rs\.?\s*|inr\s*|₹\s*)?([\d,.]+)\s*(crore|cr|million|mn|lakh)?",
            re.I | re.S,
        ),
        None,
        None,
        True,
    ),
    (
        "related_party_transaction",
        re.compile(
            r"\brelated[- ]party transactions?\b.{0,120}?"
            r"(?:rs\.?\s*|inr\s*|₹\s*)?([\d,.]+)\s*(crore|cr|million|mn|lakh)?",
            re.I | re.S,
        ),
        None,
        None,
        True,
    ),
    (
        "customer_concentration",
        re.compile(
            r"\b(top\s+(?:1|one|customer|10)|customer concentration|"
            r"single customer).{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
            re.I | re.S,
        ),
        None,
        None,
        True,
    ),
    (
        "bank_nim",
        re.compile(
            r"\b(?:net interest margin|NIM)\b[^%]{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
            re.I,
        ),
        None,
        None,
        True,
    ),
    (
        "bank_gnpa",
        re.compile(
            r"\b(?:gross NPA|GNPA)\b[^%]{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
            re.I,
        ),
        None,
        None,
        True,
    ),
    (
        "bank_nnpa",
        re.compile(
            r"\b(?:net NPA|NNPA)\b[^%]{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
            re.I,
        ),
        None,
        None,
        True,
    ),
    (
        "bank_aum",
        re.compile(
            r"\bAUM\b.{0,40}?(?:rs\.?\s*|inr\s*|₹\s*)?([\d,.]+)\s*(crore|cr|billion|bn)?",
            re.I,
        ),
        None,
        None,
        True,
    ),
    (
        "cost_of_funds",
        re.compile(
            r"\bcost of funds\b[^%]{0,40}?(\d{1,2}(?:\.\d+)?)\s*%",
            re.I,
        ),
        None,
        None,
        True,
    ),
)

_live_service = None
_warm_service = None


class DocumentResearchError(ValueError):
    pass


class DocumentSource(Protocol):
    def list_documents(self, symbol: str, start: date, end: date) -> list[dict]: ...

    def download(self, document: dict) -> bytes: ...


class DocumentStore(Protocol):
    def read(self, symbol: str, ttl_seconds: float) -> dict | None: ...

    def write(self, symbol: str, payload: dict) -> None: ...


class NseDocumentSource:
    def __init__(self, *, request_delay_seconds: float | None = None):
        self._delay = (
            DOCUMENT_CALL_DELAY_SECONDS
            if request_delay_seconds is None
            else request_delay_seconds
        )

    def list_documents(self, symbol: str, start: date, end: date) -> list[dict]:
        response = get_request(
            _ANNOUNCEMENTS_URL,
            params={
                "index": "equities",
                "symbol": symbol,
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            },
        )
        time.sleep(self._delay)
        rows = response.json() if response is not None else []
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        if not isinstance(rows, list):
            raise DocumentResearchError(
                f"unexpected NSE announcement payload for {symbol}"
            )
        return _select_documents(symbol, rows)

    def download(self, document: dict) -> bytes:
        response = get_request(document["source_url"])
        time.sleep(self._delay)
        if response is None or response.status_code >= 400:
            raise DocumentResearchError(
                f"document download failed for {document.get('document_id')}"
            )
        return response.content


class CacheDocumentStore:
    @staticmethod
    def _key(symbol: str) -> str:
        return f"document_research_v1_{symbol.upper()}"

    def read(self, symbol: str, ttl_seconds: float) -> dict | None:
        return cache.read(self._key(symbol), ttl_seconds)

    def write(self, symbol: str, payload: dict) -> None:
        cache.write(self._key(symbol), payload)


class DocumentResearchService:
    def __init__(
        self,
        source: DocumentSource,
        store: DocumentStore,
        *,
        download_missing: bool = True,
        lookback_days: int = DOCUMENT_LOOKBACK_DAYS,
    ):
        self._source = source
        self._store = store
        self._download_missing = download_missing
        self._lookback_days = lookback_days

    def get(self, symbol: str) -> dict:
        symbol = symbol.upper()
        cached = self._store.read(symbol, DOCUMENT_CACHE_TTL_HOURS * 3600)
        if cached is not None and not self._download_missing:
            return cached
        if not self._download_missing:
            return _empty_research(symbol, status="pending")
        return self.warm(symbol)

    def warm(self, symbol: str, *, as_of: date | None = None) -> dict:
        symbol = symbol.upper()
        end = as_of or now_ist_naive().date()
        start = end - timedelta(days=self._lookback_days)
        errors: list[str] = []
        try:
            documents = self._source.list_documents(symbol, start, end)
        except Exception as error:
            log.warning(
                "document_research[%s]: discovery failed",
                symbol,
                exc_info=True,
            )
            payload = _empty_research(
                symbol,
                status="unavailable",
                errors=[f"discovery:{type(error).__name__}"],
                window_start=start.isoformat(),
                window_end=end.isoformat(),
            )
            self._store.write(symbol, payload)
            return payload

        prior = self._store.read(symbol, DOCUMENT_CACHE_TTL_HOURS * 3600) or {}
        prior_docs = {
            item.get("document_id"): item
            for item in prior.get("documents") or []
            if isinstance(item, dict) and item.get("document_id")
        }

        processed = []
        facts = []
        for document in documents:
            document_id = document["document_id"]
            existing = prior_docs.get(document_id)
            if (
                existing
                and existing.get("processing_state") == "ready"
                and existing.get("checksum")
            ):
                processed.append(existing)
                facts.extend(existing.get("facts") or [])
                continue
            try:
                payload_bytes = self._source.download(document)
                record = _process_document(document, payload_bytes)
                processed.append(record)
                facts.extend(record.get("facts") or [])
            except Exception as error:
                errors.append(f"{document_id}:{type(error).__name__}")
                processed.append(
                    {
                        **document,
                        "processing_state": "failed",
                        "error": type(error).__name__,
                        "facts": [],
                    }
                )
                log.warning(
                    "document_research[%s]: document %s failed; continuing",
                    symbol,
                    document_id,
                    exc_info=True,
                )

        status = "ready" if any(
            item.get("processing_state") == "ready" for item in processed
        ) else ("unavailable" if errors else "ready")
        payload = {
            "version": DOCUMENT_RESEARCH_VERSION,
            "status": status,
            "symbol": symbol,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "lookback_days": self._lookback_days,
            "fetched_at": now_ist_naive().isoformat(),
            "documents": processed,
            "facts": _dedupe_facts(facts),
            "errors": errors,
            "document_counts": {
                "discovered": len(documents),
                "ready": sum(
                    1
                    for item in processed
                    if item.get("processing_state") == "ready"
                ),
                "failed": sum(
                    1
                    for item in processed
                    if item.get("processing_state") == "failed"
                ),
            },
        }
        self._store.write(symbol, payload)
        return payload


def get_document_research(symbol: str) -> dict:
    """Cache-only research ledger for live scans."""
    global _live_service
    if _live_service is None:
        _live_service = DocumentResearchService(
            NseDocumentSource(),
            CacheDocumentStore(),
            download_missing=False,
        )
    return _live_service.get(symbol)


def warm_document_research(symbol: str) -> dict:
    """Paced off-market warm used by the dedicated CLI/scheduler."""
    global _warm_service
    if _warm_service is None:
        _warm_service = DocumentResearchService(
            NseDocumentSource(),
            CacheDocumentStore(),
            download_missing=True,
        )
    return _warm_service.warm(symbol)


def _empty_research(
    symbol: str,
    *,
    status: str,
    errors: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    return {
        "version": DOCUMENT_RESEARCH_VERSION,
        "status": status,
        "symbol": symbol.upper(),
        "window_start": window_start,
        "window_end": window_end,
        "lookback_days": DOCUMENT_LOOKBACK_DAYS,
        "fetched_at": None,
        "documents": [],
        "facts": [],
        "errors": list(errors or []),
        "document_counts": {"discovered": 0, "ready": 0, "failed": 0},
    }


def _select_documents(symbol: str, rows: list) -> list[dict]:
    selected: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("attchmntFile") or "").strip()
        if not url.lower().startswith(("https://", "http://")):
            continue
        if not url.lower().split("?", 1)[0].endswith(".pdf"):
            continue
        haystack = " ".join(
            str(row.get(field) or "")
            for field in ("desc", "attchmntText", "attchmntFile")
        )
        doc_type = None
        for name, pattern in _DOC_RULES:
            if pattern.search(haystack):
                doc_type = name
                break
        if doc_type is None:
            continue
        announced_at = str(row.get("an_dt") or row.get("exchdisstime") or "")
        document_id = _stable_id("DOC", symbol, doc_type, url, announced_at)
        current = selected.get(doc_type)
        rank = announced_at
        if current is None or rank > str(current.get("announced_at") or ""):
            selected[doc_type] = {
                "document_id": document_id,
                "doc_type": doc_type,
                "symbol": symbol.upper(),
                "source_url": url,
                "nse_subject": str(row.get("desc") or ""),
                "nse_text": str(row.get("attchmntText") or "")[:300],
                "announced_at": announced_at,
                "reporting_period": _infer_period(haystack, announced_at),
            }
    order = {"annual_report": 0, "investor_presentation": 1, "earnings_transcript": 2}
    return sorted(
        selected.values(),
        key=lambda item: order.get(item["doc_type"], 99),
    )


def _process_document(document: dict, payload: bytes) -> dict:
    checksum = hashlib.sha256(payload).hexdigest()
    text, page_count = _extract_pdf_text(payload)
    facts = _extract_facts(document, text)
    return {
        **document,
        "retrieved_at": now_ist_naive().isoformat(),
        "checksum": checksum,
        "byte_length": len(payload),
        "page_count": page_count,
        "processing_state": "ready",
        "text_chars": len(text),
        "facts": facts,
    }


def _extract_pdf_text(payload: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise DocumentResearchError("pypdf is required for document research") from error
    reader = PdfReader(BytesIO(payload))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text.strip():
            pages.append(f"[page {index}]\n{page_text}")
        if sum(len(part) for part in pages) >= DOCUMENT_MAX_CHARS:
            break
    text = "\n\n".join(pages)[:DOCUMENT_MAX_CHARS]
    return text, len(reader.pages)


def _extract_facts(document: dict, text: str) -> list[dict]:
    facts = []
    for code, pattern, verdict, reason_code, numeric in _FACT_RULES:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 120)
            excerpt = re.sub(r"\s+", " ", text[start:end]).strip()
            page = _page_for_offset(text, match.start())
            value = None
            unit = None
            if numeric and match.lastindex:
                value = _parse_number(match.group(1))
                if match.lastindex >= 2:
                    unit = match.group(2)
            fact = {
                "id": _stable_id(
                    "RESEARCH",
                    document["document_id"],
                    code,
                    excerpt[:80],
                ),
                "kind": "document_research_fact",
                "code": code,
                "doc_type": document["doc_type"],
                "document_id": document["document_id"],
                "source_url": document["source_url"],
                "reporting_period": document.get("reporting_period"),
                "page": page,
                "excerpt": excerpt[:DOCUMENT_EXCERPT_CHARS],
                "numeric": numeric,
                "value": value,
                "unit": unit,
                "extraction_method": "deterministic_regex",
                "optional": True,
            }
            if verdict:
                fact["policy_verdict"] = verdict
                fact["policy_reason_code"] = reason_code
            facts.append(fact)
            break
    return facts


def _dedupe_facts(facts: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for fact in facts:
        fact_id = fact.get("id")
        if not fact_id or fact_id in seen:
            continue
        seen.add(fact_id)
        deduped.append(fact)
    return deduped


def _page_for_offset(text: str, offset: int) -> int | None:
    page = None
    for match in re.finditer(r"\[page (\d+)\]", text):
        if match.start() > offset:
            break
        page = int(match.group(1))
    return page


def _parse_number(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _infer_period(haystack: str, announced_at: str) -> str | None:
    match = re.search(
        r"(?:quarter|q[1-4]|year|fy|ended).{0,40}?"
        r"(\d{1,2}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"[-/ ]\d{2,4}|\d{4})",
        haystack,
        re.I,
    )
    if match:
        return match.group(0)[:80]
    if announced_at:
        try:
            return datetime.strptime(
                announced_at.split()[0], "%d-%b-%Y"
            ).date().isoformat()
        except ValueError:
            return announced_at[:10]
    return None


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256(
        "|".join(part.strip() for part in parts if part).encode()
    ).hexdigest()
    return f"RESEARCH_{digest[:16].upper()}"
