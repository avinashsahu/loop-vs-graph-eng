# Control Website — Decision/Evaluation Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browse past scan decisions from `evaluation.db` in the control website — list with filters, a detail view per decision. Module 3 of the design spec.

**Architecture:** Direct read-only `sqlite3` queries against `evaluation.db`'s `decisions` table (schema in `evaluation.py:EvaluationLedger._initialize`), same pattern already used for `_read_decisions_for_scan_label` in the scan-trigger module. No writes, no scheduler interaction.

**Tech Stack:** Same as prior plans (FastAPI, React+TS, direct `sqlite3`).

## Global Constraints

- Same constraints as the foundation and scan-trigger plans: localhost-only, no auth, polling not needed here (this is read-only, on-demand data — no background job produces it).
- **v1 scope cut**: the calibration/report page (`EvaluationLedger.calibration_report()`) is deliberately excluded — it's a complex window-function query already served by `uv run evaluation.py report`. This plan is list + detail view only.
- Pagination: `limit` (default 50, max 200) + `offset`. Filters: `symbol`, `disposition`, `scan_label` — all optional, combined with AND.
- `decisions` table columns (verified against current `evaluation.py`): `decision_id, decision_timestamp, decision_date, scan_label, symbol, status, disposition, reason_stage, reason_code, entry_price, stop_price, target_price, shares, technical_score, technical_verdict, fundamental_verdict, risk_verdict, sentiment_verdict, model_backend, model_name, llm_max_tokens, fundamental_llm_max_tokens, policy_version, risk_plan_valid, raw_record_json, created_at`.
- Verification bar: automated tests only (backend `unittest`, frontend Vitest) — no live curl/poll cycles, per established session direction.

---

