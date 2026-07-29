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
`LOCAL_LLM_MAX_TOKENS` (default `400`), and `LOCAL_LLM_REASONING_EFFORT`
(default `none`). `LOCAL_LLM_NO_THINK_DIRECTIVE` defaults to `/no_think` for the
configured Qwen3 model; set it empty for models that do not support that control token.

**Why ODA-Fin-RL-8B instead of Fin-R1:** only `node_fundamental` and
`node_sentiment` still use an LLM.
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
fetch -> technical -> [BAD] -> abort (no retry -- see below)
              |
            [GOOD]
              v
          fundamental -> [hard BAD: negative EPS/PAT] -> abort (no retry, deterministic)
              |          -> [soft BAD: LLM read] -> flag_review
            [GOOD]
              v
            risk -> [BAD] -> abort (no retry — bad risk config or price near circuit limit)
              |
            [GOOD]
              v
          sentiment -> [BAD] -> flag_review (manual review, no proposal)
              |
            [GOOD]
              v
           propose (proposal text only — no broker call, ever)
              |
              v
             log (appends a record to trade_log.jsonl)
```

- **technical** — computes real indicators (SMA20, SMA50, RSI14, MACD(12,26,9), ATR14, via `ta_analysis.compute_indicators`, **TA-Lib**-backed) independently for four timeframes — daily plus 30/15/5-minute (from `nse_data.get_multi_timeframe_history`) — and scores them with `ta_analysis.score_technical`: **deterministic code, not an LLM call.** It wasn't always this way — the original design asked an LLM to interpret all four timeframes in one prompt. Live head-to-head testing (gemma4 and Fin-R1, 11+ deliberately-constructed cases including engineered-bearish setups) showed this specific threshold/comparison task isn't something either model applies reliably: gemma4 produced byte-identical verdicts regardless of RSI value (ignoring the stated rule outright); Fin-R1 engaged with the numbers in its reasoning text but never once flipped to `BAD` across the whole battery, and in one case asserted a false numeric comparison ("90.0 > 95.0") as fact. Not a model-quality problem — exact comparisons just don't belong in a free-text prompt, regardless of which LLM runs it.

  Scoring isn't a plain point-sum of hardcoded pass/fail flags — it's graduated and confluence-gated:
  - **Invalid data cannot vote.** `ta_analysis.evaluate_technical` requires all four timeframes, at least 50 bars per timeframe, finite OHLC inputs, finite computed indicators, and positive ATR. An invalid primary frame returns a typed `invalid_data` assessment and aborts before scoring. Indicator arithmetic keeps full precision; rounding is presentation-only.
  - **Graduated, not binary.** SMA20/50 trend and MACD-histogram momentum are scored by *how far apart* the values are (SMA gap as a % of price, MACD histogram normalized by ATR), clipped to ±1 — a 0.1% SMA gap and a 3% SMA gap used to count identically as "bullish"; now they don't.
  - **Volatility-adaptive RSI bands.** The overbought/oversold cutoff isn't Wilder's fixed 70/30 (a 1978 convention, never empirically validated) — it widens or narrows with the daily ATR-as-%-of-price: 35/65 in low volatility, 30/70 in the classic range, 20/80 in high volatility. These specific cutoffs are a reasonable starting heuristic, not backtested against this app's own universe — real calibration needs a backtest harness over the growing `bhavcopy.db` history, which doesn't exist yet.
  - **Confluence across signal roles, not across timeframes.** Trend and momentum are each computed at four timeframes, but those readings are correlated because they derive from price. Each role is collapsed to one daily-heavy weighted score. A `GOOD` result requires both engaged trend and momentum to be positive; RSI is a neutral-or-negative extreme penalty, not a third bullish vote. This is rule-level confirmation, not statistical independence.

  A `BAD` verdict aborts immediately, no retry — history is cache-backed (5 min intraday TTL, 24h daily TTL), so a retry here would just re-score identical numbers. Deliberately not split into one node per timeframe — this project only splits nodes when the retry/remediation path genuinely differs, and all four timeframes feed one score either way.
- **fundamental** — fetched only for symbols that pass the technical gate, avoiding roughly seven unnecessary NSE calls for every technical rejection. `fundamentals.get_fundamental_snapshot` pulls corporate announcements, corporate actions, shareholding pattern, yearwise returns, and a peer comparison (all via raw NSE `NextApi` endpoints, see `fundamentals.py`), plus a delivery-percentage trend from `bhavcopy.get_delivery_trend` (see below). A deterministic hard check runs first: negative EPS or PAT aborts immediately, same philosophy as `risk`'s circuit-limit check — an objective number isn't the LLM's job to hallucinate over. Otherwise the LLM reads the qualitative parts for a `GOOD`/`BAD` verdict; a soft `BAD` here routes to `flag_review`.
- **risk** — deterministic code, not an LLM call. `position_risk.size_position` places an initial stop at `ATR14 * NSE_ATR_STOP_MULTIPLE` below the estimated entry, then caps shares by both `NSE_MAX_LOSS_PCT` of principal and `NSE_MAX_ALLOCATION_PCT` of principal. Zero-share plans, invalid ATR/input, non-positive stops, stops below the lower circuit, and prices near either circuit abort explicitly. Proposal text includes entry, stop, capital required, and planned maximum loss.
- **sentiment** — LLM checks whether today's price move looks like a reasonable entry (not a crash or a spike), using the live quote's `changepct` and sector. Include the company's full name, not just the ticker, in the prompt — a bare ticker (e.g. `ACE`) can be genuinely ambiguous to the model and has been observed to send gemma4 into a repetitive non-terminating reasoning loop.
- **propose** — never calls a broker. Only ever produces a proposal string for a human to review and act on manually.

Known data quirks:
- `nsemine`'s `get_stock_live_quotes` returns `upper_circuit` and `lower_circuit` swapped (confirmed against the raw NSE `priceInfo.priceBand` field, which is always `"lower-upper"`). `node_risk` in `nse_trade_graph.py` corrects for this on read — don't trust those two field names at face value if you use `nsemine` elsewhere.
- `nsemine.live.get_index_constituents_live_snapshot`'s own docstring example calls its index parameter `index_name=`, but the real keyword is `index` — `nse_data.get_index_symbols` calls it correctly, just don't copy the docstring.
- The `getYearwiseData` fundamentals endpoint (only that one) needs the symbol suffixed with `EQN` (e.g. `HDFCBANKEQN`); every other fundamentals endpoint uses the bare symbol — see `fundamentals.py`.

### Index scan / batch mode

Besides one symbol or an explicit list, `nse_trade_graph.py` can resolve and scan an entire index's constituents via `nse_data.get_index_symbols` (wraps `nsemine.live.get_index_constituents_live_snapshot`). A per-symbol failure (e.g. one blocked/slow fetch) is caught and logged so it doesn't abort the rest of the batch. `cache.py` backs both the multi-timeframe history and fundamentals fetches with a per-key JSON file under `CACHE_DIR` — this matters at batch scale: a full-index scan is ~13 HTTP calls per symbol (quote + 4 historical timeframes + several fundamentals endpoints), and repeated same-symbol fetches (e.g. `intraday_recheck.py` rechecking the same picks through the day) would otherwise re-hit NSE for same-day data that hasn't changed.

### Delivery-percentage trend (`bhavcopy.py`)

`fundamentals`'s live quote only gives one day's delivery percentage (how much traded volume actually settled, vs same-day speculative churn) — not enough to distinguish a trend from a one-off blip. `bhavcopy.py` maintains a local SQLite DB (`BHAVCOPY_DB_PATH`, default `bhavcopy.db`, gitignored) of NSE's daily bhavcopy via `nsemine.archives.get_daily_bhavcopy_and_deliverables_data` — one call returns the **whole market** (~2,400 EQ symbols: OHLCV, VWAP, turnover, delivery volume/%) for a given day, so building history here costs one request per day, not one per symbol.

- `./backfill_bhavcopy.sh` — slowly ensures the latest 30 trading sessions are available. It waits two seconds between NSE requests and skips dates already stored, so an interrupted run is safe to resume. Before the evening publication cutoff it starts from yesterday instead of repeatedly requesting today's unavailable file. Override with a positional session count, `BHAVCOPY_REQUEST_DELAY_SECONDS`, or `BHAVCOPY_PUBLISH_HOUR_IST`.
- `uv run bhavcopy.py backfill [days]` — the underlying resumable backfill command (default `BHAVCOPY_BACKFILL_DAYS=30`), skipping weekends and any date NSE has nothing for (holidays) rather than tracking a holiday calendar separately.
- `uv run bhavcopy.py` — fetches and stores just today's bhavcopy; this is what `run_overnight_scan.sh` calls before each scan to keep the DB current.
- `bhavcopy.get_delivery_trend(symbol)` — requires the complete 5-session recent and 20-session baseline windows. It compares both delivery percentage and actual delivery volume, includes recent price direction, and labels only combinations such as `possible_accumulation`, `possible_distribution`, or an unconfirmed percentage rise. These are market-participation clues, not proof of buyer/seller direction. Incomplete history and missing values are returned as explicit data-quality states.

This DB is also the natural data source for a future backtest harness (join `trade_log.jsonl` proposals against what these symbols' prices actually did) — not built yet, but the accumulating history is the same asset either way.

### Config (`.env`)

`NSE_SYMBOL` (default `RELIANCE`, used only when no symbols are passed on the command line and `NSE_INDEX` is unset), `NSE_PRINCIPAL` (default `100000`), `NSE_MAX_ALLOCATION_PCT` (default `10`), `NSE_MAX_LOSS_PCT` (default `1`), `NSE_ATR_STOP_MULTIPLE` (default `2`), `TRADE_LOG_PATH` (default `trade_log.jsonl`, gitignored), `NSE_INDEX` (e.g. `NIFTY 50` — if set and no symbols are passed on the command line, scans the index's constituents instead of falling back to `NSE_SYMBOL`), `NSE_SCAN_LIMIT` (optional, caps how many constituents are scanned), `NSE_SCAN_DELAY_SECONDS` (default `1`, pause between symbols in a batch/index scan), `NSE_CALL_DELAY_SECONDS` (default `0.3`, pause after each quote/historical-data call), `CACHE_DIR` (default `.cache`, gitignored), `FUNDAMENTALS_CACHE_TTL_HOURS` (default `24`), `FUNDAMENTALS_CALL_DELAY_SECONDS` (default `0.5`, pause after each of the ~7 fundamentals API calls per surviving symbol), `INTRADAY_CACHE_TTL_MINUTES` (default `5`), `BHAVCOPY_DB_PATH` (default `bhavcopy.db`, gitignored), `BHAVCOPY_BACKFILL_DAYS` (default `30`), `BHAVCOPY_REQUEST_DELAY_SECONDS` (default `2`, pause between whole-market archive requests), `BHAVCOPY_PUBLISH_HOUR_IST` (default `18`, before which backfill begins from yesterday). `NSE_RISK_PCT` remains a temporary fallback for the allocation cap so existing local `.env` files continue to run; replace it with `NSE_MAX_ALLOCATION_PCT`.

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

Every run appends one JSON line to `trade_log.jsonl`: timestamp, symbol, principal, loss/allocation/ATR-stop policy, typed technical assessment, full-precision indicators, risk plan, each node's verdict, final status (`proposed` / `flagged_for_review` / `aborted`), and proposal text. Inspect it with `jq`, e.g.:

```bash
jq -c '{symbol, status, technical_verdict}' trade_log.jsonl
```

This log is the intended way to eventually judge whether the checks are any good — log now, join against actual price outcomes later. No live grading is implemented; that's a deliberate second-phase feature, not an oversight.

Malformed lines (partial writes from a disk-full condition, concurrent appends) are skipped with a warning rather than crashing `digest.py`/`intraday_recheck.py` — but neither `trade_log.jsonl` nor `.cache/` are rotated or pruned. At `NIFTY TOTAL MKT` (750-symbol) overnight-scan scale this grows by roughly 1.5-2MB/night; archiving or trimming old entries is a real need before running this unattended for months, just not built yet.

## Automated email alerts

`notify.py`, `digest.py`, and `intraday_recheck.py` turn the trade graph into a cron-driven alert pipeline: scan the whole market overnight, email a full-detail digest once that scan completes, then keep an eye on that run's picks during the day.

- **`notify.py`** — generic email sending (`smtplib`, stdlib only). Stub by default (`EMAIL_ENABLED=0`): composes the email and logs it instead of sending, so everything downstream is testable without real SMTP credentials. `EMAIL_TO` is a comma-separated list — one address today, more later, no code change needed. Slack/Telegram aren't built (deliberately out of scope for now), but adding one later means adding one function here, not touching `digest.py`/`intraday_recheck.py`.
- **`scan_label`** — every row `nse_trade_graph.py` appends to `trade_log.jsonl` is tagged with `NSE_SCAN_LABEL` (default `manual`). This is how `digest.py`/`intraday_recheck.py` find "this run's" records without relying on calendar-date matching, which would be fragile for an overnight scan spanning midnight.
- **`run_overnight_scan.sh`** — the actual cron target. Generates a run id (`overnight_YYYYMMDD_HHMM`), runs `nse_trade_graph.py` over `NSE_INDEX="NIFTY TOTAL MKT"` tagged with that id, then calls `digest.py` with the same id. (`nsemine` recognizes the index name `NIFTY TOTAL MKT` — 750 constituents; `"NIFTY TOTAL MARKET"` does not, it hits an internal `nsemine` bug and returns `None`.)
- **`digest.py <run_id>`** — reads `trade_log.jsonl` filtered to that `scan_label`, emails one full-detail section (all four timeframes' indicators, every node's verdict text, the proposal) per `proposed`/`flagged_for_review` symbol, plus a one-line count of everything scanned/aborted for context.
- **`intraday_recheck.py [run_id]`** — finds the most recent `overnight_*` label (or takes one explicitly), collects the symbols that were `proposed`/`flagged_for_review` in that run, re-runs the full graph on just those (not the full 750 — confirmed too slow for a 15-30 min cadence), and emails a full-detail alert for any still `proposed`/`flagged_for_review`. A symbol that dropped to `aborted` since the overnight scan is logged but not emailed.

Runs on a different machine or a different local model port with zero code changes — everything routes through the existing `LOCAL_LLM_URL`/`LOCAL_LLM_MODEL` `.env` config already described above.

### IST timezone safety

NSE market hours are IST, but every timestamp in this codebase used to be a naive `datetime.now()` — silently using whichever timezone the *host machine's system clock* happened to be set to. Harmless on a box that's already IST, wrong by however many hours otherwise (confirmed live: `datetime.now()` under `TZ=UTC` showed 13:04 when the real IST time was 18:34 — a 5.5 hour gap). `nsemine` itself has the same issue internally (its `end_datetime` default is a naive datetime frozen at import time from its own `datetime.now()`).

Fixed via `market_time.py`: `now_ist()` / `now_ist_naive()` return the real IST time regardless of host timezone (`zoneinfo`, stdlib, no new dependency), and `is_market_hours()` checks 9:15-15:30 IST, Monday-Friday. Every log timestamp, cache-rollover date, and historical-data lookback window in `nse_trade_graph.py`/`nse_data.py`/`fundamentals.py`/`digest.py`/`intraday_recheck.py` goes through it. `run_overnight_scan.sh`'s run-id generation uses `TZ='Asia/Kolkata' date` explicitly for the same reason. `trade_log.jsonl` timestamps are now timezone-aware ISO strings (e.g. `...+05:30`) — unambiguous regardless of who's reading them or where.

`intraday_recheck.py` self-gates on `is_market_hours()` and exits immediately outside that window — this means the cron schedule itself doesn't need to get IST right; a simple "every 15-20 minutes, every day" entry is safe on any machine, any system timezone. Override the gate for manual testing with `NSE_SKIP_MARKET_HOURS_CHECK=1`.

### Config (`.env`)

`NSE_SCAN_LABEL` (default `manual`), `EMAIL_ENABLED` (default `0`, stub), `SMTP_HOST`, `SMTP_PORT` (default `587`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` (default `1`), `EMAIL_FROM`, `EMAIL_TO` (comma-separated), `NSE_SKIP_MARKET_HOURS_CHECK` (testing only, bypasses `intraday_recheck.py`'s market-hours gate).

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
