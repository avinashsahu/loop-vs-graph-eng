from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from nse_client import get_request

_XBRLI = "http://www.xbrl.org/2003/instance"
_XBRLDI = "http://xbrl.org/2006/xbrldi"
_XLINK = "http://www.w3.org/1999/xlink"
_MANIFEST_URL = "https://www.nseindia.com/api/corporate-share-holdings-master"
_IST = ZoneInfo("Asia/Kolkata")
PARSER_VERSION = "nse-shp-xbrl-v5"
_live_service = None
_warm_service = None
_registry_store = None
_MEMBERS = {
    "InstitutionsForeignMember": "fii",
    "InstitutionsDomesticMember": "dii",
    "GovernmentsMember": "government",
    "GovermentsMember": "government",
    "NonInstitutionsMember": "other_public",
    "PublicShareholdingMember": "public",
    "PromoterAndPromoterGroupMember": "promoter",
    "ShareholdingOfPromoterAndPromoterGroupMember": "promoter",
}
_PROMOTER_MEMBERS = frozenset(
    {
        "PromoterAndPromoterGroupMember",
        "ShareholdingOfPromoterAndPromoterGroupMember",
    }
)
_TOTAL_PATTERN_MEMBERS = frozenset({"ShareholdingPatternMember"})
_ENCUMBERED_SHARES_FACTS = frozenset({"NumberOfSharesEncumbered"})
_ENCUMBERED_PCT_FACTS = frozenset(
    {"EncumberedSharesHeldAsPercentageOfTotalNumberOfShares"}
)
_PLEDGE_FLAG_FACTS = frozenset(
    {
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertakingForPromoterAndPromoterGroup",
        "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU",
        "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDUForPromoterAndPromoterGroup",
    }
)


class ShareholdingError(ValueError):
    pass


class NseShareholdingRequestError(ConnectionError):
    pass


class FilingSource(Protocol):
    def list_filings(self, symbol: str) -> list[dict]: ...

    def download(self, filing: dict) -> bytes: ...


class FilingStore(Protocol):
    def get(self, record_id: str) -> dict | None: ...

    def put(self, record_id: str, record: dict) -> None: ...

    def update_normalized(self, record_id: str, period: ShareholdingPeriod) -> None: ...

    def get_manifest(
        self, symbol: str, *, allow_stale: bool = False
    ) -> list[dict] | None: ...

    def manifest_is_fresh(self, symbol: str) -> bool: ...

    def put_manifest(self, symbol: str, filings: list[dict]) -> None: ...

    def enqueue_warm(self, symbol: str, record_ids: list[str]) -> None: ...

    def queued_symbols(self) -> list[str]: ...

    def complete_warm(self, symbol: str) -> None: ...


@dataclass(frozen=True)
class ShareholdingPeriod:
    record_id: str
    period: str
    schema_version: str
    fii_pct: float
    dii_pct: float
    government_pct: float
    promoter_pct: float
    other_public_pct: float
    public_shares: int
    component_shares: int
    reconciled: bool
    checksum: str
    schema_ref: str | None = None
    source_url: str | None = None
    parser_version: str = PARSER_VERSION
    validation_status: str = "reconciled"
    promoter_encumbered_shares: int | None = None
    promoter_encumbered_pct_of_total: float | None = None
    promoter_encumbered_pct_of_promoter: float | None = None
    pledge_disclosed: bool = False


@dataclass(frozen=True)
class ShareholdingHistory:
    symbol: str
    periods: tuple[ShareholdingPeriod, ...]
    status: str = "ready"
    pending_record_ids: tuple[str, ...] = ()
    latest_period: str | None = None
    latest_record_id: str | None = None
    periods_available: int = 0
    complete: bool = False
    changes_bps: dict | None = None
    trend_labels: dict | None = None


