# Control Website — Scan Trigger + Result View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the control website trigger an ad-hoc scan for one or more symbols and show the resulting decision once it completes — Module 2 of the design spec.

**Architecture:** New `ad_hoc_requests` queue in `.app_scheduler_overrides.json`, drained by `app_scheduler.run_due_jobs()` on its normal tick (same subprocess/timeout/history machinery as named jobs, just not tied to a fixed occurrence schedule). Completed requests write a bounded `ad_hoc_results` entry back to overrides; the API reads that plus a direct read-only query against `evaluation.db`'s `decisions` table (keyed by `scan_label`) for the resulting decision.

**Tech Stack:** Same as the foundation plan (FastAPI, React+TS). Direct `sqlite3` reads against `evaluation.db` — no ORM, matching how the rest of the codebase reads this file.

## Global Constraints

- Same constraints as `docs/superpowers/plans/2026-08-04-control-website-foundation.md`: localhost-only, no auth, polling refresh, API never spawns NSE-touching subprocesses directly — only the scheduler daemon does, via the overrides file.
- `nse_trade_graph.py`'s CLI takes symbols as positional args (`nse_trade_graph.py RELIANCE HDFCBANK`, space- or comma-separated) and reads `NSE_SCAN_LABEL` from the environment (default `"manual"`) to tag the resulting `decisions` rows.
- `evaluation.db`'s `decisions` table (schema in `evaluation.py:EvaluationLedger._initialize`) has no index-scan support here — **v1 is symbol-only** (one or more explicit tickers), not index scans. Index-based ad-hoc scans are a deliberate cut, not an oversight — `NSE_SCAN_LIMIT`/`NSE_INDEX` handling can be added later if needed.
- Bounded retention for `ad_hoc_results`: keep the most recent 20 entries, evicting oldest first. This is the cleanup policy the foundation spec flagged as an open question.
- Per user direction: this plan skips the live curl/poll manual-verification cycle used in the foundation plan (cost-conscious) — automated tests (backend `unittest`, frontend Vitest) are the verification bar for every task.

---

### Task 1: `ad_hoc_requests` queue in app_scheduler.py

**Files:**
- Modify: `app_scheduler.py`
- Test: `tests/test_app_scheduler.py`

