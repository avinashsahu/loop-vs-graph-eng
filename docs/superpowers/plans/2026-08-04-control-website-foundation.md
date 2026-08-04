# Control Website — Foundation + Job Control Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `webapp/` FastAPI + React SPA skeleton, wire it into `nse_app.sh` as a fourth managed process, and port the job-control module (Module 1 of the design spec) onto it — enable/disable, run-now, status/history, live at `http://127.0.0.1:8788/`.

**Architecture:** FastAPI backend (`webapp/backend/`) reuses `app_scheduler.py`'s existing functions (`job_status_summary`, `load_overrides`, `save_overrides`) as a library — no new scheduler logic. React+TypeScript SPA (`webapp/frontend/`) polls `/api/jobs` every 5s and posts to `/api/jobs/toggle` / `/api/jobs/run-now`. `dashboard_server.py` keeps running unchanged on port 8787; this is additive.

**Tech Stack:** FastAPI, uvicorn, Vite, React 18, TypeScript, Vitest + React Testing Library. Backend tests stay in the existing `unittest discover -s tests` suite.

## Global Constraints

- Localhost only: bind `127.0.0.1`. No authentication. Never bind `0.0.0.0`.
- Polling refresh only (no WebSocket) — matches the spec's phase-1 decision.
- The API process never calls `subprocess.run`/spawns any NSE-touching script directly. All control actions write to `.app_scheduler_overrides.json`; only the existing scheduler daemon executes jobs.
- `dashboard_server.py` and its port `8787` are untouched. New service runs on port `8788` (`APP_DASHBOARD_PORT` stays 8787; new var `APP_CONTROL_API_PORT` default `8788`).
- Python `>=3.14` (see `pyproject.toml`). Add `fastapi` and `uvicorn[standard]` as new dependencies via `uv add`.
- Backend tests: `unittest`, following the `patch.object(app_scheduler, "SOME_PATH", tmp_path)` isolation pattern already used in `tests/test_app_scheduler.py`.
- Frontend tests: Vitest + `@testing-library/react`.

---

### Task 1: FastAPI backend skeleton with a health endpoint

**Files:**
- Create: `webapp/__init__.py` (empty)
- Create: `webapp/backend/__init__.py` (empty)
- Create: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`
- Modify: `pyproject.toml` (via `uv add`)

**Interfaces:**
- Produces: `webapp.backend.main:app` — the FastAPI application instance every later task adds routes to.

- [ ] **Step 1: Add the new dependencies**

Run: `uv add fastapi "uvicorn[standard]"`

This updates `pyproject.toml` and `uv.lock`.

- [ ] **Step 2: Create the package files**

`webapp/__init__.py`:
```python
```
(empty file — makes `webapp` a package)

`webapp/backend/__init__.py`:
```python
```
(empty file — makes `webapp.backend` a package)

- [ ] **Step 3: Write the failing test**

`tests/test_control_api.py`:
```python
import unittest

from fastapi.testclient import TestClient

from webapp.backend.main import app


