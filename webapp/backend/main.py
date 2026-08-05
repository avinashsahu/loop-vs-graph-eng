from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import app_scheduler
import evaluation
import shareholding as shareholding_module
from market_time import now_ist

app = FastAPI(title="NSE Stock Picker Control API")

EVALUATION_DB_PATH = Path(os.environ.get("EVALUATION_DB_PATH", "evaluation.db"))

_shareholding_store_instance = None


def _shareholding_store():
    global _shareholding_store_instance
    if _shareholding_store_instance is None:
        _shareholding_store_instance = shareholding_module.AerospikeFilingStore()
    return _shareholding_store_instance


def _epoch_to_iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class ToggleRequest(BaseModel):
    job: str
    enabled: bool


class RunNowRequest(BaseModel):
    job: str


class ScanRequest(BaseModel):
    symbols: list[str]


def _known_job_names() -> set[str]:
    return {job.name for job in app_scheduler.configured_jobs()}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return app_scheduler.job_status_summary()


@app.post("/api/jobs/toggle", status_code=204)
def toggle_job(payload: ToggleRequest) -> None:
    if payload.job not in _known_job_names():
        raise HTTPException(status_code=404, detail=f"unknown job {payload.job!r}")
    overrides = app_scheduler.load_overrides()
    overrides["enabled_overrides"][payload.job] = payload.enabled
    app_scheduler.save_overrides(overrides)


@app.post("/api/jobs/run-now", status_code=204)
def run_job_now(payload: RunNowRequest) -> None:
    if payload.job not in _known_job_names():
        raise HTTPException(status_code=404, detail=f"unknown job {payload.job!r}")
    overrides = app_scheduler.load_overrides()
    overrides["force_run"][payload.job] = now_ist().isoformat()
    app_scheduler.save_overrides(overrides)


