# Control website — design

Status: approved (pending final doc review)
Date: 2026-08-04

## Problem

The app currently has one control surface: `dashboard_server.py`, a
stdlib-only localhost HTTP server that shows the scheduler's job table and
lets you toggle/run-now a job (see `docs` in `README.md` under "Scheduler
dashboard"). That covers job control only. The app also has decision history
(`evaluation.db`), data-warm coverage (Aerospike shareholding/disclosure/
governance/document-research registries), and no way to trigger an ad-hoc
scan from a browser at all — those all require the CLI.

This spec covers a unified, "full fledged" dynamic website: a React SPA
talking to a FastAPI JSON backend, covering job control plus three new
modules, running alongside (not replacing) the existing dashboard for now.

## Decisions locked during brainstorming

- **Access model**: this machine only. Bind `127.0.0.1`, no auth — same
  posture as `dashboard_server.py` today. Not reachable from LAN or remote.
- **Frontend**: React + TypeScript, built with Vite.
- **Backend**: FastAPI (new dependency: `fastapi`, `uvicorn`).
- **Update model**: polling refresh (a few seconds), not WebSocket push. No
  live subprocess-output streaming in phase 1.
- **Relationship to `dashboard_server.py`**: additive. The new app runs on a
  separate port; `dashboard_server.py` keeps running unchanged. Retiring it
  is a future decision, not part of this phase.
- **Phase 1 module scope**: job control, scan trigger + result view,
  decision/evaluation browser, data-warm coverage view. Alerts/digest history
  and the LLM eval corpus browser are explicitly deferred to a later phase.

## Architecture

### Layout

New top-level directory `webapp/` — the first subdirectory in an otherwise
flat repo:

```
webapp/
  backend/    FastAPI app (Python)
  frontend/   Vite + React + TypeScript SPA
```

### Processes

`./nse_app.sh start` gains a third managed process, `control_api`
(`uvicorn`), with its own PID file (`.control_api.pid`) and log
(`control_api.log`), bound to `127.0.0.1:8788` (`dashboard_server.py` stays
on `8787`, unchanged). `nse_app.sh stop`/`restart`/`down`/`status` are
extended the same way they were for the dashboard server.

In normal use, `uvicorn` serves both the JSON API (`/api/*`) and the built
React static files (`webapp/frontend/dist/`) from one process on one port:
visiting `http://127.0.0.1:8788/` serves the SPA, which calls same-origin
`/api/*`. During frontend development, Vite's own dev server (port 5173)
proxies `/api` to 8788 for hot module reload — that's a dev-time tool only,
not something `nse_app.sh` runs or manages.

### Data access

- `evaluation.db` / `bhavcopy.db`: FastAPI reads these sqlite files directly
  (read-only queries). **Implementation-time check**: confirm both are
  opened in WAL mode so the API's readers never block the scheduler's
  writer; enable it if not already the case.
- Aerospike-backed data (shareholding, disclosures, governance, document
  research): reuse the existing library modules (`shareholding.py`,
  `material_disclosures.py`, `governance_filings.py`) for reads — same code
  paths the warmers already use, not reimplemented against the API layer.
- Scheduler state: reuse `app_scheduler.py` as a library
  (`job_status_summary()`, `load_overrides()`, `save_overrides()`) — the
  same functions `dashboard_server.py` already calls.

### The hard rule

The API process **never spawns an NSE-touching subprocess itself**. Every
action that talks to NSE (ad-hoc scan, force a warm run, toggle/run-now on a
scheduled job) is expressed as a write to `.app_scheduler_overrides.json`.
The existing scheduler daemon — unchanged, still the sole holder of
`.app_scheduler.lock` — drains that file on its next poll tick
(`APP_SCHEDULER_POLL_SECONDS`, default 30s) and executes the action through
its normal sequential loop. This preserves the invariant the whole app is
built around: NSE requests are never made from two processes at once.

Consequence: an ad-hoc scan submitted from the browser can wait up to one
poll interval before starting, and can queue behind a currently-running
scheduled job. This is expected, not a bug — surfaced to the user as
"queued" status in the UI.

## Components (phase 1 modules)

### 1. Job control

Thin port of `dashboard_server.py`'s functionality onto the new stack: job
table (enabled/status/history/notes), Enable/Disable toggle, Run Now button.
Backend endpoints wrap the existing `app_scheduler` functions directly — no
new scheduler logic required for this module.

### 2. Scan trigger + result view

A form to enter symbol(s) or an index. Submitting creates an **ad-hoc
request** (new concept — see below) rather than triggering one of the 10
fixed named jobs. The UI polls the request's status (`queued` → `running` →
`done`/`failed`) and, once done, displays the resulting decision
(disposition, entry/stop/target, reasoning) read from `evaluation.db` /
`trade_log.jsonl`.