def select_due_universe_symbols(
    records: list[dict],
    *,
    universe: str,
    now_epoch: int,
    refresh_after_seconds: int,
    incomplete_retry_seconds: int,
    limit: int,
) -> list[str]:
    """Select active never-warmed/stale members in stable oldest-first order."""
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    cutoff = now_epoch - refresh_after_seconds
    due = []
    for record in records:
        if (
            record.get("universe") != universe
            or not record.get("active")
            or not record.get("symbol")
        ):
            continue
        completed_at = int(record.get("completed_at") or 0)
        last_attempted_at = int(record.get("last_attempt") or 0)
        if (
            record.get("last_status") == "incomplete"
            and last_attempted_at > now_epoch - incomplete_retry_seconds
        ):
            continue
        if completed_at == 0 or completed_at <= cutoff:
            priority_time = completed_at or last_attempted_at
            due.append((priority_time, str(record["symbol"])))
    due.sort()
    return [symbol for _, symbol in due[:limit]]


class NseShareholdingSource:
    def __init__(
        self,
        *,
        request_delay_seconds: float | None = None,
        lookback_days: int | None = None,
        jitter_seconds: float | None = None,
    ):
        self._delay = (
            float(os.environ.get("NSE_XBRL_CALL_DELAY_SECONDS", "2"))
            if request_delay_seconds is None
            else request_delay_seconds
        )
        self._lookback_days = (
            int(os.environ.get("NSE_XBRL_LOOKBACK_DAYS", "730"))
            if lookback_days is None
            else lookback_days
        )
        self._jitter = (
            float(os.environ.get("NSE_XBRL_JITTER_SECONDS", "0.5"))
            if jitter_seconds is None
            else jitter_seconds
        )

    def list_filings(self, symbol: str) -> list[dict]:
        today = datetime.now(_IST).date()
        response = self._request(
            _MANIFEST_URL,
            params={
                "index": "equities",
                "from_date": (today - timedelta(days=self._lookback_days)).strftime(
                    "%d-%m-%Y"
                ),
                "to_date": today.strftime("%d-%m-%Y"),
                "symbol": symbol,
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise ShareholdingError(f"unexpected NSE shareholding manifest for {symbol}")

        filings = []
        for row in rows:
            if (
                not row.get("recordId")
                or not _is_usable_xbrl_url(row.get("xbrl"))
                or not row.get("date")
            ):
                continue
            period = (
                datetime.strptime(row["date"], "%d-%b-%Y")
                .replace(tzinfo=_IST)
                .date()
                .isoformat()
            )
            filing = {
                "record_id": str(row["recordId"]),
                "period": period,
                "url": row["xbrl"],
                "revised": str(
                    row.get("revisedData") or row.get("revisedStatus") or ""
                ).strip().upper()
                in {"Y", "YES", "REVISED"},
                "published_at": row.get("revisionDate")
                or row.get("systemDate")
                or row.get("submissionDate"),
            }
            filings.append(filing)
        return filings

    def download(self, filing: dict) -> bytes:
        return self._request(filing["url"]).content

    def _request(self, url: str, params: dict | None = None):
        response = get_request(url, params=params)
        time.sleep(self._delay + random.uniform(0, self._jitter))
        if response is not None and response.status_code < 400:
            return response
        if response is not None and response.status_code in {401, 403, 429}:
            raise NseShareholdingRequestError(
                f"NSE blocked shareholding request with HTTP {response.status_code}"
            )
        raise NseShareholdingRequestError(
            "NSE shareholding request failed repeatedly"
        )


class AerospikeFilingStore:
    """Persistent immutable filing records in Aerospike Community Edition."""

    def __init__(self):
        try:
            import aerospike
        except ImportError as error:
            raise RuntimeError(
                "Aerospike cache selected but the aerospike package is not installed"
            ) from error

        self._aerospike = aerospike
        host = os.environ.get("AEROSPIKE_HOST", "127.0.0.1")
        port = int(os.environ.get("AEROSPIKE_PORT", "3000"))
        self._namespace = os.environ.get("AEROSPIKE_NAMESPACE", "nse")
        self._set = os.environ.get("AEROSPIKE_SHAREHOLDING_SET", "shareholding")
        self._queue_set = f"{self._set}_warm"
        self._universe_set = os.environ.get(
            "AEROSPIKE_SHAREHOLDING_UNIVERSE_SET",
            f"{self._set}_universe",
        )
        self._manifest_ttl = int(
            os.environ.get("NSE_XBRL_MANIFEST_TTL_SECONDS", "21600")
        )
        self._client = aerospike.client({"hosts": [(host, port)]}).connect()

    def get(self, record_id: str) -> dict | None:
        try:
            _, _, bins = self._client.get(self._key(f"filing:{record_id}"))
        except self._aerospike.exception.RecordNotFound:
            return None
        normalized = json.loads(bins["norm_json"])
        payload = gzip.decompress(bins["raw_gz"])
        if hashlib.sha256(payload).hexdigest() != bins["sha256"]:
            raise ShareholdingError(
                f"cached XBRL checksum mismatch for record {record_id}"
            )
        if bins.get("parser") != PARSER_VERSION:
            upgraded = _cache_record(
                _parse_xbrl(
                    record_id=record_id,
                    expected_period=normalized["period"],
                    payload=payload,
                ),
                payload,
            )
            self._client.put(
                self._key(f"filing:{record_id}"),
                {
                    "norm_json": json.dumps(
                        upgraded["normalized"], separators=(",", ":")
                    ),
                    "parser": PARSER_VERSION,
                },
            )
            return upgraded
        return {
            "normalized": normalized,
            "checksum": bins["sha256"],
            "parser_version": bins["parser"],
            "raw_gzip": bins["raw_gz"],
        }

    def put(self, record_id: str, record: dict) -> None:
        bins = {
            "norm_json": json.dumps(record["normalized"], separators=(",", ":")),
            "sha256": record["checksum"],
            "parser": record["parser_version"],
            "raw_gz": record["raw_gzip"],
        }
        try:
            self._client.put(
                self._key(f"filing:{record_id}"),
                bins,
                policy={"exists": self._aerospike.POLICY_EXISTS_CREATE},
            )
        except self._aerospike.exception.RecordExistsError:
            return

    def update_normalized(
        self, record_id: str, period: ShareholdingPeriod
    ) -> None:
        self._client.put(
            self._key(f"filing:{record_id}"),
            {
                "norm_json": json.dumps(asdict(period), separators=(",", ":")),
                "parser": PARSER_VERSION,
            },
        )

    def get_manifest(
        self, symbol: str, *, allow_stale: bool = False
    ) -> list[dict] | None:
        try:
            _, _, bins = self._client.get(self._key(f"manifest:{symbol}"))
        except self._aerospike.exception.RecordNotFound:
            if not allow_stale:
                return None
            try:
                _, _, bins = self._client.get(self._key(f"index:{symbol}"))
            except self._aerospike.exception.RecordNotFound:
                return None
            self.enqueue_warm(symbol, [])
        return json.loads(bins["filings"])

    def manifest_is_fresh(self, symbol: str) -> bool:
        try:
            _, metadata = self._client.exists(self._key(f"manifest:{symbol}"))
        except self._aerospike.exception.RecordNotFound:
            return False
        return metadata is not None

    def put_manifest(self, symbol: str, filings: list[dict]) -> None:
        self._client.put(
            self._key(f"manifest:{symbol}"),
            {"filings": json.dumps(filings, separators=(",", ":"))},
            policy={"ttl": self._manifest_ttl},
        )
        self._client.put(
            self._key(f"index:{symbol}"),
            {"filings": json.dumps(filings, separators=(",", ":"))},
        )

    def enqueue_warm(self, symbol: str, record_ids: list[str]) -> None:
        self._client.put(
            self._key(symbol, set_name=self._queue_set),
            {
                "symbol": symbol,
                "records": json.dumps(record_ids, separators=(",", ":")),
                "requested": int(time.time()),
            },
            policy={"ttl": 7 * 24 * 3600},
        )

    def queued_symbols(self) -> list[str]:
        records = self._client.scan(self._namespace, self._queue_set).results()
        return sorted(
            {
                bins["symbol"]
                for _, _, bins in records
                if isinstance(bins, dict) and bins.get("symbol")
            }
        )

    def complete_warm(self, symbol: str) -> None:
        try:
            self._client.remove(
                self._key(symbol, set_name=self._queue_set),
            )
        except self._aerospike.exception.RecordNotFound:
            return

    def seed_universe(self, universe: str, symbols: list[str]) -> int:
        now_epoch = int(time.time())
        normalized = sorted(
            {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        )
        records = self._client.scan(
            self._namespace,
            self._universe_set,
        ).results()
        existing = {
            str(bins["symbol"]): bins
            for _, _, bins in records
            if isinstance(bins, dict)
            and bins.get("universe") == universe
            and bins.get("symbol")
        }
        active_symbols = set(normalized)
        previously_active = {
            symbol for symbol, bins in existing.items() if bins.get("active")
        }
        membership_is_complete = (
            not previously_active
            or len(active_symbols) >= len(previously_active) * 0.95
        )
        for symbol, bins in existing.items():
            if (
                not membership_is_complete
                or symbol in active_symbols
                or not bins.get("active")
            ):
                continue
            self._client.put(
                self._key(
                    f"{universe}:{symbol}",
                    set_name=self._universe_set,
                ),
                {"active": 0, "seeded_at": now_epoch},
            )
        for symbol in normalized:
            prior = existing.get(symbol, {})
            self._client.put(
                self._key(
                    f"{universe}:{symbol}",
                    set_name=self._universe_set,
                ),
                {
                    "universe": universe,
                    "symbol": symbol,
                    "active": 1,
                    "seeded_at": now_epoch,
                    "completed_at": int(prior.get("completed_at") or 0),
                    "last_attempt": int(prior.get("last_attempt") or 0),
                    "last_status": str(prior.get("last_status") or "pending"),
                    "periods": int(prior.get("periods") or 0),
                },
            )
        return len(normalized)

    def due_universe_symbols(
        self,
        universe: str,
        *,
        limit: int,
        refresh_after_seconds: int,
        incomplete_retry_seconds: int,
    ) -> list[str]:
        records = self._client.scan(
            self._namespace,
            self._universe_set,
        ).results()
        bins = [
            record_bins
            for _, _, record_bins in records
            if isinstance(record_bins, dict)
        ]
        return select_due_universe_symbols(
            bins,
            universe=universe,
            now_epoch=int(time.time()),
            refresh_after_seconds=refresh_after_seconds,
            incomplete_retry_seconds=incomplete_retry_seconds,
            limit=limit,
        )

    def list_universe(self, universe: str) -> list[dict]:
        """All known members of `universe`, active or not -- for coverage
        reporting, unlike due_universe_symbols which filters to due-only."""
        records = self._client.scan(
            self._namespace,
            self._universe_set,
        ).results()
        return [
            record_bins
            for _, _, record_bins in records
            if isinstance(record_bins, dict) and record_bins.get("universe") == universe
        ]

    def record_universe_attempt(
        self,
        universe: str,
        symbol: str,
        *,
        complete: bool,
        periods_available: int,
        reason_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        now_epoch = int(time.time())
        bins = {
            "universe": universe,
            "symbol": symbol,
            "active": 1,
            "last_attempt": now_epoch,
            "last_status": "complete" if complete else "incomplete",
            "periods": periods_available,
            "last_reason": reason_code or "",
            "last_error": (error_detail or "")[:1024],
        }
        if complete:
            bins["completed_at"] = now_epoch
        self._client.put(
            self._key(
                f"{universe}:{symbol}",
                set_name=self._universe_set,
            ),
            bins,
        )

    def _key(
        self, user_key: str, *, set_name: str | None = None
    ) -> tuple[str, str, str]:
        return self._namespace, set_name or self._set, user_key


class ShareholdingHistoryService:
    """Load a normalized history while hiding NSE, XBRL, and cache details."""

    def __init__(
        self,
        source: FilingSource,
        store: FilingStore,
        *,
        download_missing: bool = True,
    ):
        self._source = source
        self._store = store
        self._download_missing = download_missing

    def get(self, symbol: str, periods: int = 5) -> ShareholdingHistory:
        manifest_fresh = self._store.manifest_is_fresh(symbol)
        filings = self._store.get_manifest(
            symbol, allow_stale=not self._download_missing
        )
        if filings is None:
            if not self._download_missing:
                self._store.enqueue_warm(symbol, [])
                return ShareholdingHistory(
                    symbol=symbol,
                    periods=(),
                    status="pending",
                )
            filings = self._source.list_filings(symbol)
            self._store.put_manifest(symbol, filings)
            manifest_fresh = True
        by_period: dict[str, list[dict]] = {}
        for filing in filings:
            if (
                not _is_canonical_quarter_end(filing.get("period"))
                or not _is_usable_xbrl_url(filing.get("url"))
            ):
                continue
            by_period.setdefault(filing["period"], []).append(filing)
        selected_periods = sorted(by_period, reverse=True)[:periods]
        normalized = []
        pending = []
        request_errors = []
        for filing_period in selected_periods:
            candidates = sorted(
                by_period[filing_period], key=_revision_rank, reverse=True
            )
            if not self._download_missing:
                period = self._load(candidates[0])
                if period is None:
                    pending.append(str(candidates[0]["record_id"]))
                else:
                    normalized.append(period)
                continue

            last_parse_error = None
            last_request_error = None
            for filing in candidates:
                try:
                    normalized.append(self._load(filing))
                    break
                except ShareholdingError as error:
                    last_parse_error = error
                except NseShareholdingRequestError as error:
                    last_request_error = error
            else:
                if last_parse_error is not None:
                    raise last_parse_error
                if last_request_error is not None:
                    request_errors.append(last_request_error)
        if not normalized and request_errors:
            raise request_errors[-1]
        if pending:
            self._store.enqueue_warm(symbol, pending)
            return _history(
                symbol,
                normalized,
                status="pending",
                pending_record_ids=pending,
            )
        if self._download_missing:
            self._store.complete_warm(symbol)
        elif not manifest_fresh:
            # Manifest TTL is only a refresh signal. Cached quarters remain usable
            # for live scans; queue a background re-warm instead of REVIEW.
            self._store.enqueue_warm(symbol, [])
        return _history(symbol, normalized, status="ready")

    def _load(self, filing: dict) -> ShareholdingPeriod | None:
        record_id = str(filing["record_id"])
        cached = self._store.get(record_id)
        if cached is not None:
            period = ShareholdingPeriod(**cached["normalized"])
            if not period.source_url and filing.get("url"):
                period = replace(period, source_url=filing["url"])
                self._store.update_normalized(record_id, period)
            return period
        if not self._download_missing:
            return None

        payload = self._source.download(filing)
        parsed = replace(
            _parse_xbrl(
                record_id=record_id,
                expected_period=filing["period"],
                payload=payload,
            ),
            source_url=filing.get("url"),
        )
        self._store.put(record_id, _cache_record(parsed, payload))
        return parsed


def get_shareholding_history(symbol: str, periods: int = 5) -> ShareholdingHistory:
    """Read warmed history for a live scan; report pending instead of downloading inline."""
    global _live_service
    if _live_service is None:
        _live_service = ShareholdingHistoryService(
            NseShareholdingSource(),
            AerospikeFilingStore(),
            download_missing=False,
        )
    return _live_service.get(symbol, periods)


def warm_shareholding_history(symbol: str, periods: int = 5) -> ShareholdingHistory:
    """Paced off-market fetch used by the dedicated warmer."""
    global _warm_service
    if _warm_service is None:
        _warm_service = ShareholdingHistoryService(
            NseShareholdingSource(),
            AerospikeFilingStore(),
            download_missing=True,
        )
    return _warm_service.get(symbol, periods)


def queued_shareholding_symbols() -> list[str]:
    return AerospikeFilingStore().queued_symbols()


def _universe_store() -> AerospikeFilingStore:
    global _registry_store
    if _registry_store is None:
        _registry_store = AerospikeFilingStore()
    return _registry_store


def seed_shareholding_universe(universe: str, symbols: list[str]) -> int:
    return _universe_store().seed_universe(universe, symbols)


def due_shareholding_universe_symbols(
    universe: str,
    *,
    limit: int,
    refresh_after_days: int,
    incomplete_retry_days: int,
) -> list[str]:
    return _universe_store().due_universe_symbols(
        universe,
        limit=limit,
        refresh_after_seconds=refresh_after_days * 24 * 3600,
        incomplete_retry_seconds=incomplete_retry_days * 24 * 3600,
    )


def record_shareholding_universe_attempt(
    universe: str,
    symbol: str,
    *,
    complete: bool,
    periods_available: int,
    reason_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    _universe_store().record_universe_attempt(
        universe,
        symbol,
        complete=complete,
        periods_available=periods_available,
        reason_code=reason_code,
        error_detail=error_detail,
    )


def _cache_record(period: ShareholdingPeriod, payload: bytes) -> dict:
    return {
        "normalized": asdict(period),
        "checksum": period.checksum,
        "parser_version": PARSER_VERSION,
        "raw_gzip": gzip.compress(payload),
    }


def _history(
    symbol: str,
    periods: list[ShareholdingPeriod],
    *,
    status: str = "ready",
    pending_record_ids: list[str] | None = None,
) -> ShareholdingHistory:
    latest = periods[0] if periods else None
    changes = {}
    labels = {}
    if len(periods) >= 2:
        for field in (
            "fii_pct",
            "dii_pct",
            "government_pct",
            "promoter_pct",
            "other_public_pct",
        ):
            changes[f"{field.removesuffix('_pct')}_qoq"] = round(
                (getattr(periods[0], field) - getattr(periods[1], field)) * 100
            )
        encumbered_qoq = _encumbrance_delta_bps(periods[0], periods[1])
        if encumbered_qoq is not None:
            changes["promoter_encumbered_qoq"] = encumbered_qoq
    if len(periods) >= 5:
        for field in (
            "fii_pct",
            "dii_pct",
            "government_pct",
            "promoter_pct",
            "other_public_pct",
        ):
            name = field.removesuffix("_pct")
            delta = round(
                (getattr(periods[0], field) - getattr(periods[4], field)) * 100
            )
            changes[f"{name}_4q"] = delta
            labels[name] = "rising" if delta > 0 else "falling" if delta < 0 else "flat"
        encumbered_4q = _encumbrance_delta_bps(periods[0], periods[4])
        if encumbered_4q is not None:
            changes["promoter_encumbered_4q"] = encumbered_4q
            labels["promoter_encumbered"] = (
                "rising"
                if encumbered_4q > 0
                else "falling"
                if encumbered_4q < 0
                else "flat"
            )
    return ShareholdingHistory(
        symbol=symbol,
        periods=tuple(periods),
        status=status,
        pending_record_ids=tuple(pending_record_ids or ()),
        latest_period=latest.period if latest else None,
        latest_record_id=latest.record_id if latest else None,
        periods_available=len(periods),
        complete=status == "ready" and _has_consecutive_quarters(periods, 5),
        changes_bps=changes,
        trend_labels=labels,
    )


def _encumbrance_delta_bps(
    newer: ShareholdingPeriod, older: ShareholdingPeriod
) -> int | None:
    left = newer.promoter_encumbered_pct_of_total
    right = older.promoter_encumbered_pct_of_total
    if left is None or right is None:
        return None
    return round((left - right) * 100)


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _as_ratio_pct(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(Decimal(value.strip()))
    except Exception:
        return None
    # SHP pure-unit percentages are ratios (0.05 == 5%).
    if abs(number) <= 1.5:
        number *= 100
    return round(number, 4)


def _has_consecutive_quarters(
    periods: list[ShareholdingPeriod], required: int
) -> bool:
    if len(periods) < required:
        return False
    try:
        expected = date.fromisoformat(periods[0].period)
        for period in periods[:required]:
            if date.fromisoformat(period.period) != expected:
                return False
            expected = _previous_quarter_end(expected)
        return True
    except (ShareholdingError, ValueError):
        return False


def _previous_quarter_end(value: date) -> date:
    previous = {
        (3, 31): (value.year - 1, 12, 31),
        (6, 30): (value.year, 3, 31),
        (9, 30): (value.year, 6, 30),
        (12, 31): (value.year, 9, 30),
    }.get((value.month, value.day))
    if previous is None:
        raise ShareholdingError(f"Not a quarter-end period: {value.isoformat()}")
    return date(*previous)


def _revision_rank(filing: dict) -> tuple[bool, str, int]:
    return (
        bool(filing.get("revised")),
        filing.get("published_at") or "",
        int(filing["record_id"]),
    )


def _is_canonical_quarter_end(period: object) -> bool:
    value = str(period or "")
    return value[4:] in {
        "-03-31",
        "-06-30",
        "-09-30",
        "-12-31",
    }


def _is_usable_xbrl_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    path = normalized.split("?", 1)[0].split("#", 1)[0]
    return normalized.startswith(("https://", "http://")) and path.endswith(
        ".xml"
    )


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].split(":", 1)[-1]


def _parse_xbrl(
    *, record_id: str, expected_period: str, payload: bytes
) -> ShareholdingPeriod:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ShareholdingError(f"invalid XBRL for record {record_id}") from error

    schema_ref = root.find(".//{http://www.xbrl.org/2003/linkbase}schemaRef")
    href = schema_ref.get(f"{{{_XLINK}}}href", "") if schema_ref is not None else ""
    match = re.search(
        r"(?:^|/)(?:in-bse-shp|in-capmkt)-(\d{4}-\d{2}-\d{2})\.xsd"
        r"(?:[?#].*)?$",
        href,
    )
    if match is None:
        raise ShareholdingError(f"unknown shareholding schema for record {record_id}")
    schema_version = match.group(1)

    fact_period = expected_period
    instant_periods = {
        instant.text.strip()
        for instant in root.findall(f".//{{{_XBRLI}}}instant")
        if instant.text and instant.text.strip()
    }
    if expected_period not in instant_periods and _is_canonical_quarter_end(
        expected_period
    ):
        next_day = (
            date.fromisoformat(expected_period) + timedelta(days=1)
        ).isoformat()
        if next_day in instant_periods:
            fact_period = next_day

    contexts: dict[str, str] = {}
    for context in root.findall(f".//{{{_XBRLI}}}context"):
        context_id = context.get("id")
        instant = context.find(f".//{{{_XBRLI}}}instant")
        if (
            not context_id
            or instant is None
            or not instant.text
            or instant.text.strip() != fact_period
        ):
            continue
        category_members = [
            member
            for member in context.findall(f".//{{{_XBRLDI}}}explicitMember")
            if _local_name(member.get("dimension", ""))
            == "CategoryOfShareholdersAxis"
        ]
        if len(category_members) > 1:
            raise ShareholdingError(
                f"duplicate shareholder-axis member for record {record_id}"
            )
        if category_members and category_members[0].text:
            contexts[context_id] = _local_name(category_members[0].text.strip())

    shares: dict[str, Decimal] = {}
    promoter_encumbered_shares: int | None = None
    promoter_encumbered_pct_of_total: float | None = None
    promoter_encumbered_pct_of_promoter: float | None = None
    pledge_flags: list[bool] = []
    for fact in root:
        local_name = _local_name(fact.tag)
        member = contexts.get(fact.get("contextRef", ""))
        text = fact.text.strip() if fact.text else None

        if local_name in _PLEDGE_FLAG_FACTS:
            flag = _as_bool(text)
            if flag is not None:
                pledge_flags.append(flag)
            continue

        if local_name in _ENCUMBERED_SHARES_FACTS and fact.get("unitRef") == "shares":
            if member in _PROMOTER_MEMBERS and text:
                promoter_encumbered_shares = int(Decimal(text))
            continue

        if local_name in _ENCUMBERED_PCT_FACTS and fact.get("unitRef") == "pure":
            pct = _as_ratio_pct(text)
            if pct is None:
                continue
            if member in _PROMOTER_MEMBERS:
                promoter_encumbered_pct_of_promoter = pct
            elif member in _TOTAL_PATTERN_MEMBERS:
                promoter_encumbered_pct_of_total = pct
            continue

        if local_name != "NumberOfShares":
            continue
        if fact.get("unitRef") != "shares":
            continue
        bucket = _MEMBERS.get(member)
        if bucket and text:
            if bucket in shares:
                raise ShareholdingError(
                    f"duplicate {bucket} share fact for record {record_id}"
                )
            shares[bucket] = Decimal(text)

    required = {"public"}
    missing = required - shares.keys()
    if missing:
        raise ShareholdingError(
            f"incomplete shareholding XBRL for record {record_id}: {sorted(missing)}"
        )
    for bucket in ("fii", "dii", "government", "other_public"):
        shares.setdefault(bucket, Decimal(0))

    public_shares = shares["public"]
    promoter_shares = shares.get("promoter", Decimal(0))
    component_shares = sum(
        shares[bucket]
        for bucket in ("fii", "dii", "government", "other_public")
    )
    equity_shares = public_shares + promoter_shares
    if (
        public_shares <= 0
        or promoter_shares < 0
        or equity_shares <= 0
        or component_shares != public_shares
    ):
        raise ShareholdingError(f"shareholding totals do not reconcile for record {record_id}")

    def percentage(bucket: str) -> float:
        return round(float(shares.get(bucket, Decimal(0)) / equity_shares * 100), 4)

    if (
        promoter_encumbered_pct_of_total is None
        and promoter_encumbered_shares is not None
        and equity_shares > 0
    ):
        promoter_encumbered_pct_of_total = round(
            float(Decimal(promoter_encumbered_shares) / equity_shares * 100),
            4,
        )
    if (
        promoter_encumbered_pct_of_promoter is None
        and promoter_encumbered_shares is not None
        and promoter_shares > 0
    ):
        promoter_encumbered_pct_of_promoter = round(
            float(Decimal(promoter_encumbered_shares) / promoter_shares * 100),
            4,
        )

    pledge_disclosed = any(pledge_flags) or promoter_encumbered_shares not in (
        None,
        0,
    )

    return ShareholdingPeriod(
        record_id=record_id,
        period=expected_period,
        schema_version=schema_version,
        fii_pct=percentage("fii"),
        dii_pct=percentage("dii"),
        government_pct=percentage("government"),
        promoter_pct=percentage("promoter"),
        other_public_pct=percentage("other_public"),
        public_shares=int(public_shares),
        component_shares=int(component_shares),
        reconciled=True,
        checksum=hashlib.sha256(payload).hexdigest(),
        schema_ref=href,
        parser_version=PARSER_VERSION,
        validation_status=(
            "reconciled"
            if fact_period == expected_period
            else "reconciled_manifest_period_plus_1d"
        ),
        promoter_encumbered_shares=promoter_encumbered_shares,
        promoter_encumbered_pct_of_total=promoter_encumbered_pct_of_total,
        promoter_encumbered_pct_of_promoter=promoter_encumbered_pct_of_promoter,
        pledge_disclosed=pledge_disclosed,
    )