class HealthTests(unittest.TestCase):
    def test_health_endpoint_reports_ok(self):
        client = TestClient(app)
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'webapp.backend.main'`)

- [ ] **Step 5: Write the minimal implementation**

`webapp/backend/main.py`:
```python
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="NSE Stock Picker Control API")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock webapp/__init__.py webapp/backend/__init__.py webapp/backend/main.py tests/test_control_api.py
git commit -m "Add FastAPI control-api skeleton with a health endpoint"
```

---

### Task 2: Wire the control API into nse_app.sh

**Files:**
- Modify: `nse_app.sh`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `webapp.backend.main:app` (Task 1)
- Produces: `./nse_app.sh start|stop|restart|status|down` all manage a `control_api` process on `127.0.0.1:$APP_CONTROL_API_PORT` (default 8788), PID file `.control_api.pid`, log `control_api.log`.

- [ ] **Step 1: Add the PID/log constants**

In `nse_app.sh`, near the existing `DASHBOARD_PID_FILE`/`DASHBOARD_LOG` declarations, add:
```bash
CONTROL_API_PID_FILE=".control_api.pid"
CONTROL_API_LOG="${APP_CONTROL_API_LOG_PATH:-control_api.log}"
```

- [ ] **Step 2: Add a `control_api_url` helper next to `dashboard_url`**

```bash
control_api_url() {
    local port
    port="$(configured_value APP_CONTROL_API_PORT 8788)"
    printf 'http://127.0.0.1:%s/' "$port"
}
```

- [ ] **Step 3: Add a `start_control_api` function next to `start_dashboard_server`**

```bash
start_control_api() {
    if process_is_running "$CONTROL_API_PID_FILE" "webapp.backend.main:app"; then
        echo "Control API is already running (PID $(<"$CONTROL_API_PID_FILE"))."
        return
    fi
    unlink "$CONTROL_API_PID_FILE" 2>/dev/null || true
    local port
    port="$(configured_value APP_CONTROL_API_PORT 8788)"
    echo "Starting control API..."
    nohup setsid uv run uvicorn webapp.backend.main:app \
        --host 127.0.0.1 --port "$port" \
        >>"$CONTROL_API_LOG" 2>&1 </dev/null &
    local pid="$!"
    printf '%s\n' "$pid" >"$CONTROL_API_PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "error: control API exited during startup; inspect $CONTROL_API_LOG" >&2
        return 1
    fi
    echo "Control API: $(control_api_url)"
}
```

- [ ] **Step 4: Wire it into `start`, `restart`, `stop`, `down`, and `status`**

In the `case "$command" in` block, update:
```bash
    start)
        prepare_dependencies
        start_scheduler
        start_dashboard_server
        start_control_api
        ;;
    stop)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        ;;
    restart)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        prepare_dependencies
        start_scheduler
        start_dashboard_server
        start_control_api
        ;;
```
and in `down)`:
```bash
    down)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        docker compose down
        if [[ -f "$OLLAMA_PID_FILE" ]]; then
            stop_pid_group "$OLLAMA_PID_FILE" "managed Ollama" "ollama serve"
        fi
        ;;
```

In `show_status`, add after the dashboard status block:
```bash
    if process_is_running "$CONTROL_API_PID_FILE" "webapp.backend.main:app"; then
        echo "Control API: running (PID $(<"$CONTROL_API_PID_FILE")) at $(control_api_url)"
    else
        echo "Control API: stopped"
    fi
```

- [ ] **Step 5: Add the new env var to `.env.example`**

Add near `APP_DASHBOARD_PORT`:
```dotenv
# Localhost-only FastAPI control website (job control today; more modules later).
# Never expose this port beyond the local machine.
APP_CONTROL_API_PORT=8788
```

- [ ] **Step 6: Syntax-check and manually verify**

Run: `bash -n nse_app.sh`
Expected: no output (valid syntax)

Run: `./nse_app.sh start` then `curl -s http://127.0.0.1:8788/api/health`
Expected: `{"status":"ok"}`

Run: `./nse_app.sh status`
Expected: shows `Control API: running (PID ...) at http://127.0.0.1:8788/`

Run: `./nse_app.sh stop` then `curl -s http://127.0.0.1:8788/api/health`
Expected: connection refused (process stopped). Restart it afterward with `./nse_app.sh start` so later manual-verification steps have it running.

- [ ] **Step 7: Commit**

```bash
git add nse_app.sh .env.example
git commit -m "Wire the FastAPI control API into nse_app.sh lifecycle"
```

---

