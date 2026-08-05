# Control Website — Calibration/Report Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the calibration report (what `uv run evaluation.py report` prints) in the control website. Previously deferred from the decision-browser plan as "too complex"; verified against real code that assumption was wrong -- `EvaluationLedger.calibration_report()` already returns a fully-formed, JSON-serializable dict. The backend wrap is trivial; the real work is presenting deeply nested data sensibly.

**Architecture:** `GET /api/calibration-report` calls `evaluation.EvaluationLedger(EVALUATION_DB_PATH).calibration_report()` directly and returns it as-is -- no new aggregation logic, reusing the exact method the CLI already uses. Verified live against production `evaluation.db`: 2226 total decisions, 97 evaluable, 1 horizon, 1 methodology, 13 decision-graph cohorts -- a real, moderately-sized payload, not something that needs pagination.

**Tech Stack:** Same as prior plans (FastAPI, React+TS).

## Global Constraints

- Same constraints as prior plans: localhost-only, no auth, no live NSE calls.
- `calibration_report()`'s well-known top-level lists (`reason_codes`, `model_configs`, `policy_versions`, `status_counts`) get real tables. The deeply nested, shape-varying sections (`horizons`, `technical_score_bands`, `model_performance`, `decision_graph_performance`, `methodology_performance`, `methodology`) get formatted-JSON display, matching the existing pattern in `DecisionsPage.tsx`'s evidence view -- building bespoke tables for every nested shape is out of scope for this pass.
- `EvaluationLedger(path)` takes a `str`, not a `Path` -- verified in `evaluation.py:EvaluationLedger.__init__(self, database_path: str)`.
- **Mandatory** (per the last two modules' lesson): after backend changes, restart `control_api` and do one `curl` sanity check before calling a task done.
- Verification bar otherwise: automated tests only (backend `unittest`, frontend Vitest).

---

### Task 1: `GET /api/calibration-report`

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Produces: `GET /api/calibration-report` -> `200`, body is exactly `EvaluationLedger.calibration_report()`'s return shape (see `evaluation.py:924-1255` for the authoritative structure -- top-level keys `decisions`, `horizons`, `technical_score_bands`, `model_performance`, `decision_graph_performance`, `methodology_performance`, `methodology`). Returns `200` with `decisions.total == 0` and empty collections if `evaluation.db` doesn't exist yet (matches how the other endpoints treat a missing DB, e.g. `list_decisions`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_control_api.py`:
```python
class CalibrationReportTests(unittest.TestCase):
    def test_report_reflects_recorded_decisions(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            ledger = evaluation.EvaluationLedger(str(db_path))
            ledger.record_decision(
                {
                    "timestamp": "2026-08-01T09:00:00+05:30",
                    "scan_label": "overnight_1",
                    "symbol": "RELIANCE",
                    "status": "proposed",
                }
            )
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/calibration-report")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["decisions"]["total"], 1)
                self.assertEqual(body["decisions"]["status_counts"], {"proposed": 1})
                self.assertIn("methodology", body)

    def test_report_handles_missing_database(self):
        with TemporaryDirectory() as temporary:
            missing_db = Path(temporary) / "does-not-exist.db"
            with patch.object(main_module, "EVALUATION_DB_PATH", missing_db):
                client = TestClient(app)
                response = client.get("/api/calibration-report")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["decisions"]["total"], 0)
```

Add `import evaluation` to the top of `tests/test_control_api.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404 -- route doesn't exist)

- [ ] **Step 3: Add the endpoint**

In `webapp/backend/main.py`, add near the top:
```python
import evaluation
```

Then add the route (anywhere before the `_FRONTEND_DIST` mount):
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add GET /api/calibration-report endpoint"
```

---

### Task 2: Calibration report page

**Files:**
- Create: `webapp/frontend/src/CalibrationPage.tsx`
- Create: `webapp/frontend/src/CalibrationPage.test.tsx`
- Modify: `webapp/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/calibration-report` (Task 1)
- Produces: `CalibrationPage` -- summary stat line (total/evaluable/status counts as badges), tables for `reason_codes`/`model_configs`/`policy_versions`, and a formatted-JSON block for the remaining nested sections (`horizons`, `technical_score_bands`, `model_performance`, `decision_graph_performance`, `methodology_performance`, `methodology`).

- [ ] **Step 1: Write the failing test**

`webapp/frontend/src/CalibrationPage.test.tsx`:
```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import CalibrationPage from './CalibrationPage'

const reportResponse = {
  decisions: {
    total: 2226,
    status_counts: { proposed: 185, aborted: 1879, flagged_for_review: 161, failed: 1 },
    evaluable: 97,
    raw_evaluable: 100,
    repeated_evaluable: 3,
    canonical: [],
    reason_codes: [{ stage: 'technical', code: 'TECHNICAL_CONFLUENCE_FAILED', count: 900 }],
    model_configs: [{ backend: 'openai_compatible_local', name: 'phi4:14b-q4_K_M', max_tokens: 2048, count: 300 }],
    policy_versions: [{ version: 'technical-relative-participation-v2', count: 2226 }],
  },
  horizons: { '1': { count: 97 } },
  technical_score_bands: {},
  model_performance: [],
  decision_graph_performance: [],
  methodology_performance: [],
  methodology: { scope: 'selected_candidate_evaluation' },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(reportResponse), { status: 200 })),
  )
})

