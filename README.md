# loop-vs-graph-eng

Small side-by-side comparison of two ways to structure an LLM retry/verify agent, plus a real branching use case built on the graph style.

- `loop_agent.py` — plain `while` loop, state in local vars, control flow via `if`/`break`.
- `graph_agent.py` — explicit node graph, state in a dict, control flow via `(next_node, state)` return values.
- `nse_trade_graph.py` — real use case for the graph style: an NSE (Indian stock exchange) trade-proposal pipeline with four independent, differently-failing checks, runnable over a single symbol, an explicit list, or a whole index's constituents.
- `nse_data.py`, `fundamentals.py`, `ta_analysis.py`, `cache.py`, `bhavcopy.py` — data/analysis layers used by `nse_trade_graph.py` (see below).

Both toy agents implement the same task: answer a question, check the answer, retry up to `MAX_ITERS` times until the check passes.

See `NOTES.md` for the tradeoffs between the loop and graph styles.

## Setup

Requires Python 3.14 and [`uv`](https://github.com/astral-sh/uv). The locked `ta-lib`
package includes a compatible Linux wheel on this machine, so setup is:

```bash
uv sync
```

This creates `.venv` and installs the pinned dependencies (`anthropic`, `openai`, `nsemine`, `pandas`, `python-dotenv`, `ta-lib`).

On macOS, install the TA-Lib C library first if `uv sync` cannot find it:

```bash
brew install ta-lib
TA_LIBRARY_PATH="$(brew --prefix ta-lib)/lib" TA_INCLUDE_PATH="$(brew --prefix ta-lib)/include" uv sync
```

Copy `.env.example` to `.env` and adjust as needed — all config below is read from `.env` via `python-dotenv` (loaded in `llm.py`), so no manual `export` is required.

```bash
cp .env.example .env
```

## LLM backends

`llm.py` supports three backends, controlled by env vars (set in `.env`):

| Backend | How to enable | Notes |
|---|---|---|
| Stub (default) | nothing set | Hardcoded fake answers, no network calls. Good for testing control flow only. |
| Anthropic (Claude) | `USE_REAL_LLM=1` | Requires `ANTHROPIC_API_KEY`. Uses `claude-haiku-4-5`. |
| Local (Ollama or compatible) | `USE_LOCAL_LLM=1` | Uses an OpenAI-compatible local endpoint; this machine profile targets Ollama. |

If both `USE_REAL_LLM` and `USE_LOCAL_LLM` are set, local wins.

### Running the local backend

This Linux host already has Ollama and an RTX 3060 with 12 GB VRAM. Pull the configured
5.03 GB Q4_K_M model once:

```bash
ollama pull hf.co/alexsabaka/ODA-Fin-RL-8B-GGUF:Q4_K_M
```

Ollama serves its API at `http://localhost:11434` when its service is running. The
OpenAI-compatible URL used here is `http://localhost:11434/v1`.

Config (`.env`): `LOCAL_LLM_URL` (default `http://localhost:11434/v1`),
`LOCAL_LLM_MODEL` (default `hf.co/alexsabaka/ODA-Fin-RL-8B-GGUF:Q4_K_M`),
`LOCAL_LLM_MAX_TOKENS` (default `800`), `FUNDAMENTAL_LLM_MAX_TOKENS` (default
`384` for the compact structured assessment; it can be raised deliberately if a
replacement model needs more room), and `LOCAL_LLM_REASONING_EFFORT`
(default `none`). `LOCAL_LLM_NO_THINK_DIRECTIVE` defaults to `/no_think` for the
configured Qwen3 model; set it empty for models that do not support that control token.

Local `check` calls request a strict JSON-schema response (`GOOD` or `BAD`, plus a
short reason) at temperature zero, then normalize it to the graph's existing
one-line verdict contract. If a compatible server ignores the schema, one bounded
64-token repair call is attempted before the original response is returned.
Fundamental checks use a narrower typed assessment: `PASS` / `REVIEW` / `REJECT`,
a controlled reason code, a summary capped at 220 characters, up to three validated
evidence IDs, and an explicit missing-evidence list. The local adapter makes one
bounded 192-token repair attempt; every adapter fails closed if valid JSON is still
unavailable.

**Why ODA-Fin-RL-8B instead of Fin-R1:** only `node_fundamental` in the trade
pipeline uses an LLM.
[ODA-Fin-RL-8B](https://huggingface.co/OpenDataArena/ODA-Fin-RL-8B) is a March
2026 Qwen3-8B finance fine-tune trained with SFT and GRPO. In the same nine-benchmark
comparison, its [paper](https://arxiv.org/abs/2603.07223) reports a 74.6% average versus
61.4% for Fin-R1 across financial understanding, sentiment, and numerical reasoning,
including 83.4% on Financial PhraseBank and 80.4% on ConvFinQA. Those are
publisher-reported benchmark results, not evidence that model-generated trade verdicts
are safe; the deterministic technical and risk gates remain intentionally outside the
LLM.

Ollama enables Qwen3 thinking by default. This application only needs a short verdict,
so the local profile sets both `LOCAL_LLM_REASONING_EFFORT=none` and the Qwen3
`/no_think` prompt directive. `llm.py` also strips Qwen's `<think>`/`<answer>` wrappers
and extracts the first `GOOD`/`BAD` line for check calls. Set either control empty for
an OpenAI-compatible server or model that does not support it.

## Logging

Both `loop_agent.py` and `graph_agent.py` (and `nse_trade_graph.py`) log through `logging_config.setup_logging(name)` instead of `print` — timestamped, one logger per module (`loop`, `graph`, `nse`), with the noisy `httpx` request logger quieted to `WARNING`. A `WARNING` is emitted whenever a run hits `MAX_ITERS` without a passing verdict.

## Running the toy agents

```bash
# stub, no network
uv run loop_agent.py
uv run graph_agent.py

# against the local backend (server must already be running, see above; set USE_LOCAL_LLM=1 in .env)
uv run loop_agent.py
uv run graph_agent.py

# against Claude (set USE_REAL_LLM=1 and ANTHROPIC_API_KEY in .env)
uv run loop_agent.py
```

Each run prints the answer/verdict per iteration and the final result.

## NSE trade graph (`nse_trade_graph.py`)

A real branching use case for the graph style: propose (never execute) an NSE stock trade, gated behind four independent checks that each fail differently.

```
fetch -> technical -> [REJECT] -> abort (no retry -- see below)
              |
            [GOOD]
              v
          fundamental -> [REJECT] -> abort (model or deterministic)
              |          -> [REVIEW] -> flag_review
            [PASS]
              v
            risk -> [REJECT] -> abort (no retry — bad risk config or current price near circuit)
              |
            [GOOD]
              v
          sentiment -> [REVIEW] -> flag_review (manual review, no proposal)
              |
            [GOOD]
              v
           propose (proposal text only — no broker call, ever)
              |
              v
             log (appends a record to trade_log.jsonl)
```

- **technical** — computes real indicators (SMA20, SMA50, RSI14, MACD(12,26,9), ATR14, via `ta_analysis.compute_indicators`, **TA-Lib**-backed) independently for four timeframes — daily plus 30/15/5-minute (from `nse_data.get_market_snapshot`) — and scores them with `ta_analysis.score_technical`: **deterministic code, not an LLM call.** It wasn't always this way — the original design asked an LLM to interpret all four timeframes in one prompt. Live head-to-head testing (gemma4 and Fin-R1, 11+ deliberately-constructed cases including engineered-bearish setups) showed this specific threshold/comparison task isn't something either model applies reliably: gemma4 produced byte-identical verdicts regardless of RSI value (ignoring the stated rule outright); Fin-R1 engaged with the numbers in its reasoning text but never once flipped to `BAD` across the whole battery, and in one case asserted a false numeric comparison ("90.0 > 95.0") as fact. Not a model-quality problem — exact comparisons just don't belong in a free-text prompt, regardless of which LLM runs it.
  - **Versioned policy.** `technical_policies.json` owns timeframe weights, score scales, RSI bands, engagement thresholds, liquidity threshold and enabled indicator families. The original `technical-confluence-v1` remains replayable; live scans default to `technical-relative-participation-v2`, whose daily weight is at least the combined intraday weight. Each assessment persists the policy ID and configuration fingerprint.
  - **Less-correlated confirmation.** The revised policy adds 20-session strength relative to the benchmark named in the versioned policy and a liquidity/participation family. Participation can be positive only when actual delivery volume, total volume and price direction confirm accumulation; a rising delivery percentage alone is explicitly neutral.
  - **Same evaluator for replay.** `ta_analysis.replay_technical_policies` evaluates current and revised policies on the same `TechnicalObservations`, requires completed-bar provenance and rejects future-dated bars, retains only the earliest repeated scan per symbol/session, and deducts an explicit round-trip cost from every GOOD signal.

  Scoring isn't a plain point-sum of hardcoded pass/fail flags — it's graduated and confluence-gated:
  - **Invalid data cannot vote.** `ta_analysis.evaluate_technical` requires all four timeframes, at least 50 bars per timeframe, finite OHLC inputs, finite computed indicators, and positive ATR. An invalid primary frame returns a typed `invalid_data` assessment and aborts before scoring. Indicator arithmetic keeps full precision; rounding is presentation-only.
  - **Graduated, not binary.** SMA20/50 trend and MACD-histogram momentum are scored by *how far apart* the values are (SMA gap as a % of price, MACD histogram normalized by ATR), clipped to ±1 — a 0.1% SMA gap and a 3% SMA gap used to count identically as "bullish"; now they don't.
  - **Volatility-adaptive RSI bands.** The overbought/oversold cutoff isn't Wilder's fixed 70/30 (a 1978 convention, never empirically validated) — it widens or narrows with the daily ATR-as-%-of-price: 35/65 in low volatility, 30/70 in the classic range, 20/80 in high volatility. These specific cutoffs are a reasonable starting heuristic, not yet validated against enough of this app's own decisions; `evaluation.py` now measures them, but useful calibration needs a materially larger sample.
  - **Confluence across signal roles, not across timeframes.** Trend and momentum are each computed at four timeframes, but those readings are correlated because they derive from price. Each role is collapsed to one daily-heavy weighted score. A `GOOD` result requires both engaged trend and momentum to be positive; RSI is a neutral-or-negative extreme penalty, not a third bullish vote. This is rule-level confirmation, not statistical independence.

  `nse_data.get_market_snapshot` is the completed-candle seam. During market hours it excludes today's unfinished daily candle and the provider's latest intraday tail; for 15 minutes after 15:30 it keeps the same conservative policy while NSE's historical endpoint settles. The subsequent closed-session request uses a distinct key, fetches again, and then caches the completed session for 24 hours. Each trade-log record retains per-timeframe source, fetch timestamp, cache hit/miss, TTL, completion drops, and latest completed bar. A `REJECT` verdict aborts immediately, no retry — a retry inside the same cache phase would just re-score identical numbers. Deliberately not split into one node per timeframe — this project only splits nodes when the retry/remediation path genuinely differs, and all four timeframes feed one score either way.
- **fundamental** — fetched only for symbols that pass the technical gate, avoiding unnecessary NSE calls for every technical rejection. `fundamentals.get_fundamental_snapshot` pulls corporate announcements, corporate actions, yearwise returns, and peer comparison data via NSE's `NextApi`. NSE's shallow quote shareholding response is supplemented by Regulation 31 XBRL history warmed into Aerospike: FII, DII, government, promoter, and other-public percentages are derived from reconciled share counts across five consecutive quarters. QoQ and four-quarter changes plus trend labels are deterministic for all five categories. The compact prompt contains only typed facts with stable evidence IDs; delivery and raw table dumps are excluded. Source ages are calculated as of the scan date; peer data older than 200 days or shareholding data older than 160 days forces `REVIEW`. Model output uses `PASS` / `REVIEW` / `REJECT`, and cited IDs are checked against the supplied input before a result can proceed. A missing or expired live manifest is explicitly pending and enqueued; scans do not make an NSE manifest or XBRL request.
- **risk** — deterministic code, not an LLM call. `position_risk.size_position` places an initial stop at `ATR14 * NSE_ATR_STOP_MULTIPLE` below the estimated entry and a profit target at `NSE_REWARD_RISK_RATIO` times that stop distance above entry (default 2R), then caps shares by both `NSE_MAX_LOSS_PCT` of principal and `NSE_MAX_ALLOCATION_PCT` of principal. Zero-share plans, invalid ATR/input, non-positive stops, stops below the lower circuit, and a **current reconstructed entry price** within 2% of either circuit abort explicitly. An earlier same-day intraday circuit-band touch is retained as `circuit_context` but does not reject a price that has recovered; when no same-day bars are available the context is explicitly unavailable rather than borrowing the prior daily candle. Proposal text includes entry, stop, target, capital, planned maximum loss, and planned target profit.
- **sentiment** — deterministic volatility-aware entry gate, not an LLM call. It compares the absolute daily move with twice daily ATR as a percentage of entry, bounded to a 3%-10% review threshold. The old prompt supplied a sector name but no sector return, which invited unsupported claims such as “within sector range.”
- **propose** — never calls a broker. Only ever produces a proposal string for a human to review and act on manually.

Every terminal record carries a typed `disposition` (`PROPOSE`, `REVIEW`, or `REJECT`) plus `decision_reason.stage` and `decision_reason.code`. `REJECT` always maps to the non-actionable `aborted` status; `REVIEW` remains `flagged_for_review`. Digests render these fields directly, and the evaluation ledger groups reason codes without parsing verdict prose.

Known data quirks:
- `nsemine`'s `get_stock_live_quotes` returns `upper_circuit` and `lower_circuit` swapped (confirmed against the raw NSE `priceInfo.priceBand` field, which is always `"lower-upper"`). `node_risk` in `nse_trade_graph.py` corrects for this on read — don't trust those two field names at face value if you use `nsemine` elsewhere.
- `nsemine.live.get_index_constituents_live_snapshot`'s own docstring example calls its index parameter `index_name=`, but the real keyword is `index` — `nse_data.get_index_symbols` calls it correctly, just don't copy the docstring.
- The `getYearwiseData` fundamentals endpoint (only that one) needs the symbol suffixed with `EQN` (e.g. `HDFCBANKEQN`); every other fundamentals endpoint uses the bare symbol — see `fundamentals.py`.

### Index scan / batch mode

Besides one symbol or an explicit list, `nse_trade_graph.py` can resolve and scan an entire index's constituents via `nse_data.get_index_symbols` (wraps `nsemine.live.get_index_constituents_live_snapshot`). A per-symbol failure (e.g. one blocked/slow fetch) is caught and logged so it doesn't abort the rest of the batch. `cache.py` still backs short-lived market/fundamental snapshots with per-key JSON files. Immutable XBRL filings use Aerospike Community Edition instead: each record keeps compressed source XML, normalized facts, checksum, schema reference, and parser version.

### XBRL shareholding history

Start the persistent local Aerospike server and warm symbols outside NSE market hours:

```bash
docker compose up -d aerospike
uv run warm_shareholding.py FEDERALBNK
uv run warm_shareholding.py --index "NIFTY NEXT 50"
uv run warm_shareholding.py --queued
# Multiple --index arguments are deduplicated in one paced run.
uv run warm_shareholding.py --index "NIFTY NEXT 50" --index "NIFTY MIDCAP 50"
```

`shareholding.get_shareholding_history` is the network-free live-scan interface. It reads the latest five consecutive periods from Aerospike and returns `pending` if a filing is not warm or the short-lived manifest has expired; the durable stale index remains available for inspection but cannot pass the fundamental gate. `warm_shareholding.py` is the only path that downloads missing XBRL; it waits two seconds plus jitter after every request, backs off and stops after repeated/blocked NSE access, and can drain queued live misses. The latest valid revision wins for each period. Raw compressed XML and its checksum remain immutable; normalized facts can be reparsed from that source when the parser version changes. Invalid or unreconciled XML is not cached as successful.

### Delivery-percentage trend (`bhavcopy.py`)

`fundamentals`'s live quote only gives one day's delivery percentage (how much traded volume actually settled, vs same-day speculative churn) — not enough to distinguish a trend from a one-off blip. `bhavcopy.py` maintains a local SQLite DB (`BHAVCOPY_DB_PATH`, default `bhavcopy.db`, gitignored) of NSE's daily bhavcopy via `nsemine.archives.get_daily_bhavcopy_and_deliverables_data` — one call returns the **whole market** (~2,400 EQ symbols: OHLCV, VWAP, turnover, delivery volume/%) for a given day, so building history here costs one request per day, not one per symbol.

- `./backfill_bhavcopy.sh` — slowly ensures the latest 30 trading sessions are available. It waits two seconds between NSE requests and skips dates already stored, so an interrupted run is safe to resume. Before the evening publication cutoff it starts from yesterday instead of repeatedly requesting today's unavailable file. Override with a positional session count, `BHAVCOPY_REQUEST_DELAY_SECONDS`, or `BHAVCOPY_PUBLISH_HOUR_IST`.
- `uv run bhavcopy.py backfill [days]` — the underlying resumable backfill command (default `BHAVCOPY_BACKFILL_DAYS=30`), skipping weekends and any date NSE has nothing for (holidays) rather than tracking a holiday calendar separately.
- `uv run bhavcopy.py` — fetches and stores just today's bhavcopy; this is what `run_overnight_scan.sh` calls before each scan to keep the DB current.
- `bhavcopy.get_delivery_trend(symbol)` — requires the complete 5-session recent and 20-session baseline windows. It compares both delivery percentage and actual delivery volume, includes recent price direction, and labels only combinations such as `possible_accumulation`, `possible_distribution`, or an unconfirmed percentage rise. These are market-participation clues, not proof of buyer/seller direction. Incomplete history and missing values are returned as explicit data-quality states.

This DB also supplies the completed-session outcomes used by `evaluation.py`.

### Decision evaluation (`evaluation.py`)

Every completed graph run still appends JSONL, and now also inserts the same record into an immutable SQLite decision ledger (`EVALUATION_DB_PATH`, default `evaluation.db`, gitignored). Batch membership and per-symbol completion/failure events also append to `SCAN_RUN_LOG_PATH` (default `scan_runs.jsonl`) so requested symbols remain auditable when SQLite is unavailable. On the next scan, unfinished journal runs older than `SCAN_RUN_STALE_AFTER_SECONDS` (default six hours) are closed as `RecoveredIncompleteScan`; the timeout avoids interfering with active overlapping scans. The decision record includes the effective backend/model/token limit (local, Anthropic, or stub) and `NSE_POLICY_VERSION`, so later model, prompt, scoring and risk-policy changes remain distinguishable. Because calibration selects one candidate per symbol/date/policy, bump `NSE_POLICY_VERSION` when the model, prompt, scoring, or risk configuration changes. A ledger failure does not fail the scan: JSONL is the durable recovery path and decision records can be imported idempotently.

```bash
# one-time/resumable import of existing records
uv run evaluation.py import-jsonl

# compute every 1/5/10/20-session outcome that now has enough completed data
uv run evaluation.py update

# JSON calibration summary: selection counts, returns, stop/target rates, excursions and score bands
uv run evaluation.py report
```

Outcomes use only bhavcopy dates strictly after the decision date, so an intraday scan never sees the rest of its own session. Horizons follow the benchmark's exchange-session dates; a suspended/illiquid stock missing any required daily bar is marked incomplete rather than silently advancing its horizon. Only selected candidates with a complete validated risk plan (positive entry, stop below entry and positive integer shares) are graded; technical rejects remain visible in selection counts but are not return-calibrated. Entry is the recorded risk-plan reference price, not a claimed fill. A stop is filled at the next session's open if price gaps below it, otherwise at the stop; a target gap receives the better opening price. The configurable round-trip cost (30 bps by default) is then subtracted. `JUNIORBEES` is the default NIFTY NEXT 50 proxy, entered at the first aligned future session's open. Reports retain gross/net and horizon-close returns, benchmark-relative return, stop and target rates, held-period MFE/MAE, fixed technical-score bands, and model/backend cohorts.

These are paper/reference outcomes, not a production backtest. Bhavcopy OHLC is raw and unadjusted, and proposal price is not execution price. A daily candle touching both stop and target is conservatively classified as `both_hit_stop_first` because daily OHLC cannot establish intraday ordering; the report exposes that ambiguity rate. Benchmark, cost, price-basis and evaluator-version settings form a methodology identity, so rerunning a different methodology preserves rather than overwrites prior outcomes. Corporate-action adjustment, actual fill capture, reference outcomes for rejected candidates and walk-forward parameter selection remain necessary before using the statistics to tune thresholds.

### Config (`.env`)

`NSE_SYMBOL` (default `RELIANCE`, used only when no symbols are passed on the command line and `NSE_INDEX` is unset), `NSE_PRINCIPAL` (default `100000`), `NSE_MAX_ALLOCATION_PCT` (default `10`), `NSE_MAX_LOSS_PCT` (default `1`), `NSE_ATR_STOP_MULTIPLE` (default `2`), `NSE_REWARD_RISK_RATIO` (default `2`), `NSE_TECHNICAL_POLICY_ID` (default `technical-relative-participation-v2`; its benchmark is part of that policy's fingerprint), `NSE_POLICY_VERSION` (bump when model/technical/risk/prompt semantics change), `TRADE_LOG_PATH` (default `trade_log.jsonl`, gitignored), `SCAN_RUN_LOG_PATH` (default `scan_runs.jsonl`, gitignored), `SCAN_RUN_STALE_AFTER_SECONDS` (default `21600`), `EVALUATION_DB_PATH` (default `evaluation.db`, gitignored), `EVALUATION_BENCHMARK_SYMBOL` (default `JUNIORBEES`), `EVALUATION_ROUND_TRIP_COST_BPS` (default `30`, an explicit starting assumption), `NSE_INDEX` (e.g. `NIFTY 50` — if set and no symbols are passed on the command line, scans the index's constituents instead of falling back to `NSE_SYMBOL`), `NSE_SCAN_LIMIT` (optional, caps how many constituents are scanned), `NSE_SCAN_DELAY_SECONDS` (default `1`, pause between symbols in a batch/index scan), `NSE_CALL_DELAY_SECONDS` (default `0.3`, pause after each quote/historical-data call), `CACHE_DIR` (default `.cache`, gitignored), `FUNDAMENTALS_CACHE_TTL_HOURS` (default `24`), `FUNDAMENTALS_CALL_DELAY_SECONDS` (default `0.5`), `INTRADAY_CACHE_TTL_MINUTES` (default `5`), `NSE_MARKET_DATA_GRACE_MINUTES` (default `15`), `AEROSPIKE_HOST` / `AEROSPIKE_PORT` / `AEROSPIKE_NAMESPACE`, `NSE_XBRL_CALL_DELAY_SECONDS` (default `2`), `NSE_XBRL_JITTER_SECONDS` (default `0.5`), `NSE_XBRL_LOOKBACK_DAYS` (default `730`), `NSE_XBRL_MANIFEST_TTL_SECONDS` (default `21600`), `BHAVCOPY_DB_PATH` (default `bhavcopy.db`), `BHAVCOPY_BACKFILL_DAYS` (default `30`), `BHAVCOPY_REQUEST_DELAY_SECONDS` (default `2`), and `BHAVCOPY_PUBLISH_HOUR_IST` (default `18`). `NSE_RISK_PCT` remains a temporary fallback for the allocation cap.

### Running

```bash
# one symbol, falls back to NSE_SYMBOL from .env
uv run nse_trade_graph.py

# one or more symbols on the command line (space- or comma-separated), each run independently through the full graph
uv run nse_trade_graph.py RELIANCE
uv run nse_trade_graph.py RELIANCE ACE HDFCBANK
uv run nse_trade_graph.py RELIANCE,ACE,HDFCBANK

# scan an index's constituents (no symbols on the command line, NSE_INDEX set in .env)
NSE_INDEX="NIFTY 50" NSE_SCAN_LIMIT=5 uv run nse_trade_graph.py
```

Every run appends one JSON line to `trade_log.jsonl` and records the same immutable decision in `evaluation.db`: timestamp, symbol, principal, loss/allocation/ATR-stop policy, model configuration, typed technical assessment, full-precision indicators, risk plan, each node's verdict, final status (`proposed` / `flagged_for_review` / `aborted`), and proposal text. Inspect the fallback log with `jq`, e.g.:

```bash
jq -c '{symbol, status, technical_verdict}' trade_log.jsonl
```

Malformed lines (partial writes from a disk-full condition, concurrent appends) are skipped with a warning rather than crashing `digest.py`/`intraday_recheck.py` — but neither `trade_log.jsonl` nor `.cache/` are rotated or pruned. At `NIFTY TOTAL MKT` (750-symbol) overnight-scan scale this grows by roughly 1.5-2MB/night; archiving or trimming old entries is a real need before running this unattended for months, just not built yet.

## Automated email alerts

`notify.py`, `digest.py`, and `intraday_recheck.py` turn the trade graph into a cron-driven alert pipeline: scan the whole market overnight, email a full-detail digest once that scan completes, then keep an eye on that run's picks during the day.

- **`notify.py`** — generic email sending (`smtplib`, stdlib only), plus Slack (`urllib.request` + an incoming webhook, still stdlib only). Slack is additive, not a replacement for email. Both functions remain stubbed by default for direct callers and `digest.py`; `intraday_recheck.py` deliberately passes only enabled/configured channels to its transition ledger so enabling Slack later can still deliver the current transition. SMTP and Slack calls are bounded by configurable/default 10-second timeouts. `EMAIL_TO` is a comma-separated list — one address today, more later, no code change needed. `send_slack(text)` takes a single pre-formatted mrkdwn string (a webhook post has no separate subject line the way email does) and splits long text into multiple messages on record boundaries (`_slack_chunks`) since Slack folds long messages behind a "show more". `digest.py`'s `build_slack_digest`/`format_symbol_section_slack` build a shorter, mrkdwn-formatted version of the same digest (bold/status-emoji per symbol, the per-timeframe indicator breakdown dropped since it's too dense for a Slack line — full detail stays in the email). Telegram still isn't built — adding it means adding one function here, not touching `digest.py`/`intraday_recheck.py`.
- **`scan_label`** — every row `nse_trade_graph.py` appends to `trade_log.jsonl` is tagged with `NSE_SCAN_LABEL` (default `manual`). This is how `digest.py`/`intraday_recheck.py` find "this run's" records without relying on calendar-date matching, which would be fragile for an overnight scan spanning midnight.
- **`run_overnight_scan.sh`** — the actual cron target. Generates a run id (`overnight_YYYYMMDD_HHMM`), runs `nse_trade_graph.py` over `NSE_INDEX="NIFTY TOTAL MKT"` tagged with that id, then calls `digest.py` with the same id. (`nsemine` recognizes the index name `NIFTY TOTAL MKT` — 750 constituents; `"NIFTY TOTAL MARKET"` does not, it hits an internal `nsemine` bug and returns `None`.)
- **`digest.py <run_id>`** — reads `trade_log.jsonl` filtered to that `scan_label`, emails one full-detail section (all four timeframes' indicators, every node's verdict text, the proposal) per `proposed`/`flagged_for_review` symbol, plus a one-line count of everything scanned/aborted for context.
- **`intraday_recheck.py [run_id]`** — finds the most recent `overnight_*` label (or takes one explicitly), collects the symbols that were `proposed`/`flagged_for_review` in that run, and re-runs the full graph on just those (not the full 750 — confirmed too slow for a 15-30 min cadence). `alert_ledger.py` persists the last observed material decision and successful channels under `INTRADAY_ALERT_STATE_PATH`. Its fingerprint contains disposition, entry, stop, target, shares, and typed reason stage/code; timestamps and verdict prose are excluded. An unchanged actionable decision is sent once, while a changed plan or review reason creates a new alert even when status is unchanged. A transition through `aborted` and back to actionable also creates a new alert, and a failed channel retries without duplicating a channel that already succeeded. Disabled channels are not marked delivered, so enabling Slack later still sends the current actionable transition. On first use after upgrading a legacy status-only ledger, the current decision becomes the material baseline without replaying already delivered channels because the prior plan was never stored. A process lock prevents overlapping cron runs from racing; malformed state—including an invalid fingerprint—fails closed rather than replaying alerts. Delivery is at-least-once: the narrow crash window after a remote service accepts a message but before local success is persisted can still duplicate that channel.

Runs on a different machine or a different local model port with zero code changes — everything routes through the existing `LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` `.env` config already described above.

### IST timezone safety

NSE market hours are IST, but every timestamp in this codebase used to be a naive `datetime.now()` — silently using whichever timezone the *host machine's system clock* happened to be set to. Harmless on a box that's already IST, wrong by however many hours otherwise (confirmed live: `datetime.now()` under `TZ=UTC` showed 13:04 when the real IST time was 18:34 — a 5.5 hour gap). `nsemine` itself has the same issue internally (its `end_datetime` default is a naive datetime frozen at import time from its own `datetime.now()`).

Fixed via `market_time.py`: `now_ist()` / `now_ist_naive()` return the real IST time regardless of host timezone (`zoneinfo`, stdlib, no new dependency), and `is_market_hours()` checks 9:15-15:30 IST, Monday-Friday. Every log timestamp, cache-rollover date, and historical-data lookback window in `nse_trade_graph.py`/`nse_data.py`/`fundamentals.py`/`digest.py`/`intraday_recheck.py` goes through it. `run_overnight_scan.sh`'s run-id generation uses `TZ='Asia/Kolkata' date` explicitly for the same reason. `trade_log.jsonl` timestamps are now timezone-aware ISO strings (e.g. `...+05:30`) — unambiguous regardless of who's reading them or where.

`intraday_recheck.py` self-gates on `is_market_hours()` and exits immediately outside that window — this means the cron schedule itself doesn't need to get IST right; a simple "every 15-20 minutes, every day" entry is safe on any machine, any system timezone. Override the gate for manual testing with `NSE_SKIP_MARKET_HOURS_CHECK=1`.

### Config (`.env`)

`NSE_SCAN_LABEL` (default `manual`), `EMAIL_ENABLED` (default `0`, stub), `SMTP_HOST`, `SMTP_PORT` (default `587`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` (default `1`), `SMTP_TIMEOUT_SECONDS` (default `10`), `EMAIL_FROM`, `EMAIL_TO` (comma-separated), `SLACK_ENABLED` (default `0`, stub), `SLACK_WEBHOOK_URL` (create one at [api.slack.com/messaging/webhooks](https://api.slack.com/messaging/webhooks) — per-channel, no bot/app needed), `INTRADAY_ALERT_STATE_PATH` (default `.intraday_alert_state.json`, durable transition/channel dedupe), `NSE_SKIP_MARKET_HOURS_CHECK` (testing only, bypasses `intraday_recheck.py`'s market-hours gate).

### Cron setup

Add to `crontab -e` (adjust the path):

```cron
# Overnight full-market scan + digest, once after market close. cron itself
# interprets "22" in the host's own system timezone, not necessarily IST -- if this
# machine isn't set to IST, adjust the hour accordingly (or set CRON_TZ=Asia/Kolkata
# if your cron implementation supports it).
0 22 * * 1-5 /path/to/loop-vs-graph-eng/run_overnight_scan.sh >> /path/to/loop-vs-graph-eng/cron.log 2>&1

# Intraday recheck every 20 minutes, every day -- the script itself no-ops outside
# 9:15-15:30 IST via is_market_hours(), so this line doesn't need the host's system
# timezone to match IST at all.
*/20 * * * * cd /path/to/loop-vs-graph-eng && uv run intraday_recheck.py >> cron.log 2>&1
```

### Running manually

```bash
uv run digest.py <run_id>
uv run intraday_recheck.py [run_id]   # defaults to the most recent overnight_* label; skipped outside market hours unless NSE_SKIP_MARKET_HOURS_CHECK=1
```