**Interfaces:**
- Produces:
  - `app_scheduler.submit_ad_hoc_scan(symbols: list[str], *, overrides: dict | None = None) -> str` — writes a pending request, returns its `request_id` (a `uuid4` hex string).
  - `app_scheduler.get_ad_hoc_result(request_id: str, *, overrides: dict | None = None) -> dict | None` — returns `{"status": "queued"|"running"|"done", "scan_label": str, "finished_at": str | None}` or `None` if the id is unknown.
  - `run_due_jobs()` also drains `overrides["ad_hoc_requests"]` each tick (in addition to its existing named-job loop), running each as `(sys.executable, "nse_trade_graph.py", *symbols)` with `NSE_SCAN_LABEL` set to `f"adhoc-{request_id}"` in the subprocess environment, subject to the same `JOB_TIMEOUT`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app_scheduler.py`:
```python
class AdHocScanTests(unittest.TestCase):
    def test_submit_creates_a_pending_request_and_get_result_reports_it(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                request_id = app_scheduler.submit_ad_hoc_scan(["RELIANCE"])
                result = app_scheduler.get_ad_hoc_result(request_id)
                self.assertEqual(result["status"], "queued")

    def test_get_result_returns_none_for_unknown_id(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                self.assertIsNone(app_scheduler.get_ad_hoc_result("not-a-real-id"))

    def test_run_due_jobs_executes_a_queued_ad_hoc_scan(self):
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            overrides_path = Path(temporary) / "overrides.json"
            history_path = Path(temporary) / "history.jsonl"
            with (
                patch.object(app_scheduler, "STATE_PATH", state_path),
                patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path),
                patch.object(app_scheduler, "HISTORY_PATH", history_path),
                patch.object(app_scheduler.subprocess, "run") as mock_run,
            ):
                mock_run.return_value.returncode = 0
                request_id = app_scheduler.submit_ad_hoc_scan(["RELIANCE"])
                app_scheduler.run_due_jobs({}, jobs=())
                result = app_scheduler.get_ad_hoc_result(request_id)
                self.assertEqual(result["status"], "done")
                self.assertEqual(result["scan_label"], f"adhoc-{request_id}")
                called_env = mock_run.call_args.kwargs["env"]
                self.assertEqual(called_env["NSE_SCAN_LABEL"], f"adhoc-{request_id}")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_app_scheduler -v`
Expected: FAIL (`AttributeError: module 'app_scheduler' has no attribute 'submit_ad_hoc_scan'`)

- [ ] **Step 3: Implement the queue**

In `app_scheduler.py`, add near `load_overrides`/`save_overrides`:
```python
import uuid

AD_HOC_RESULTS_LIMIT = 20


def submit_ad_hoc_scan(symbols: list[str], *, overrides: dict | None = None) -> str:
    overrides = overrides if overrides is not None else load_overrides()
    overrides.setdefault("ad_hoc_requests", {})
    overrides.setdefault("ad_hoc_results", {})
    request_id = uuid.uuid4().hex
    overrides["ad_hoc_requests"][request_id] = {
        "symbols": symbols,
        "requested_at": now_ist().isoformat(),
    }
    save_overrides(overrides)
    return request_id


def get_ad_hoc_result(request_id: str, *, overrides: dict | None = None) -> dict | None:
    overrides = overrides if overrides is not None else load_overrides()
    if request_id in overrides.get("ad_hoc_requests", {}):
        return {"status": "queued", "scan_label": f"adhoc-{request_id}", "finished_at": None}
    return overrides.get("ad_hoc_results", {}).get(request_id)


def _run_ad_hoc_requests(overrides: dict) -> None:
    pending = dict(overrides.get("ad_hoc_requests", {}))
    for request_id, request in pending.items():
        scan_label = f"adhoc-{request_id}"
        env = os.environ.copy()
        env["NSE_SCAN_LABEL"] = scan_label
        log.info("ad-hoc scan[%s] starting symbols=%s", request_id, request["symbols"])
        try:
            result = subprocess.run(
                (sys.executable, "nse_trade_graph.py", *request["symbols"]),
                cwd=REPO_ROOT,
                env=env,
                check=False,
                timeout=JOB_TIMEOUT.total_seconds(),
            )
            status = "done" if result.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            log.error("ad-hoc scan[%s] exceeded timeout; killed", request_id)
            status = "failed"
        except OSError:
            log.exception("ad-hoc scan[%s] could not start", request_id)
            status = "failed"

        overrides.setdefault("ad_hoc_results", {})
        overrides["ad_hoc_results"][request_id] = {
            "status": status,
            "scan_label": scan_label,
            "finished_at": now_ist().isoformat(),
        }
        results = overrides["ad_hoc_results"]
        if len(results) > AD_HOC_RESULTS_LIMIT:
            oldest_ids = sorted(
                results, key=lambda key: results[key]["finished_at"]
            )[: len(results) - AD_HOC_RESULTS_LIMIT]
            for stale_id in oldest_ids:
                del results[stale_id]
        del overrides["ad_hoc_requests"][request_id]
        save_overrides(overrides)
        _append_history(
            f"adhoc:{request_id}",
            JobRecord(
                occurrence=scan_label,
                status=status,
                attempted_at=request["requested_at"],
                finished_at=now_ist().isoformat(),
                return_code=0 if status == "done" else 1,
            ),
        )
```

Then call it from `run_due_jobs`, right after the `overrides = load_overrides()` line at the top of that function:
```python
    overrides = load_overrides()
    _run_ad_hoc_requests(overrides)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_app_scheduler -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add app_scheduler.py tests/test_app_scheduler.py
git commit -m "Add an ad-hoc scan request queue to the scheduler"
```

---

### Task 2: POST /api/scans and GET /api/scans/{id} endpoints

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Consumes: `app_scheduler.submit_ad_hoc_scan`, `app_scheduler.get_ad_hoc_result` (Task 1)
- Produces:
  - `POST /api/scans` body `{"symbols": list[str]}` -> `201 {"request_id": str}`. `400` if `symbols` is empty.
  - `GET /api/scans/{request_id}` -> `200 {"status": str, "scan_label": str, "finished_at": str | None, "decisions": list[dict]}`. `decisions` is `[]` until `status == "done"`, then populated from `evaluation.db`. `404` for an unknown id.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_control_api.py`:
```python
import sqlite3


class ScanTriggerTests(unittest.TestCase):
    def test_post_scans_rejects_empty_symbol_list(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.post("/api/scans", json={"symbols": []})
                self.assertEqual(response.status_code, 400)

    def test_post_scans_then_get_scans_reports_queued_status(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                post_response = client.post(
                    "/api/scans", json={"symbols": ["RELIANCE"]}
                )
                self.assertEqual(post_response.status_code, 201)
                request_id = post_response.json()["request_id"]

                get_response = client.get(f"/api/scans/{request_id}")
                self.assertEqual(get_response.status_code, 200)
                body = get_response.json()
                self.assertEqual(body["status"], "queued")
                self.assertEqual(body["decisions"], [])

    def test_get_scans_returns_404_for_unknown_id(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            with patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path):
                client = TestClient(app)
                response = client.get("/api/scans/not-a-real-id")
                self.assertEqual(response.status_code, 404)

    def test_get_scans_reads_decisions_once_done(self):
        with TemporaryDirectory() as temporary:
            overrides_path = Path(temporary) / "overrides.json"
            db_path = Path(temporary) / "evaluation.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE decisions (
                    decision_id TEXT PRIMARY KEY, decision_timestamp TEXT,
                    scan_label TEXT, symbol TEXT, status TEXT, disposition TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                ("id1", "2026-08-04T10:00:00+05:30", "adhoc-abc", "RELIANCE",
                 "ok", "PROPOSE"),
            )
            connection.commit()
            connection.close()
            with (
                patch.object(app_scheduler, "OVERRIDES_PATH", overrides_path),
                patch.object(main_module, "EVALUATION_DB_PATH", db_path),
            ):
                overrides = app_scheduler.load_overrides()
                overrides["ad_hoc_results"] = {
                    "abc": {
                        "status": "done",
                        "scan_label": "adhoc-abc",
                        "finished_at": "2026-08-04T10:00:05+05:30",
                    }
                }
                app_scheduler.save_overrides(overrides)

                client = TestClient(app)
                response = client.get("/api/scans/abc")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "done")
                self.assertEqual(len(body["decisions"]), 1)
                self.assertEqual(body["decisions"][0]["symbol"], "RELIANCE")
                self.assertEqual(body["decisions"][0]["disposition"], "PROPOSE")
```

Add the needed import near the top of `tests/test_control_api.py`:
```python
import webapp.backend.main as main_module
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404s for missing routes; `AttributeError` for `main_module.EVALUATION_DB_PATH` not existing yet)

- [ ] **Step 3: Add the endpoints**

In `webapp/backend/main.py`, add:
```python
import os
import sqlite3

EVALUATION_DB_PATH = Path(os.environ.get("EVALUATION_DB_PATH", "evaluation.db"))


class ScanRequest(BaseModel):
    symbols: list[str]


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add scan-trigger endpoints: POST /api/scans, GET /api/scans/{id}"
```

---

### Task 3: Scan trigger + result page

**Files:**
- Create: `webapp/frontend/src/ScanPage.tsx`
- Create: `webapp/frontend/src/ScanPage.test.tsx`
- Modify: `webapp/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `POST /api/scans`, `GET /api/scans/{id}` (Task 2)
- Produces: `ScanPage` — a form (space/comma-separated symbols) that submits a scan, then polls `GET /api/scans/{id}` every 3000ms until `status !== "queued"` and `status !== "running"`, rendering the resulting decisions.

- [ ] **Step 1: Write the failing test**

`webapp/frontend/src/ScanPage.test.tsx`:
```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ScanPage from './ScanPage'

beforeEach(() => {
  let getCallCount = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url
      if (url === '/api/scans' && init?.method === 'POST') {
        return new Response(JSON.stringify({ request_id: 'abc' }), { status: 201 })
      }
      if (url === '/api/scans/abc') {
        getCallCount += 1
        if (getCallCount === 1) {
          return new Response(
            JSON.stringify({ status: 'queued', scan_label: 'adhoc-abc', finished_at: null, decisions: [] }),
            { status: 200 },
          )
        }
        return new Response(
          JSON.stringify({
            status: 'done',
            scan_label: 'adhoc-abc',
            finished_at: '2026-08-04T10:00:05+05:30',
            decisions: [{ symbol: 'RELIANCE', disposition: 'PROPOSE', status: 'ok' }],
          }),
          { status: 200 },
        )
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('ScanPage', () => {
  it('submits symbols and polls until the decision is shown', async () => {
    render(<ScanPage pollIntervalMs={10} />)
    await userEvent.type(screen.getByLabelText(/symbols/i), 'RELIANCE')
    await userEvent.click(screen.getByRole('button', { name: /run scan/i }))
    await waitFor(() => expect(screen.getByText('PROPOSE')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webapp/frontend && npm test`
Expected: FAIL (`Cannot find module './ScanPage'`)

- [ ] **Step 3: Write the component**

`webapp/frontend/src/ScanPage.tsx`:
```typescript
import { useRef, useState } from 'react'

type Decision = {
  symbol: string
  disposition: string | null
  status: string
}

type ScanStatus = {
  status: string
  scan_label: string
  finished_at: string | null
  decisions: Decision[]
}

const DEFAULT_POLL_INTERVAL_MS = 3000

export default function ScanPage({
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  pollIntervalMs?: number
}) {
  const [symbolsInput, setSymbolsInput] = useState('')
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const pollHandle = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = () => {
    if (pollHandle.current !== null) {
      clearInterval(pollHandle.current)
      pollHandle.current = null
    }
  }

  const pollScan = (requestId: string) => {
    pollHandle.current = setInterval(async () => {
      const response = await fetch(`/api/scans/${requestId}`)
      if (!response.ok) return
      const body: ScanStatus = await response.json()
      setScan(body)
      if (body.status !== 'queued' && body.status !== 'running') {
        stopPolling()
      }
    }, pollIntervalMs)
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    stopPolling()
    const symbols = symbolsInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (symbols.length === 0) return
    const response = await fetch('/api/scans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbols }),
    })
    if (!response.ok) return
    const { request_id: requestId } = await response.json()
    setScan({ status: 'queued', scan_label: '', finished_at: null, decisions: [] })
    pollScan(requestId)
  }

  return (
    <section>
      <h2>Trigger a scan</h2>
      <form onSubmit={submit}>
        <label htmlFor="symbols">Symbols</label>
        <input
          id="symbols"
          value={symbolsInput}
          onChange={(event) => setSymbolsInput(event.target.value)}
          placeholder="RELIANCE HDFCBANK"
        />
        <button type="submit">Run scan</button>
      </form>
      {scan && (
        <div>
          <p>Status: {scan.status}</p>
          <ul>
            {scan.decisions.map((decision) => (
              <li key={decision.symbol}>
                {decision.symbol}: {decision.disposition ?? decision.status}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webapp/frontend && npm test`
Expected: PASS (all test files)

- [ ] **Step 5: Wire it into `App.tsx`**

```typescript
import JobsPage from './JobsPage'
import ScanPage from './ScanPage'

function App() {
  return (
    <main>
      <h1>NSE Stock Picker — Control</h1>
      <ScanPage />
      <JobsPage />
    </main>
  )
}

export default App
```

- [ ] **Step 6: Update `App.test.tsx`'s fetch stub to also handle `/api/scans`**

The existing stub in `webapp/frontend/src/App.test.tsx` returns `[]` for every call; `ScanPage` doesn't fetch on mount (only on submit), so no change is needed there — confirm by re-running:

Run: `cd webapp/frontend && npm test`
Expected: PASS (all test files, no new failures)

- [ ] **Step 7: Commit**

```bash
git add webapp/frontend/src
git commit -m "Add the scan trigger + result page"
```

---

### Task 4: Rebuild, full test suite, docs

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation + verification only)

- [ ] **Step 1: Rebuild the frontend**

Run: `cd webapp/frontend && npm run build`
Expected: builds successfully

- [ ] **Step 2: Run both test suites**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all pass

Run: `cd webapp/frontend && npm test`
Expected: all pass

- [ ] **Step 3: Update README**

In the "Control website" subsection added by the foundation plan, replace the sentence "It currently covers job control..." with:

```markdown
It covers job control and an ad-hoc scan trigger (enter one or more symbols,
see the resulting PROPOSE/REVIEW/REJECT decision once the scan completes) --
more modules are planned.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Rebuild frontend and document the scan trigger module"
```

---

## Follow-up plans (not in this plan)

- **Decision/evaluation browser** — full paginated/filterable view over `evaluation.db`, calibration report page.
- **Data-warm coverage view** — Aerospike-backed staleness view.
- **Index-based ad-hoc scans** — deliberately cut from this plan (v1 is symbol-only).
