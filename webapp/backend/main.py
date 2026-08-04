from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import app_scheduler
from market_time import now_ist

app = FastAPI(title="NSE Stock Picker Control API")


class ToggleRequest(BaseModel):
    job: str
    enabled: bool


class RunNowRequest(BaseModel):
    job: str


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


_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
