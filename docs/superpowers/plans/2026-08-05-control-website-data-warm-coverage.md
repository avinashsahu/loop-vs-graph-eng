# Control Website — Data-Warm Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-symbol warm coverage for all 4 warmers (shareholding, disclosures, governance, document-research) in the control website. Module 4 of the design spec, last of the phase-1 modules.

**Architecture:** Two genuinely different backends, because the 4 warmers persist state differently -- verified by reading the actual code, not assumed:
- **Shareholding** is backed by Aerospike's `shareholding_universe` set (`shareholding.py:AerospikeFilingStore`), which persistently tracks every symbol ever seeded into an index: `active`, `last_status` (`pending`/`complete`/`incomplete`), `last_attempt`, `completed_at`, `periods`. This gives a real "vs full universe" completeness view.
- **Disclosures, governance, and document-research** are backed by a plain local file cache (`cache.py`, one JSON file per symbol under `.cache/`), with **no persisted universe/queue table at all**. `warm_disclosures.py` (and the other two) compute "due" by fetching live index membership from NSE on every run and checking each symbol's cache freshness -- there is nothing durable to query for "vs universe" without making a live NSE call, which the control API is not allowed to do. So coverage for these three is **cache freshness only**: which symbols have a cached entry and how old it is, not membership completeness.

**Tech Stack:** Same as prior plans (FastAPI, React+TS). One new method on `shareholding.AerospikeFilingStore`; the other three systems need no production-code changes, only a directory scan in the API layer.

## Global Constraints

- Same constraints as prior plans: localhost-only, no auth, no live NSE calls from the API process (this is precisely why the disclosures/governance/document-research view is cache-freshness-only, not membership-based).
- Aerospike may be unreachable (container down, network issue) -- the shareholding endpoint must return `503` with a clear message, not crash the whole page; the other three endpoints don't depend on Aerospike at all.
- Real cache key prefixes (verified in source): `material_disclosures_v1_{SYMBOL}`, `governance_v1_{SYMBOL}`, `document_research_v1_{SYMBOL}`, files at `{CACHE_DIR}/{key}.json` containing `{"fetched_at": epoch_seconds, "data": ...}`.
- Real TTL defaults (verified in source): disclosures 336h (`MATERIAL_DISCLOSURE_CACHE_TTL_HOURS`), governance 168h (`GOVERNANCE_CACHE_TTL_HOURS`), document-research 720h (`DOCUMENT_RESEARCH_CACHE_TTL_HOURS`).
- Real `shareholding_universe` bins (verified in `shareholding.py:AerospikeFilingStore.seed_universe`/`record_universe_attempt`): `universe, symbol, active (0/1), seeded_at, completed_at, last_attempt, last_status, periods, last_reason, last_error`.
- **Lesson from the last module**: after backend changes, restart `control_api` (`./nse_app.sh restart`) and do one quick `curl` sanity check before calling a task done -- the UI bug two turns ago was a stale server process, not a code bug. This is now mandatory, not optional, even though full live-poll verification stays skipped per session direction.
- Verification bar otherwise: automated tests only (backend `unittest`, frontend Vitest).

---

### Task 1: `AerospikeFilingStore.list_universe` + `GET /api/coverage/shareholding`

**Files:**
- Modify: `shareholding.py`
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Produces:
  - `shareholding.AerospikeFilingStore.list_universe(universe: str) -> list[dict]` -- all bins (active or not) for that universe, unfiltered by due-ness (unlike the existing `due_universe_symbols`).
  - `GET /api/coverage/shareholding?universe=NIFTY+TOTAL+MKT` -> `200 {"universe": str, "total": int, "results": [{"symbol": str, "active": bool, "last_status": str|None, "last_attempt": str|None, "completed_at": str|None, "periods": int|None, "queued": bool}]}`. `503` if Aerospike is unreachable.

- [ ] **Step 1: Add `list_universe` to `AerospikeFilingStore`**

In `shareholding.py`, add right after `due_universe_symbols`:
```python
    def list_universe(self, universe: str) -> list[dict]:
        """All known members of `universe`, active or not -- for coverage
        reporting, unlike due_universe_symbols which filters to due-only."""
        records = self._client.scan(
            self._namespace,
            self._universe_set,
        ).results()
        return [
            record_bins
            for _, _, record_bins in records
            if isinstance(record_bins, dict) and record_bins.get("universe") == universe
        ]
```