**Scheduler extension required**: today's `force_run` mechanism (built for
the earlier job-control dashboard) only targets the 10 fixed jobs returned
by `configured_jobs()`. This module needs a new `ad_hoc_requests` list in
`.app_scheduler_overrides.json` — each entry `{id, kind, args,
requested_at}` — that `run_due_jobs()` also drains every tick, running
`nse_trade_graph.py SYMBOL` (or `NSE_INDEX=...`) as its command. Same
timeout/history/alerting machinery as named jobs; results are recorded
keyed by request `id` so the API can look up completion by id rather than
by job name + occurrence.

### 3. Decision/evaluation browser

- Paginated, filterable table over `evaluation.db` decisions (symbol, date,
  disposition, reason).
- Per-decision detail view showing the full stored evidence bundle.
- A calibration/report page rendering what `evaluation.py report` already
  computes, as structured JSON instead of printed text.

### 4. Data-warm coverage view

Per-symbol/universe status across shareholding, disclosures, governance, and
document-research warming: last-warmed date, staleness, queued/backfill
progress. Reads the Aerospike `shareholding_universe` / `shareholding_warm`
sets and the equivalent registries for the other three warmers.

## Data flow

```
Browser (React)
  -> GET /api/*        -> direct read: evaluation.db / bhavcopy.db / Aerospike
  -> POST /api/*        -> write to .app_scheduler_overrides.json
                            (enabled_overrides | force_run | ad_hoc_requests)
                                |
                                v
                     scheduler daemon (app_scheduler.py, unchanged)
                     drains overrides file every poll tick, executes
                     sequentially, writes:
                       - .app_scheduler_state.json / scheduler_history.jsonl
                       - evaluation.db / trade_log.jsonl (via the job's own script)
                                |
                                v
                     Browser's next poll reads the updated state
```

## Error handling

Partial-failure tolerant, consistent with how the rest of the app already
treats missing data (e.g. `get_delivery_trend` returns `"status":
"insufficient_history"` rather than raising). Each dashboard
widget/section fails independently — if Aerospike is unreachable, the
data-warm-coverage widget shows "unavailable" while the other three modules
keep working normally. An ad-hoc request that stays `queued` for more than
5x `APP_SCHEDULER_POLL_SECONDS` (150s by default) with no status change is
surfaced as "scheduler unreachable" rather than spinning indefinitely. Symbol input
is validated both client-side and server-side before being queued.

## Testing

- **Backend**: FastAPI `TestClient` tests in `tests/test_control_api.py`,
  kept in the existing `unittest discover -s tests` suite rather than
  introducing pytest just for this.
- **Frontend**: Vitest + React Testing Library for the key interactive views
  (job table toggle/run-now, scan trigger form, decision table
  filtering) — the standard pairing for a Vite+React project, no additional
  build tooling.
- **Out of scope for phase 1**: full browser e2e (Playwright) — deliberate
  cut, can be added once the UI stabilizes.
- **Manual verification**: curl/API smoke tests plus one real end-to-end
  scan-trigger run against a live symbol, the same bar used when
  `dashboard_server.py` shipped.

## Explicitly deferred (later phases)

- Alerts/digest history module (intraday alerts, digest sends).
- LLM eval corpus browser (`run_llm_eval.py` results).
- WebSocket/live log streaming for in-progress jobs.
- Retiring `dashboard_server.py`.
- Any access model beyond localhost-only (LAN, remote/VPN) — would need real
  auth and is out of scope until explicitly requested.

## Risks / open implementation-time checks

- Confirm `evaluation.db` / `bhavcopy.db` WAL mode for safe concurrent reads.
- `ad_hoc_requests` queue needs its own cleanup/expiry policy (e.g. a
  completed or abandoned request shouldn't accumulate forever in the
  overrides file) — to be defined in the implementation plan.
- `webapp/frontend` introduces `npm`/Node tooling into a previously
  pure-Python repo for the first time — CI/dev-setup docs will need a note
  in `README.md`'s Setup section.
