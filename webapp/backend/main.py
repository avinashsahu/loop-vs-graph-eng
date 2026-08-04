from __future__ import annotations

from fastapi import FastAPI

import app_scheduler

app = FastAPI(title="NSE Stock Picker Control API")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return app_scheduler.job_status_summary()