### Task 1: GET /api/decisions and GET /api/decisions/{decision_id}

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Produces:
  - `GET /api/decisions?symbol=&disposition=&scan_label=&limit=&offset=` -> `200 {"total": int, "results": list[dict]}`. Each result omits `raw_record_json` (list view stays light).
  - `GET /api/decisions/{decision_id}` -> `200 {...all columns..., "evidence": dict}` where `evidence` is `raw_record_json` parsed from JSON. `404` for an unknown id.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_control_api.py`:
```python
class DecisionsEndpointTests(unittest.TestCase):
    def _seed_db(self, db_path):
        connection = sqlite3.connect(db_path)
        connection.execute(
            """
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY, decision_timestamp TEXT,
                decision_date TEXT, scan_label TEXT, symbol TEXT, status TEXT,
                disposition TEXT, reason_stage TEXT, reason_code TEXT,
                entry_price REAL, stop_price REAL, target_price REAL,
                shares INTEGER, technical_score REAL, technical_verdict TEXT,
                fundamental_verdict TEXT, risk_verdict TEXT,
                sentiment_verdict TEXT, model_backend TEXT, model_name TEXT,
                llm_max_tokens INTEGER, fundamental_llm_max_tokens INTEGER,
                policy_version TEXT, risk_plan_valid INTEGER,
                raw_record_json TEXT, created_at TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO decisions (
                decision_id, decision_timestamp, decision_date, scan_label,
                symbol, status, disposition, raw_record_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("id1", "2026-08-01T09:00:00+05:30", "2026-08-01", "overnight_1",
                 "RELIANCE", "ok", "PROPOSE", '{"note": "first"}', "2026-08-01T09:00:01+05:30"),
                ("id2", "2026-08-02T09:00:00+05:30", "2026-08-02", "overnight_2",
                 "HDFCBANK", "ok", "REJECT", '{"note": "second"}', "2026-08-02T09:00:01+05:30"),
            ],
        )
        connection.commit()
        connection.close()

    def test_list_decisions_returns_paginated_results(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["total"], 2)
                self.assertEqual(len(body["results"]), 2)
                self.assertNotIn("raw_record_json", body["results"][0])

    def test_list_decisions_filters_by_symbol(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions", params={"symbol": "RELIANCE"})
                body = response.json()
                self.assertEqual(body["total"], 1)
                self.assertEqual(body["results"][0]["symbol"], "RELIANCE")

    def test_get_decision_returns_parsed_evidence(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions/id1")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["symbol"], "RELIANCE")
                self.assertEqual(body["evidence"], {"note": "first"})

    def test_get_decision_returns_404_for_unknown_id(self):
        with TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "evaluation.db"
            self._seed_db(db_path)
            with patch.object(main_module, "EVALUATION_DB_PATH", db_path):
                client = TestClient(app)
                response = client.get("/api/decisions/not-a-real-id")
                self.assertEqual(response.status_code, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404s — routes don't exist yet)

- [ ] **Step 3: Add the endpoints**

In `webapp/backend/main.py`, add (after `_read_decisions_for_scan_label`, before the scan endpoints or after — order among endpoints doesn't matter, only staying before the `_FRONTEND_DIST` mount does):
```python
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
```

Add `import json` near the top of `webapp/backend/main.py` alongside the existing `import os`/`import sqlite3`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add decision list and detail endpoints to the control API"
```

---

### Task 2: Decisions browser page

**Files:**
- Create: `webapp/frontend/src/DecisionsPage.tsx`
- Create: `webapp/frontend/src/DecisionsPage.test.tsx`
- Modify: `webapp/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/decisions`, `GET /api/decisions/{id}` (Task 1)
- Produces: `DecisionsPage` — a symbol filter input, a table of results, and an expandable detail view (fetches `/api/decisions/{id}` on row click, shows `evidence` as formatted JSON).

- [ ] **Step 1: Write the failing test**

`webapp/frontend/src/DecisionsPage.test.tsx`:
```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DecisionsPage from './DecisionsPage'

const listResponse = {
  total: 1,
  results: [
    {
      decision_id: 'id1',
      decision_timestamp: '2026-08-01T09:00:00+05:30',
      symbol: 'RELIANCE',
      disposition: 'PROPOSE',
      status: 'ok',
    },
  ],
}

const detailResponse = {
  decision_id: 'id1',
  symbol: 'RELIANCE',
  disposition: 'PROPOSE',
  evidence: { note: 'first' },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url.startsWith('/api/decisions/id1')) {
        return new Response(JSON.stringify(detailResponse), { status: 200 })
      }
      if (url.startsWith('/api/decisions')) {
        return new Response(JSON.stringify(listResponse), { status: 200 })
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('DecisionsPage', () => {
  it('lists decisions and shows evidence on row click', async () => {
    render(<DecisionsPage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    await userEvent.click(screen.getByText('RELIANCE'))
    await waitFor(() => expect(screen.getByText(/"note": "first"/)).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webapp/frontend && npm test`
Expected: FAIL (`Cannot find module './DecisionsPage'`)

- [ ] **Step 3: Write the component**

`webapp/frontend/src/DecisionsPage.tsx`:
```typescript
import { useEffect, useState } from 'react'

type DecisionSummary = {
  decision_id: string
  decision_timestamp: string
  symbol: string
  disposition: string | null
  status: string
}

type DecisionDetail = DecisionSummary & {
  evidence: unknown
}

export default function DecisionsPage() {
  const [symbolFilter, setSymbolFilter] = useState('')
  const [results, setResults] = useState<DecisionSummary[]>([])
  const [selected, setSelected] = useState<DecisionDetail | null>(null)

  useEffect(() => {
    const params = new URLSearchParams()
    if (symbolFilter.trim()) params.set('symbol', symbolFilter.trim())
    fetch(`/api/decisions?${params.toString()}`)
      .then((response) => (response.ok ? response.json() : { results: [] }))
      .then((body) => setResults(body.results ?? []))
  }, [symbolFilter])

  const openDetail = async (decisionId: string) => {
    const response = await fetch(`/api/decisions/${decisionId}`)
    if (response.ok) {
      setSelected(await response.json())
    }
  }

  return (
    <section>
      <h2>Decisions</h2>
      <input
        aria-label="Filter by symbol"
        placeholder="Filter by symbol"
        value={symbolFilter}
        onChange={(event) => setSymbolFilter(event.target.value)}
      />
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Disposition</th>
            <th>Timestamp</th>
          </tr>
        </thead>
        <tbody>
          {results.map((decision) => (
            <tr
              key={decision.decision_id}
              onClick={() => openDetail(decision.decision_id)}
              style={{ cursor: 'pointer' }}
            >
              <td>{decision.symbol}</td>
              <td>{decision.disposition ?? decision.status}</td>
              <td>{decision.decision_timestamp}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected && (
        <pre>{JSON.stringify(selected.evidence, null, 2)}</pre>
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
import DecisionsPage from './DecisionsPage'
import JobsPage from './JobsPage'
import ScanPage from './ScanPage'

function App() {
  return (
    <main>
      <h1>NSE Stock Picker — Control</h1>
      <ScanPage />
      <DecisionsPage />
      <JobsPage />
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
git commit -m "Add the decisions browser page"
```

---

### Task 3: Rebuild, full test suites, docs

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

In the "Control website" subsection, replace the sentence describing current coverage with:

```markdown
It covers job control, an ad-hoc scan trigger, and a decisions browser
(filter past scan decisions by symbol, inspect the full evidence bundle
per decision) -- more modules are planned.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Rebuild frontend and document the decisions browser module"
```

---

## Follow-up plans (not in this plan)

- **Data-warm coverage view** — Aerospike-backed staleness view.
- **Calibration/report page** — deliberately cut from this plan; `calibration_report()`'s window-function query needs its own careful design, and `evaluation.py report` already serves this need via CLI.
