from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import app_scheduler
from market_time import now_ist

app = FastAPI(title="NSE Stock Picker Control API")

EVALUATION_DB_PATH = Path(os.environ.get("EVALUATION_DB_PATH", "evaluation.db"))


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


_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
