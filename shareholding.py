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

from nsemine.bin.scraper import get_request

_XBRLI = "http://www.xbrl.org/2003/instance"
_XBRLDI = "http://xbrl.org/2006/xbrldi"
_XLINK = "http://www.w3.org/1999/xlink"
_MANIFEST_URL = "https://www.nseindia.com/api/corporate-share-holdings-master"
_IST = ZoneInfo("Asia/Kolkata")
PARSER_VERSION = "nse-shp-xbrl-v3"
_live_service = None
_warm_service = None
_MEMBERS = {
    "InstitutionsForeignMember": "fii",
    "InstitutionsDomesticMember": "dii",
    "GovernmentsMember": "government",
    "NonInstitutionsMember": "other_public",
    "PublicShareholdingMember": "public",
    "PromoterAndPromoterGroupMember": "promoter",
    "ShareholdingOfPromoterAndPromoterGroupMember": "promoter",
}


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
            if not row.get("recordId") or not row.get("xbrl") or not row.get("date"):
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
                "revised": row.get("revisedData") == "Y",
                "published_at": row.get("revisionDate")
                or row.get("systemDate")
                or row.get("submissionDate"),
            }
            filings.append(filing)
        return filings

    def download(self, filing: dict) -> bytes:
        return self._request(filing["url"]).content

    def _request(self, url: str, params: dict | None = None):
        for attempt in range(3):
            response = get_request(url, params=params)
            time.sleep(self._delay + random.uniform(0, self._jitter))
            if response is not None and response.status_code < 400:
                return response
            if response is not None and response.status_code in {401, 403, 429}:
                raise NseShareholdingRequestError(
                    f"NSE blocked shareholding request with HTTP {response.status_code}"
                )
            if attempt < 2:
                time.sleep(self._delay * (2**attempt))
        raise NseShareholdingRequestError(
            "NSE shareholding request failed repeatedly; warmer stopped"
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
            by_period.setdefault(filing["period"], []).append(filing)
        selected_periods = sorted(by_period, reverse=True)[:periods]
        normalized = []
        pending = []
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
            for filing in candidates:
                try:
                    normalized.append(self._load(filing))
                    break
                except ShareholdingError as error:
                    last_parse_error = error
            else:
                if last_parse_error is not None:
                    raise last_parse_error
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
        status = "ready" if self._download_missing or manifest_fresh else "pending"
        return _history(symbol, normalized, status=status)

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
    match = re.search(r"in-bse-shp-(\d{4}-\d{2}-\d{2})\.xsd", href)
    if match is None:
        raise ShareholdingError(f"unknown shareholding schema for record {record_id}")
    schema_version = match.group(1)

    contexts: dict[str, str] = {}
    for context in root.findall(f".//{{{_XBRLI}}}context"):
        context_id = context.get("id")
        instant = context.find(f".//{{{_XBRLI}}}instant")
        if (
            not context_id
            or instant is None
            or not instant.text
            or instant.text.strip() != expected_period
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
    for fact in root:
        if _local_name(fact.tag) != "NumberOfShares":
            continue
        if fact.get("unitRef") != "shares":
            continue
        member = contexts.get(fact.get("contextRef", ""))
        bucket = _MEMBERS.get(member)
        if bucket and fact.text:
            if bucket in shares:
                raise ShareholdingError(
                    f"duplicate {bucket} share fact for record {record_id}"
                )
            shares[bucket] = Decimal(fact.text.strip())

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
        validation_status="reconciled",
    )