def _read_decisions_for_scan_label(scan_label: str) -> list[dict]:
    if not EVALUATION_DB_PATH.exists():
        return []
    connection = sqlite3.connect(EVALUATION_DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM decisions WHERE scan_label = ? ORDER BY decision_timestamp",
            (scan_label,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


@app.post("/api/scans", status_code=201)
def create_scan(payload: ScanRequest) -> dict:
    symbols = [s.strip().upper() for s in payload.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols must not be empty")
    request_id = app_scheduler.submit_ad_hoc_scan(symbols)
    return {"request_id": request_id}


@app.get("/api/scans/{request_id}")
def get_scan(request_id: str) -> dict:
    result = app_scheduler.get_ad_hoc_result(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="unknown scan request")
    decisions = (
        _read_decisions_for_scan_label(result["scan_label"])
        if result["status"] == "done"
        else []
    )
    return {**result, "decisions": decisions}


DECISION_LIST_COLUMNS = (
    "decision_id, decision_timestamp, decision_date, scan_label, symbol, "
    "status, disposition, reason_stage, reason_code, entry_price, "
    "stop_price, target_price, shares, technical_score, technical_verdict, "
    "fundamental_verdict, risk_verdict, sentiment_verdict, model_backend, "
    "model_name, policy_version, risk_plan_valid, created_at"
)


@app.get("/api/decisions")
def list_decisions(
    symbol: str | None = None,
    disposition: str | None = None,
    scan_label: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    filters = []
    params: list[str] = []
    if symbol:
        filters.append("symbol = ?")
        params.append(symbol.strip().upper())
    if disposition:
        filters.append("disposition = ?")
        params.append(disposition.strip().upper())
    if scan_label:
        filters.append("scan_label = ?")
        params.append(scan_label.strip())
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    if not EVALUATION_DB_PATH.exists():
        return {"total": 0, "results": []}
    connection = sqlite3.connect(EVALUATION_DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        total = connection.execute(
            f"SELECT COUNT(*) FROM decisions {where_clause}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT {DECISION_LIST_COLUMNS} FROM decisions {where_clause}
            ORDER BY decision_timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    finally:
        connection.close()
    return {"total": total, "results": [dict(row) for row in rows]}


@app.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str) -> dict:
    if not EVALUATION_DB_PATH.exists():
        raise HTTPException(status_code=404, detail="unknown decision")
    connection = sqlite3.connect(EVALUATION_DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown decision")
    decision = dict(row)
    raw_json = decision.pop("raw_record_json", None)
    try:
        decision["evidence"] = json.loads(raw_json) if raw_json else None
    except ValueError:
        decision["evidence"] = None
    return decision


@app.get("/api/coverage/shareholding")
def shareholding_coverage(universe: str = "NIFTY TOTAL MKT") -> dict:
    try:
        store = _shareholding_store()
        members = store.list_universe(universe)
        queued = set(store.queued_symbols())
    except Exception as error:
        raise HTTPException(
            status_code=503, detail=f"Aerospike unavailable: {error}"
        ) from error
    results = [
        {
            "symbol": str(bins.get("symbol", "")),
            "active": bool(bins.get("active")),
            "last_status": bins.get("last_status"),
            "last_attempt": _epoch_to_iso(bins.get("last_attempt")),
            "completed_at": _epoch_to_iso(bins.get("completed_at")),
            "periods": bins.get("periods"),
            "queued": str(bins.get("symbol", "")) in queued,
        }
        for bins in sorted(members, key=lambda b: str(b.get("symbol", "")))
    ]
    return {"universe": universe, "total": len(results), "results": results}


CACHE_DIR = Path(os.environ.get("CACHE_DIR", ".cache"))

CACHE_COVERAGE_SYSTEMS = {
    "disclosures": (
        "material_disclosures_v1_",
        float(os.environ.get("MATERIAL_DISCLOSURE_CACHE_TTL_HOURS", "336")),
    ),
    "governance": (
        "governance_v1_",
        float(os.environ.get("GOVERNANCE_CACHE_TTL_HOURS", "168")),
    ),
    "document_research": (
        "document_research_v1_",
        float(os.environ.get("DOCUMENT_RESEARCH_CACHE_TTL_HOURS", "720")),
    ),
}


@app.get("/api/coverage/cache/{system}")
def cache_coverage(system: str) -> dict:
    if system not in CACHE_COVERAGE_SYSTEMS:
        raise HTTPException(status_code=404, detail=f"unknown system {system!r}")
    prefix, ttl_hours = CACHE_COVERAGE_SYSTEMS[system]
    results = []
    if CACHE_DIR.exists():
        for path in CACHE_DIR.glob(f"{prefix}*.json"):
            symbol = path.stem[len(prefix):]
            fetched_at = None
            try:
                fetched_at = json.loads(path.read_text()).get("fetched_at")
            except (OSError, ValueError):
                pass
            age_hours = (time.time() - fetched_at) / 3600 if fetched_at else None
            results.append(
                {
                    "symbol": symbol,
                    "fetched_at": _epoch_to_iso(
                        int(fetched_at) if fetched_at else None
                    ),
                    "age_hours": round(age_hours, 1) if age_hours is not None else None,
                    "fresh": age_hours is not None and age_hours < ttl_hours,
                }
            )
    results.sort(
        key=lambda row: row["age_hours"] if row["age_hours"] is not None else -1,
        reverse=True,
    )
    return {
        "system": system,
        "ttl_hours": ttl_hours,
        "total": len(results),
        "results": results,
    }


@app.get("/api/calibration-report")
def calibration_report() -> dict:
    if not EVALUATION_DB_PATH.exists():
        return {
            "decisions": {
                "total": 0,
                "status_counts": {},
                "evaluable": 0,
                "raw_evaluable": 0,
                "repeated_evaluable": 0,
                "canonical": [],
                "reason_codes": [],
                "model_configs": [],
                "policy_versions": [],
            },
            "horizons": {},
            "technical_score_bands": {},
            "model_performance": [],
            "decision_graph_performance": [],
            "methodology_performance": [],
            "methodology": {},
        }
    ledger = evaluation.EvaluationLedger(str(EVALUATION_DB_PATH))
    return ledger.calibration_report()


_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