describe('CalibrationPage', () => {
  it('shows decision totals, status counts, and reason codes', async () => {
    render(<CalibrationPage />)
    await waitFor(() => expect(screen.getByText('2226')).toBeInTheDocument())
    expect(screen.getByText('97')).toBeInTheDocument()
    expect(screen.getByText('TECHNICAL_CONFLUENCE_FAILED')).toBeInTheDocument()
    expect(screen.getByText(/"scope": "selected_candidate_evaluation"/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webapp/frontend && npm test`
Expected: FAIL (`Cannot find module './CalibrationPage'`)

- [ ] **Step 3: Write the component**

`webapp/frontend/src/CalibrationPage.tsx`:
```typescript
import { useEffect, useState } from 'react'

type ReasonCode = { stage: string | null; code: string | null; count: number }
type ModelConfig = { backend: string | null; name: string | null; max_tokens: number | null; count: number }
type PolicyVersion = { version: string | null; count: number }

type Report = {
  decisions: {
    total: number
    status_counts: Record<string, number>
    evaluable: number
    raw_evaluable: number
    repeated_evaluable: number
    reason_codes: ReasonCode[]
    model_configs: ModelConfig[]
    policy_versions: PolicyVersion[]
  }
  horizons: unknown
  technical_score_bands: unknown
  model_performance: unknown
  decision_graph_performance: unknown
  methodology_performance: unknown
  methodology: unknown
}

export default function CalibrationPage() {
  const [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/calibration-report')
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then(setReport)
      .catch(() => setError('Could not load the calibration report. Is the control API running?'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <section>
        <h2>Calibration report</h2>
        <div className="status-line">
          <span className="spinner" />
          <span>Loading…</span>
        </div>
      </section>
    )
  }

  if (error || !report) {
    return (
      <section>
        <h2>Calibration report</h2>
        <p className="error-text">{error ?? 'No report available.'}</p>
      </section>
    )
  }

  const { decisions } = report
  const analytical = {
    horizons: report.horizons,
    technical_score_bands: report.technical_score_bands,
    model_performance: report.model_performance,
    decision_graph_performance: report.decision_graph_performance,
    methodology_performance: report.methodology_performance,
    methodology: report.methodology,
  }

  return (
    <section>
      <h2>Calibration report</h2>

      <p className="hint-text">
        {decisions.total} total decisions, {decisions.evaluable} evaluable
        (canonical signal with a validated risk plan and a completed outcome).
      </p>

      <div className="row-buttons">
        {Object.entries(decisions.status_counts).map(([status, count]) => (
          <span key={status} className="badge badge-neutral">
            {status}: {count}
          </span>
        ))}
      </div>

      {decisions.reason_codes.length > 0 && (
        <div className="table-scroll">
          <h2>Reason codes</h2>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Code</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.reason_codes.map((row) => (
                <tr key={`${row.stage}-${row.code}`}>
                  <td>{row.stage}</td>
                  <td>{row.code}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {decisions.model_configs.length > 0 && (
        <div className="table-scroll">
          <h2>Model configs</h2>
          <table>
            <thead>
              <tr>
                <th>Backend</th>
                <th>Model</th>
                <th>Max tokens</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.model_configs.map((row, index) => (
                <tr key={index}>
                  <td>{row.backend}</td>
                  <td>{row.name}</td>
                  <td>{row.max_tokens ?? '-'}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {decisions.policy_versions.length > 0 && (
        <div className="table-scroll">
          <h2>Policy versions</h2>
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {decisions.policy_versions.map((row) => (
                <tr key={row.version}>
                  <td>{row.version}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="evidence-detail">
        <p className="hint-text">
          Outcome performance, score bands, and methodology (nested/variable shape --
          shown as-is):
        </p>
        <pre>{JSON.stringify(analytical, null, 2)}</pre>
      </div>
    </section>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webapp/frontend && npm test`
Expected: PASS (all test files)

- [ ] **Step 5: Wire it into `App.tsx`**

```typescript
import CalibrationPage from './CalibrationPage'
import CoveragePage from './CoveragePage'
import DecisionsPage from './DecisionsPage'
import JobsPage from './JobsPage'
import ScanPage from './ScanPage'

function App() {
  return (
    <main>
      <header className="app-header">
        <h1>NSE Stock Picker — Control</h1>
      </header>
      <div className="panel">
        <ScanPage />
      </div>
      <div className="panel">
        <DecisionsPage />
      </div>
      <div className="panel">
        <CalibrationPage />
      </div>
      <div className="panel">
        <CoveragePage />
      </div>
      <div className="panel">
        <JobsPage />
      </div>
    </main>
  )
}

export default App
```

- [ ] **Step 6: Run tests to verify nothing else broke**

Run: `cd webapp/frontend && npm test`
Expected: PASS (all test files, including `App.test.tsx`)

- [ ] **Step 7: Commit**

```bash
git add webapp/frontend/src
git commit -m "Add the calibration report page"
```

---

### Task 3: Rebuild, restart+verify live, docs

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation + verification only)

- [ ] **Step 1: Rebuild the frontend**

Run: `cd webapp/frontend && npm run build`
Expected: builds successfully

- [ ] **Step 2: Run both full test suites**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all pass

Run: `cd webapp/frontend && npm test`
Expected: all pass

- [ ] **Step 3: Restart the control API and sanity-check the new route live (mandatory)**

Run: `./nse_app.sh restart`

Run: `curl -s http://127.0.0.1:8788/openapi.json | python3 -c "import json,sys; print('\n'.join(sorted(json.load(sys.stdin)['paths'].keys())))"`
Expected: includes `/api/calibration-report`

Run: `curl -s http://127.0.0.1:8788/api/calibration-report | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['decisions']['total'], d['decisions']['status_counts'])"`
Expected: real numbers matching the current production `evaluation.db` (verified during planning: total 2226)

- [ ] **Step 4: Update README**

In the "Control website" subsection, replace the sentence describing current coverage with:

```markdown
It covers job control, an ad-hoc scan trigger, a decisions browser, data-warm
coverage, and the calibration report (decision/outcome statistics, reason
codes, model and policy breakdowns) -- more modules are planned.
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Rebuild frontend, verify live, and document the calibration report page"
```

---

## Follow-up plans (not in this plan)

Remaining phase 2+ items, unchanged: alerts/digest history module, LLM eval corpus browser, index-based ad-hoc scans, retiring `dashboard_server.py`.