- [ ] **Step 2: Write the failing API test**

Add to `tests/test_control_api.py`:
```python
class ShareholdingCoverageTests(unittest.TestCase):
    def test_coverage_reports_universe_members(self):
        fake_store = Mock()
        fake_store.list_universe.return_value = [
            {
                "symbol": "RELIANCE",
                "active": 1,
                "last_status": "complete",
                "last_attempt": 1754270400,
                "completed_at": 1754270400,
                "periods": 5,
            },
            {
                "symbol": "TCS",
                "active": 1,
                "last_status": "pending",
                "last_attempt": 0,
                "completed_at": 0,
                "periods": 0,
            },
        ]
        fake_store.queued_symbols.return_value = ["TCS"]
        with patch.object(main_module, "_shareholding_store", return_value=fake_store):
            client = TestClient(app)
            response = client.get(
                "/api/coverage/shareholding", params={"universe": "NIFTY TOTAL MKT"}
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["total"], 2)
            by_symbol = {row["symbol"]: row for row in body["results"]}
            self.assertEqual(by_symbol["RELIANCE"]["last_status"], "complete")
            self.assertFalse(by_symbol["RELIANCE"]["queued"])
            self.assertTrue(by_symbol["TCS"]["queued"])
            self.assertIsNone(by_symbol["TCS"]["last_attempt"])

    def test_coverage_returns_503_when_aerospike_unreachable(self):
        with patch.object(
            main_module,
            "_shareholding_store",
            side_effect=RuntimeError("connection refused"),
        ):
            client = TestClient(app)
            response = client.get("/api/coverage/shareholding")
            self.assertEqual(response.status_code, 503)
```

Add `from unittest.mock import Mock, patch` -- extend the existing `from unittest.mock import patch` import to `from unittest.mock import Mock, patch`.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404 -- route doesn't exist; `AttributeError` for `main_module._shareholding_store` not existing)

- [ ] **Step 4: Add the endpoint**

In `webapp/backend/main.py`, add near the top (after the `EVALUATION_DB_PATH` line):
```python
import shareholding as shareholding_module

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
```

Add `from datetime import datetime, timezone` to the imports.

Then add the route (anywhere before the `_FRONTEND_DIST` mount):
```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests)

- [ ] **Step 6: Run the full backend suite to check for regressions**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add shareholding.py webapp/backend/main.py tests/test_control_api.py
git commit -m "Add shareholding coverage endpoint (Aerospike-backed universe view)"
```

---

### Task 2: Cache-freshness coverage for disclosures/governance/document-research

**Files:**
- Modify: `webapp/backend/main.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Produces: `GET /api/coverage/cache/{system}` where `system` is one of `disclosures`, `governance`, `document_research` -> `200 {"system": str, "ttl_hours": float, "total": int, "results": [{"symbol": str, "fetched_at": str|None, "age_hours": float|None, "fresh": bool}]}`, sorted by `age_hours` descending (stalest first -- the actionable ordering). `404` for an unknown system name.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_control_api.py`:
```python
class CacheCoverageTests(unittest.TestCase):
    def test_disclosures_coverage_lists_cached_symbols_by_staleness(self):
        with TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            now = time.time()
            (cache_dir / "material_disclosures_v1_RELIANCE.json").write_text(
                json.dumps({"fetched_at": now - 3600, "data": {}})
            )
            (cache_dir / "material_disclosures_v1_TCS.json").write_text(
                json.dumps({"fetched_at": now - 3600 * 400, "data": {}})
            )
            with patch.object(main_module, "CACHE_DIR", cache_dir):
                client = TestClient(app)
                response = client.get("/api/coverage/cache/disclosures")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["total"], 2)
                self.assertEqual(body["results"][0]["symbol"], "TCS")
                self.assertFalse(body["results"][0]["fresh"])
                self.assertTrue(body["results"][1]["fresh"])

    def test_coverage_cache_returns_404_for_unknown_system(self):
        client = TestClient(app)
        response = client.get("/api/coverage/cache/not_a_real_system")
        self.assertEqual(response.status_code, 404)

    def test_coverage_cache_handles_missing_directory(self):
        with TemporaryDirectory() as temporary:
            missing_dir = Path(temporary) / "does-not-exist"
            with patch.object(main_module, "CACHE_DIR", missing_dir):
                client = TestClient(app)
                response = client.get("/api/coverage/cache/governance")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total"], 0)
```