### Task 3: GET /api/jobs — job status endpoint

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Consumes: `app_scheduler.job_status_summary(records=None, *, now=None, overrides=None) -> list[dict]` (existing, unchanged)
- Produces: `GET /api/jobs -> 200, JSON list matching job_status_summary()'s shape` (`name`, `base_enabled`, `override`, `enabled`, `due_now`, `current_occurrence`, `force_run_requested`, `last_record`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_control_api.py`:
```python
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app_scheduler


class JobsEndpointTests(unittest.TestCase):
    def test_get_jobs_returns_job_status_summary(self):
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            overrides_path = Path(temporary) / "overrides.json"
            with (
                patch.object(app_scheduler, "STATE_PATH", state_path),
                patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path),
            ):
                client = TestClient(app)
                response = client.get("/api/jobs")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertIsInstance(body, list)
                names = {job["name"] for job in body}
                self.assertIn("bhavcopy", names)
                self.assertIn("intraday_recheck", names)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Add the endpoint**

In `webapp/backend/main.py`, add:
```python
import app_scheduler


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return app_scheduler.job_status_summary()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add GET /api/jobs endpoint to the control API"
```

---

### Task 4: POST /api/jobs/toggle and POST /api/jobs/run-now

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Consumes: `app_scheduler.load_overrides() -> dict`, `app_scheduler.save_overrides(overrides: dict) -> None`, `app_scheduler.configured_jobs() -> tuple[Job, ...]` (existing, unchanged)
- Produces:
  - `POST /api/jobs/toggle` body `{"job": str, "enabled": bool}` -> `204` on success, `404` if `job` is not a known job name
  - `POST /api/jobs/run-now` body `{"job": str}` -> `204` on success, `404` if `job` is not a known job name

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_control_api.py`:
```python
class JobControlActionTests(unittest.TestCase):
    def test_toggle_writes_an_enabled_override(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post(
                    "/api/jobs/toggle", json={"job": "cleanup", "enabled": False}
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(
                    app_scheduler.load_overrides()["enabled_overrides"]["cleanup"],
                    False,
                )

    def test_toggle_rejects_unknown_job(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post(
                    "/api/jobs/toggle", json={"job": "not_a_real_job", "enabled": True}
                )
                self.assertEqual(response.status_code, 404)

    def test_run_now_queues_a_force_run_request(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post("/api/jobs/run-now", json={"job": "cleanup"})
                self.assertEqual(response.status_code, 204)
                self.assertIn("cleanup", app_scheduler.load_overrides()["force_run"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404 for all three — routes don't exist)

- [ ] **Step 3: Add the endpoints**

In `webapp/backend/main.py`, add:
```python
from fastapi import HTTPException
from pydantic import BaseModel

from market_time import now_ist


class ToggleRequest(BaseModel):
    job: str
    enabled: bool


class RunNowRequest(BaseModel):
    job: str


def _known_job_names() -> set[str]:
    return {job.name for job in app_scheduler.configured_jobs()}


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add job toggle and run-now endpoints to the control API"
```

---

### Task 5: Scaffold the React + TypeScript frontend

**Files:**
- Create: `webapp/frontend/` (generated by Vite, then edited)
- Modify: `.gitignore`

**Interfaces:**
- Produces: `webapp/frontend/` — a working Vite+React+TS project with `npm run dev`, `npm run build`, `npm test` all functional. `vite.config.ts` proxies `/api` to `http://127.0.0.1:8788` for dev-time hot reload.

- [ ] **Step 1: Generate the Vite project**

Run (from repo root):
```bash
npm create vite@latest webapp/frontend -- --template react-ts
```

- [ ] **Step 2: Install dependencies plus the test toolchain**

Run:
```bash
cd webapp/frontend
npm install
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
cd ../..
```

- [ ] **Step 3: Configure the dev proxy and test runner**

Replace `webapp/frontend/vite.config.ts` with:
```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8788',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.ts',
  },
})
```

Create `webapp/frontend/src/setupTests.ts`:
```typescript
import '@testing-library/jest-dom'
```

Add a `test` script to `webapp/frontend/package.json`'s `"scripts"` block:
```json
"test": "vitest run"
```

- [ ] **Step 4: Write a placeholder smoke test to prove the toolchain works**

`webapp/frontend/src/App.test.tsx`:
```typescript
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import App from './App'

describe('App', () => {
  it('renders without crashing', () => {
    render(<App />)
    expect(document.body).toBeTruthy()
  })
})
```

- [ ] **Step 5: Run the test, the build, and verify both succeed**

Run: `cd webapp/frontend && npm test`
Expected: 1 test passed

Run: `cd webapp/frontend && npm run build`
Expected: builds successfully, produces `webapp/frontend/dist/`

- [ ] **Step 6: Ignore generated frontend artifacts**

Add to `.gitignore`:
```
webapp/frontend/node_modules/
webapp/frontend/dist/
```

- [ ] **Step 7: Commit**

```bash
git add webapp/frontend .gitignore
git commit -m "Scaffold the React+TypeScript frontend with Vitest"
```

---

### Task 6: Job control page

**Files:**
- Create: `webapp/frontend/src/JobsPage.tsx`
- Create: `webapp/frontend/src/JobsPage.test.tsx`
- Modify: `webapp/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/jobs`, `POST /api/jobs/toggle`, `POST /api/jobs/run-now` (Tasks 3-4)
- Produces: `JobsPage` React component — a table with one row per job, an Enable/Disable button, and a Run Now button, polling `/api/jobs` every 5000ms.

- [ ] **Step 1: Write the failing component test**

`webapp/frontend/src/JobsPage.test.tsx`:
```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import JobsPage from './JobsPage'

const sampleJobs = [
  {
    name: 'cleanup',
    base_enabled: true,
    override: null,
    enabled: true,
    due_now: false,
    current_occurrence: null,
    force_run_requested: false,
    last_record: { status: 'success', occurrence: '2026-08-03', finished_at: '2026-08-03T02:00:11+05:30', return_code: 0 },
  },
]

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url === '/api/jobs') {
        return new Response(JSON.stringify(sampleJobs), { status: 200 })
      }
      return new Response(null, { status: 204 })
    }),
  )
})

describe('JobsPage', () => {
  it('renders a row per job with its last status', async () => {
    render(<JobsPage />)
    await waitFor(() => expect(screen.getByText('cleanup')).toBeInTheDocument())
    expect(screen.getByText('success')).toBeInTheDocument()
  })

  it('posts a toggle request when Disable is clicked', async () => {
    render(<JobsPage />)
    await waitFor(() => expect(screen.getByText('cleanup')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /disable/i }))
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        '/api/jobs/toggle',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
  })
})
```

- [ ] **Step 2: Install the user-event testing helper**

Run: `cd webapp/frontend && npm install -D @testing-library/user-event`

- [ ] **Step 3: Run test to verify it fails**

Run: `cd webapp/frontend && npm test`
Expected: FAIL (`Cannot find module './JobsPage'`)

- [ ] **Step 4: Write the component**

`webapp/frontend/src/JobsPage.tsx`:
```typescript
import { useCallback, useEffect, useState } from 'react'

type JobRecord = {
  status: string
  occurrence: string
  finished_at: string | null
  return_code: number | null
} | null

type Job = {
  name: string
  base_enabled: boolean
  override: boolean | null
  enabled: boolean
  due_now: boolean
  current_occurrence: string | null
  force_run_requested: boolean
  last_record: JobRecord
}

const POLL_INTERVAL_MS = 5000

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])

  const refresh = useCallback(async () => {
    const response = await fetch('/api/jobs')
    if (response.ok) {
      setJobs(await response.json())
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const toggle = async (job: Job) => {
    await fetch('/api/jobs/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: job.name, enabled: !job.enabled }),
    })
    refresh()
  }

  const runNow = async (job: Job) => {
    await fetch('/api/jobs/run-now', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: job.name }),
    })
    refresh()
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Job</th>
          <th>Enabled</th>
          <th>Last status</th>
          <th>Occurrence</th>
          <th>Controls</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job) => (
          <tr key={job.name}>
            <td>{job.name}</td>
            <td>{job.enabled ? 'enabled' : 'disabled'}</td>
            <td>{job.last_record?.status ?? 'never run'}</td>
            <td>{job.last_record?.occurrence ?? job.current_occurrence ?? '-'}</td>
            <td>
              <button onClick={() => toggle(job)}>
                {job.enabled ? 'Disable' : 'Enable'}
              </button>{' '}
              <button onClick={() => runNow(job)} disabled={job.force_run_requested}>
                {job.force_run_requested ? 'Queued...' : 'Run now'}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
```

- [ ] **Step 5: Wire it into `App.tsx`**

Replace the contents of `webapp/frontend/src/App.tsx` with:
```typescript
import JobsPage from './JobsPage'

export default function App() {
  return (
    <main>
      <h1>NSE Stock Picker — Control</h1>
      <JobsPage />
    </main>
  )
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd webapp/frontend && npm test`
Expected: PASS (all tests, including the Task 5 `App.test.tsx` smoke test)

- [ ] **Step 7: Commit**

```bash
git add webapp/frontend/src
git commit -m "Add the job control page: status table, toggle, and run-now"
```

---

### Task 7: Serve the built frontend from the API and verify end-to-end

**Files:**
- Modify: `webapp/backend/main.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `GET /` (and any non-`/api` path) serves `webapp/frontend/dist/index.html`; `GET /assets/*` serves the built JS/CSS. In dev, this route is unused — Vite's dev server (port 5173) is used instead, proxying `/api` to 8788.

- [ ] **Step 1: Mount the built static files**

In `webapp/backend/main.py`, add at the end of the file (after all `/api/*` routes are defined, so they take precedence):
```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles

_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
```

- [ ] **Step 2: Rebuild the frontend so `dist/` exists**

Run: `cd webapp/frontend && npm run build`
Expected: `webapp/frontend/dist/index.html` exists

- [ ] **Step 3: Restart the control API and verify the full stack manually**

Run: `./nse_app.sh restart`

Run: `curl -s http://127.0.0.1:8788/api/jobs | head -c 200`
Expected: JSON array of jobs

Run: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/`
Expected: `200` (serves `index.html`)

Open `http://127.0.0.1:8788/` in a browser (or `curl -s http://127.0.0.1:8788/ | grep -o '<title>[^<]*'`) and confirm the page loads with the job table populated.

Click Disable on a low-stakes job (e.g. `cleanup`), confirm via `cat .app_scheduler_overrides.json` that the override was written, then click Enable again to restore it. Click Run now on `cleanup` and, within one poll interval, confirm `scheduler_history.jsonl` gained a new entry for it (same verification pattern used for `dashboard_server.py` in the prior session).

- [ ] **Step 4: Document the new service in README**

In `README.md`, in the "Scheduler dashboard" section (or immediately after it), add a subsection:

```markdown
### Control website (new)

`./nse_app.sh start` also launches a FastAPI + React control website at
`http://127.0.0.1:8788/` (`APP_CONTROL_API_PORT`). It currently covers job
control (the same actions as the dashboard above); more modules are planned.
Frontend development requires Node.js/npm (`webapp/frontend/`, built with
Vite) -- run `npm run dev` there for hot reload against a running control API,
or `npm run build` to produce the static files the API serves in normal use.
Like the dashboard, this has no authentication and must stay localhost-only.
```

- [ ] **Step 5: Run the full backend test suite one more time**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -20`
Expected: all tests pass, including `test_control_api` and the existing `test_app_scheduler`

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py README.md
git commit -m "Serve the built frontend from the control API; document the new service"
```

---

## Follow-up plans (not in this plan)

Each gets its own `docs/superpowers/plans/YYYY-MM-DD-<name>.md`, written just before starting it, per the design spec's phase-1 module list:

- **Scan trigger + result view** — requires designing the `ad_hoc_requests` queue extension to `app_scheduler.py` (new concept, not yet built) and reading real `evaluation.db`/`trade_log.jsonl` schemas.
- **Decision/evaluation browser** — requires reading `evaluation.db`'s actual schema (via `evaluation.py`) to write real queries.
- **Data-warm coverage view** — requires reading `shareholding.py`/`material_disclosures.py`/`governance_filings.py`'s actual Aerospike access patterns to write real read calls.
