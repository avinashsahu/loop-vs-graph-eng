from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="NSE Stock Picker Control API")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