Add `import time` to the top of `tests/test_control_api.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: FAIL (404s for missing routes; the unknown-system test trivially passes -- confirm it fails for the right reason once the route exists, in the next step)

- [ ] **Step 3: Add the endpoint**

In `webapp/backend/main.py`, add:
```python
import time

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
    results.sort(key=lambda row: row["age_hours"] if row["age_hours"] is not None else -1, reverse=True)
    return {
        "system": system,
        "ttl_hours": ttl_hours,
        "total": len(results),
        "results": results,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_control_api -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `uv run python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py tests/test_control_api.py
git commit -m "Add cache-freshness coverage endpoint for disclosures/governance/document-research"
```

---

### Task 3: Coverage page

**Files:**
- Create: `webapp/frontend/src/CoveragePage.tsx`
- Create: `webapp/frontend/src/CoveragePage.test.tsx`
- Modify: `webapp/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/coverage/shareholding`, `GET /api/coverage/cache/{system}` (Tasks 1-2)
- Produces: `CoveragePage` -- a system selector (4 buttons/tabs: Shareholding, Disclosures, Governance, Document Research) and a table whose columns adapt to the selected system's shape (shareholding shows status/queued; the cache-based three show freshness/age).

- [ ] **Step 1: Write the failing test**

`webapp/frontend/src/CoveragePage.test.tsx`:
```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CoveragePage from './CoveragePage'

const shareholdingResponse = {
  universe: 'NIFTY TOTAL MKT',
  total: 1,
  results: [
    {
      symbol: 'RELIANCE',
      active: true,
      last_status: 'complete',
      last_attempt: '2026-08-01T00:00:00+00:00',
      completed_at: '2026-08-01T00:00:00+00:00',
      periods: 5,
      queued: false,
    },
  ],
}

const disclosuresResponse = {
  system: 'disclosures',
  ttl_hours: 336,
  total: 1,
  results: [
    { symbol: 'TCS', fetched_at: '2026-07-01T00:00:00+00:00', age_hours: 800, fresh: false },
  ],
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url
      if (url.startsWith('/api/coverage/shareholding')) {
        return new Response(JSON.stringify(shareholdingResponse), { status: 200 })
      }
      if (url.startsWith('/api/coverage/cache/disclosures')) {
        return new Response(JSON.stringify(disclosuresResponse), { status: 200 })
      }
      return new Response(null, { status: 404 })
    }),
  )
})

describe('CoveragePage', () => {
  it('shows shareholding coverage by default', async () => {
    render(<CoveragePage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    expect(screen.getByText('complete')).toBeInTheDocument()
  })

  it('switches to a cache-based system and shows its freshness view', async () => {
    render(<CoveragePage />)
    await waitFor(() => expect(screen.getByText('RELIANCE')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /disclosures/i }))
    await waitFor(() => expect(screen.getByText('TCS')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webapp/frontend && npm test`
Expected: FAIL (`Cannot find module './CoveragePage'`)

- [ ] **Step 3: Write the component**

`webapp/frontend/src/CoveragePage.tsx`:
```typescript
import { useEffect, useState } from 'react'

type ShareholdingRow = {
  symbol: string
  active: boolean
  last_status: string | null
  last_attempt: string | null
  completed_at: string | null
  periods: number | null
  queued: boolean
}

type CacheRow = {
  symbol: string
  fetched_at: string | null
  age_hours: number | null
  fresh: boolean
}

type System = 'shareholding' | 'disclosures' | 'governance' | 'document_research'

const SYSTEM_LABELS: Record<System, string> = {
  shareholding: 'Shareholding',
  disclosures: 'Disclosures',
  governance: 'Governance',
  document_research: 'Document Research',
}

function endpointFor(system: System): string {
  return system === 'shareholding'
    ? '/api/coverage/shareholding'
    : `/api/coverage/cache/${system}`
}

export default function CoveragePage() {
  const [system, setSystem] = useState<System>('shareholding')
  const [rows, setRows] = useState<(ShareholdingRow | CacheRow)[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(endpointFor(system))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((body) => setRows(body.results ?? []))
      .catch(() => setError('Could not load coverage. Is the control API running?'))
      .finally(() => setLoading(false))
  }, [system])

  return (
    <section>
      <h2>Data-warm coverage</h2>
      <div className="row-buttons">
        {(Object.keys(SYSTEM_LABELS) as System[]).map((key) => (
          <button
            key={key}
            className={key === system ? 'primary' : undefined}
            onClick={() => setSystem(key)}
          >
            {SYSTEM_LABELS[key]}
          </button>
        ))}
      </div>

      {error && <p className="error-text">{error}</p>}
      {!error && loading && (
        <div className="status-line">
          <span className="spinner" />
          <span>Loading…</span>
        </div>
      )}
      {!error && !loading && rows.length === 0 && (
        <p className="empty-text">No coverage data for {SYSTEM_LABELS[system]} yet.</p>
      )}

      {!error && !loading && rows.length > 0 && (
        <div className="table-scroll">
          {system === 'shareholding' ? (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Status</th>
                  <th>Completed</th>
                  <th>Periods</th>
                  <th>Queued</th>
                </tr>
              </thead>
              <tbody>
                {(rows as ShareholdingRow[]).map((row) => (
                  <tr key={row.symbol}>
                    <td>{row.symbol}</td>
                    <td>
                      <span
                        className={
                          row.last_status === 'complete'
                            ? 'badge badge-success'
                            : row.last_status === 'incomplete'
                              ? 'badge badge-warning'
                              : 'badge badge-neutral'
                        }
                      >
                        {row.last_status ?? 'pending'}
                      </span>
                    </td>
                    <td>{row.completed_at ?? '-'}</td>
                    <td>{row.periods ?? '-'}</td>
                    <td>{row.queued ? 'yes' : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Freshness</th>
                  <th>Age (hours)</th>
                  <th>Last fetched</th>
                </tr>
              </thead>
              <tbody>
                {(rows as CacheRow[]).map((row) => (
                  <tr key={row.symbol}>
                    <td>{row.symbol}</td>
                    <td>
                      <span className={row.fresh ? 'badge badge-success' : 'badge badge-warning'}>
                        {row.fresh ? 'fresh' : 'stale'}
                      </span>
                    </td>
                    <td>{row.age_hours ?? '-'}</td>
                    <td>{row.fetched_at ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
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
git commit -m "Add the data-warm coverage page"
```

---

### Task 4: Rebuild, restart, verify live, docs

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

- [ ] **Step 3: Restart the control API and sanity-check the new routes live**

This step is mandatory (see Global Constraints) -- the previous module shipped with the live server running stale code because this was skipped.

Run: `./nse_app.sh restart`

Run: `curl -s http://127.0.0.1:8788/openapi.json | python3 -c "import json,sys; print('\n'.join(sorted(json.load(sys.stdin)['paths'].keys())))"`
Expected: includes `/api/coverage/shareholding` and `/api/coverage/cache/{system}`

Run: `curl -s http://127.0.0.1:8788/api/coverage/cache/disclosures | head -c 200`
Expected: JSON response, not a 404/405

- [ ] **Step 4: Update README**

In the "Control website" subsection, replace the sentence describing current coverage with:

```markdown
It covers job control, an ad-hoc scan trigger, a decisions browser, and
data-warm coverage (shareholding completeness vs universe membership;
cache freshness for disclosures/governance/document-research, which have
no persisted universe table to compare against).
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Rebuild frontend, verify live, and document the coverage module"
```

---

## Follow-up plans (not in this plan)

Phase 1 of the design spec is complete after this plan (job control, scan trigger, decisions browser, data-warm coverage all shipped). Remaining work is explicitly phase 2+:
- Alerts/digest history module.
- LLM eval corpus browser.
- Calibration/report page.
- Index-based ad-hoc scans.
- Retiring `dashboard_server.py`.
