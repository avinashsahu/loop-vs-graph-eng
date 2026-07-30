from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from time import perf_counter
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


class Disposition(StrEnum):
    PROPOSE = "PROPOSE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    FAILED = "FAILED"


class ScanPurpose(StrEnum):
    MANUAL = "manual"
    BATCH = "batch"
    INTRADAY = "intraday"


class ScanStatus(StrEnum):
    PROPOSED = "proposed"
    FLAGGED_FOR_REVIEW = "flagged_for_review"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class ScanRequest:
    symbol: str
    principal: float
    scan_label: str = "manual"
    purpose: ScanPurpose = ScanPurpose.MANUAL
    max_allocation_pct: float = 10.0
    max_loss_pct: float = 1.0
    atr_stop_multiple: float = 2.0
    reward_risk_ratio: float = 2.0
    run_id: str | None = None
    requested_at: datetime = field(
        default_factory=lambda: datetime.now(_IST)
    )

    def __post_init__(self):
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if self.principal <= 0:
            raise ValueError("principal must be positive")
        for name in (
            "max_allocation_pct",
            "max_loss_pct",
            "atr_stop_multiple",
            "reward_risk_ratio",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "scan_label", self.scan_label.strip() or "manual")
        object.__setattr__(self, "purpose", ScanPurpose(self.purpose))

    @property
    def request_id(self) -> str:
        payload = {
            "run_id": self.run_id,
            "scan_label": self.scan_label,
            "symbol": self.symbol,
            "purpose": self.purpose.value,
            "requested_at": self.requested_at.isoformat(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DecisionReason:
    stage: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "code": self.code}


@dataclass(frozen=True)
class PolicyIdentity:
    version: str
    technical_policy_id: str | None = None
    technical_policy_fingerprint: str | None = None
    fundamental_policy_version: str | None = None


@dataclass(frozen=True)
class ModelIdentity:
    backend: str
    name: str | None = None
    max_tokens: int | None = None
    fundamental_max_tokens: int | None = None


@dataclass(frozen=True)
class StageTiming:
    stage: str
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True)
class DecisionGraphNode:
    node_id: str
    kind: str
    status: str
    reason_codes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    elapsed_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "kind": self.kind,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "evidence": dict(self.evidence),
            "elapsed_ms": (
                round(self.elapsed_ms, 3) if self.elapsed_ms is not None else None
            ),
        }


@dataclass(frozen=True)
class DecisionGraphEdge:
    source: str
    target: str
    relation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
        }


@dataclass(frozen=True)
class DecisionGraph:
    nodes: tuple[DecisionGraphNode, ...]
    edges: tuple[DecisionGraphEdge, ...]
    version: str = "decision-graph-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class ScanFailure:
    stage: str
    error_type: str
    message: str
    durable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "error_type": self.error_type,
            "message": self.message,
            "durable": self.durable,
        }


@dataclass(frozen=True)
class PersistenceReceipt:
    decision_id: str | None
    durable: bool


@dataclass(frozen=True)
class ScanExecution:
    record: Mapping[str, Any]
    timings: tuple[StageTiming, ...] = ()


class ScanExecutionError(RuntimeError):
    def __init__(
        self,
        stage: str,
        cause: Exception,
        timings: tuple[StageTiming, ...] = (),
    ):
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause
        self.timings = timings


class ScanAdapter(Protocol):
    def execute(self, request: ScanRequest) -> ScanExecution: ...


class DecisionStore(Protocol):
    def persist(self, record: Mapping[str, Any]) -> PersistenceReceipt: ...


@dataclass(frozen=True)
class ScanResult:
    request_id: str
    symbol: str
    status: ScanStatus
    disposition: Disposition
    reason_codes: tuple[DecisionReason, ...]
    policy: PolicyIdentity
    model: ModelIdentity
    elapsed_ms: float
    timings: tuple[StageTiming, ...]
    decision_graph: DecisionGraph
    record: Mapping[str, Any]
    decision_id: str | None = None
    failure: ScanFailure | None = None


class ScanEngine:
    """One typed seam for production, batch, intraday, and fixture scans."""

    def __init__(
        self,
        adapter: ScanAdapter,
        store: DecisionStore,
        *,
        fallback_policy: PolicyIdentity | None = None,
        fallback_model: ModelIdentity | None = None,
    ):
        self._adapter = adapter
        self._store = store
        self._fallback_policy = fallback_policy or PolicyIdentity("unknown")
        self._fallback_model = fallback_model or ModelIdentity("unknown")

    def scan(self, request: ScanRequest) -> ScanResult:
        started = perf_counter()
        try:
            execution = self._adapter.execute(request)
        except ScanExecutionError as error:
            elapsed_ms = (perf_counter() - started) * 1000
            return self._failed_result(
                request,
                error.stage,
                error.cause,
                error.timings,
                elapsed_ms,
            )
        except Exception as error:
            elapsed_ms = (perf_counter() - started) * 1000
            return self._failed_result(
                request,
                "scan_engine",
                error,
                (),
                elapsed_ms,
            )

        elapsed_ms = (perf_counter() - started) * 1000
        record = self._complete_record(
            request,
            execution.record,
            execution.timings,
            elapsed_ms,
        )
        try:
            receipt = self._store.persist(record)
        except Exception as error:
            return self._persistence_failure(
                request,
                record,
                execution.timings,
                elapsed_ms,
                error,
            )
        return self._result_from_record(
            request,
            record,
            execution.timings,
            elapsed_ms,
            receipt,
        )

    def _complete_record(
        self,
        request: ScanRequest,
        raw_record: Mapping[str, Any],
        timings: tuple[StageTiming, ...],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        record = dict(raw_record)
        record.setdefault("timestamp", datetime.now(_IST).isoformat())
        record["scan_label"] = request.scan_label
        record["scan_purpose"] = request.purpose.value
        record["scan_request_id"] = request.request_id
        record["elapsed_ms"] = round(elapsed_ms, 3)
        record["stage_timings"] = [timing.to_dict() for timing in timings]
        graph = build_decision_graph(record, timings, request.purpose)
        record["decision_graph"] = graph.to_dict()
        return record

    def _failed_result(
        self,
        request: ScanRequest,
        stage: str,
        error: Exception,
        timings: tuple[StageTiming, ...],
        elapsed_ms: float,
    ) -> ScanResult:
        failure = ScanFailure(
            stage=stage,
            error_type=type(error).__name__,
            message=str(error)[:500],
            durable=True,
        )
        record = self._failure_record(request, failure, timings, elapsed_ms)
        try:
            receipt = self._store.persist(record)
        except Exception:
            receipt = PersistenceReceipt(decision_id=None, durable=False)
        failure = replace(failure, durable=receipt.durable)
        record["durable_failure"] = failure.to_dict()
        return self._result_from_record(
            request,
            record,
            timings,
            elapsed_ms,
            receipt,
            failure=failure,
        )

    def _persistence_failure(
        self,
        request: ScanRequest,
        record: Mapping[str, Any],
        timings: tuple[StageTiming, ...],
        elapsed_ms: float,
        error: Exception,
    ) -> ScanResult:
        failure = ScanFailure(
            stage="persistence",
            error_type=type(error).__name__,
            message=str(error)[:500],
            durable=False,
        )
        failed_record = {
            **record,
            "status": "failed",
            "disposition": Disposition.FAILED.value,
            "decision_reason": {
                "stage": "persistence",
                "code": "DECISION_PERSISTENCE_FAILED",
            },
            "durable_failure": failure.to_dict(),
        }
        graph = build_decision_graph(failed_record, timings, request.purpose)
        failed_record["decision_graph"] = graph.to_dict()
        return self._result_from_record(
            request,
            failed_record,
            timings,
            elapsed_ms,
            PersistenceReceipt(None, False),
            failure=failure,
        )

    def _failure_record(
        self,
        request: ScanRequest,
        failure: ScanFailure,
        timings: tuple[StageTiming, ...],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(_IST).isoformat(),
            "scan_label": request.scan_label,
            "scan_purpose": request.purpose.value,
            "scan_request_id": request.request_id,
            "symbol": request.symbol,
            "principal": request.principal,
            "status": "failed",
            "disposition": Disposition.FAILED.value,
            "decision_reason": {
                "stage": failure.stage,
                "code": "UNHANDLED_SCAN_FAILURE",
            },
            "policy_version": self._fallback_policy.version,
            "model_config": {
                "backend": self._fallback_model.backend,
                "name": self._fallback_model.name,
                "max_tokens": self._fallback_model.max_tokens,
                "fundamental_max_tokens": (
                    self._fallback_model.fundamental_max_tokens
                ),
            },
            "proposal": None,
            "elapsed_ms": round(elapsed_ms, 3),
            "stage_timings": [timing.to_dict() for timing in timings],
            "durable_failure": failure.to_dict(),
        }
        graph = build_decision_graph(record, timings, request.purpose)
        record["decision_graph"] = graph.to_dict()
        return record

    def _result_from_record(
        self,
        request: ScanRequest,
        record: Mapping[str, Any],
        timings: tuple[StageTiming, ...],
        elapsed_ms: float,
        receipt: PersistenceReceipt,
        *,
        failure: ScanFailure | None = None,
    ) -> ScanResult:
        graph = decision_graph_from_dict(record["decision_graph"])
        disposition = _disposition(record.get("disposition"))
        return ScanResult(
            request_id=request.request_id,
            symbol=request.symbol,
            status=_status(record.get("status")),
            disposition=disposition,
            reason_codes=_reason_codes(record, graph),
            policy=_policy_identity(record, self._fallback_policy),
            model=_model_identity(record, self._fallback_model),
            elapsed_ms=elapsed_ms,
            timings=timings,
            decision_graph=graph,
            record=record,
            decision_id=receipt.decision_id,
            failure=failure,
        )


def build_decision_graph(
    record: Mapping[str, Any],
    timings: tuple[StageTiming, ...],
    purpose: ScanPurpose,
) -> DecisionGraph:
    timing = {item.stage: item.elapsed_ms for item in timings}
    assessment = record.get("technical_assessment") or {}
    technical_evidence = assessment.get("evidence") or {}
    fundamental_evidence = record.get("fundamental_evidence") or {}
    coverage = fundamental_evidence.get("coverage") or {}
    fundamental = record.get("fundamental_assessment") or {}
    risk_plan = record.get("risk_plan") or {}
    disposition = _disposition(record.get("disposition"))
    final_reason = record.get("decision_reason") or {}
    technical_reasons = tuple(assessment.get("reason_codes") or ())
    fundamental_reason = fundamental.get("reason_code")

    technical_status = assessment.get("status") or (
        "skipped" if not record.get("technical_verdict") else "evaluated"
    )
    data_status = (
        "failed"
        if final_reason.get("stage") in {"market_data", "scan_engine"}
        else "ready"
        if technical_status != "invalid_data"
        else "invalid"
    )
    coverage_status = (
        "complete"
        if coverage.get("complete")
        else "missing"
        if fundamental_evidence
        else "skipped"
    )
    qualitative_status = (
        "interpreted"
        if fundamental.get("model_invoked")
        else "not_required"
        if fundamental
        else "skipped"
    )
    risk_status = (
        "accepted"
        if risk_plan and disposition == Disposition.PROPOSE
        else "evaluated"
        if record.get("risk_verdict")
        else "skipped"
    )
    alert_status = (
        "eligible"
        if purpose == ScanPurpose.INTRADAY
        and disposition in {Disposition.PROPOSE, Disposition.REVIEW}
        else "not_applicable"
    )
    outcome_status = (
        "eligible_pending_market_data"
        if disposition == Disposition.PROPOSE
        else "not_eligible"
    )

    nodes = (
        DecisionGraphNode(
            "data_validity",
            "data_validity",
            data_status,
            technical_reasons if technical_status == "invalid_data" else (),
            {"market_snapshot": bool(record.get("market_snapshot"))},
            timing.get("fetch"),
        ),
        DecisionGraphNode(
            "technical_families",
            "technical_family_evidence",
            str(technical_status),
            technical_reasons,
            {
                "families": technical_evidence.get("families") or {},
                "policy_id": assessment.get("policy_id"),
                "policy_fingerprint": assessment.get("policy_fingerprint"),
            },
            timing.get("technical"),
        ),
        DecisionGraphNode(
            "fundamental_coverage",
            "fundamental_coverage",
            coverage_status,
            ("INSUFFICIENT_EVIDENCE",)
            if coverage_status == "missing"
            else (),
            {
                "missing": coverage.get("missing") or [],
                "profile": fundamental.get("profile"),
                "policy_version": fundamental.get("policy_version"),
            },
            timing.get("fundamental"),
        ),
        DecisionGraphNode(
            "qualitative_evidence",
            "qualitative_evidence",
            qualitative_status,
            (fundamental_reason,) if fundamental_reason else (),
            {
                "model_invoked": bool(fundamental.get("model_invoked")),
                "evidence_ids": fundamental.get("evidence_ids") or [],
            },
            timing.get("fundamental"),
        ),
        DecisionGraphNode(
            "risk",
            "risk",
            risk_status,
            (
                (final_reason.get("code"),)
                if final_reason.get("stage") == "risk"
                else ()
            ),
            {
                key: risk_plan.get(key)
                for key in (
                    "entry_price",
                    "stop_price",
                    "target_price",
                    "shares",
                    "max_loss_at_stop",
                )
                if key in risk_plan
            },
            timing.get("risk"),
        ),
        DecisionGraphNode(
            "disposition",
            "disposition",
            disposition.value,
            (final_reason.get("code"),) if final_reason.get("code") else (),
            {"stage": final_reason.get("stage")},
            timing.get("disposition"),
        ),
        DecisionGraphNode(
            "alert",
            "alert",
            alert_status,
            evidence={"purpose": purpose.value},
        ),
        DecisionGraphNode(
            "outcome",
            "outcome",
            outcome_status,
            evidence={"methodology": "future_bhavcopy"},
        ),
    )
    edges = (
        DecisionGraphEdge("data_validity", "technical_families", "enables"),
        DecisionGraphEdge(
            "technical_families", "fundamental_coverage", "gates"
        ),
        DecisionGraphEdge(
            "fundamental_coverage", "qualitative_evidence", "enables"
        ),
        DecisionGraphEdge("technical_families", "risk", "informs"),
        DecisionGraphEdge("fundamental_coverage", "risk", "informs"),
        DecisionGraphEdge("qualitative_evidence", "risk", "informs"),
        DecisionGraphEdge("risk", "disposition", "determines"),
        DecisionGraphEdge("disposition", "alert", "controls"),
        DecisionGraphEdge("disposition", "outcome", "selects"),
        DecisionGraphEdge("technical_families", "outcome", "attributed_to"),
        DecisionGraphEdge("fundamental_coverage", "outcome", "attributed_to"),
        DecisionGraphEdge("qualitative_evidence", "outcome", "attributed_to"),
        DecisionGraphEdge("risk", "outcome", "attributed_to"),
    )
    return DecisionGraph(nodes=nodes, edges=edges)


def decision_graph_from_dict(payload: Mapping[str, Any]) -> DecisionGraph:
    return DecisionGraph(
        version=str(payload.get("version") or "decision-graph-v1"),
        nodes=tuple(
            DecisionGraphNode(
                node_id=str(node["id"]),
                kind=str(node["kind"]),
                status=str(node["status"]),
                reason_codes=tuple(node.get("reason_codes") or ()),
                evidence=node.get("evidence") or {},
                elapsed_ms=node.get("elapsed_ms"),
            )
            for node in payload.get("nodes") or ()
        ),
        edges=tuple(
            DecisionGraphEdge(
                source=str(edge["source"]),
                target=str(edge["target"]),
                relation=str(edge["relation"]),
            )
            for edge in payload.get("edges") or ()
        ),
    )


def _reason_codes(
    record: Mapping[str, Any],
    graph: DecisionGraph,
) -> tuple[DecisionReason, ...]:
    reasons = []
    seen_codes = set()
    final = record.get("decision_reason") or {}
    if final.get("code"):
        item = DecisionReason(
            str(final.get("stage") or "decision"),
            str(final["code"]),
        )
        reasons.append(item)
        seen_codes.add(item.code)
    for node in graph.nodes:
        for code in node.reason_codes:
            if code not in seen_codes:
                reasons.append(DecisionReason(node.kind, code))
                seen_codes.add(code)
    return tuple(reasons)


def _policy_identity(
    record: Mapping[str, Any],
    fallback: PolicyIdentity,
) -> PolicyIdentity:
    technical = record.get("technical_assessment") or {}
    fundamental = record.get("fundamental_assessment") or {}
    return PolicyIdentity(
        version=str(record.get("policy_version") or fallback.version),
        technical_policy_id=technical.get("policy_id")
        or fallback.technical_policy_id,
        technical_policy_fingerprint=technical.get("policy_fingerprint")
        or fallback.technical_policy_fingerprint,
        fundamental_policy_version=fundamental.get("policy_version")
        or fallback.fundamental_policy_version,
    )


def _model_identity(
    record: Mapping[str, Any],
    fallback: ModelIdentity,
) -> ModelIdentity:
    model = record.get("model_config") or {}
    return ModelIdentity(
        backend=str(model.get("backend") or fallback.backend),
        name=model.get("name") or fallback.name,
        max_tokens=model.get("max_tokens")
        if model.get("max_tokens") is not None
        else fallback.max_tokens,
        fundamental_max_tokens=model.get("fundamental_max_tokens")
        if model.get("fundamental_max_tokens") is not None
        else fallback.fundamental_max_tokens,
    )


def _disposition(value: object) -> Disposition:
    try:
        return Disposition(str(value))
    except ValueError:
        return Disposition.FAILED


def _status(value: object) -> ScanStatus:
    try:
        return ScanStatus(str(value))
    except ValueError:
        return ScanStatus.FAILED
