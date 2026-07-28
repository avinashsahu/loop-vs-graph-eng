# loop-vs-graph-eng

Small side-by-side comparison of two ways to structure an LLM retry/verify agent, plus a real branching use case built on the graph style.

- `loop_agent.py` — plain `while` loop, state in local vars, control flow via `if`/`break`.
- `graph_agent.py` — explicit node graph, state in a dict, control flow via `(next_node, state)` return values.
- `nse_trade_graph.py` — real use case for the graph style: an NSE (Indian stock exchange) trade-proposal pipeline with four independent, differently-failing checks, runnable over a single symbol, an explicit list, or a whole index's constituents.
- `nse_data.py`, `fundamentals.py`, `ta_analysis.py`, `cache.py` — data/analysis layers used by `nse_trade_graph.py` (see below).

Both toy agents implement the same task: answer a question, check the answer, retry up to `MAX_ITERS` times until the check passes.

See `NOTES.md` for the tradeoffs between the loop and graph styles.

## Setup

Requires [`uv`](https://github.com/astral-sh/uv) and, for the `nse_trade_graph.py` technical indicators, the TA-Lib C library (macOS via Homebrew):

```bash
brew install ta-lib
```

Then:

```bash
uv sync
```

This creates `.venv` and installs the pinned dependencies (`anthropic`, `openai`, `nsemine`, `python-dotenv`, `ta-lib`).

If `uv add ta-lib` / `uv sync` ever fails to find the C library's headers, point it explicitly:

```bash
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
| Local (gemma4 via MLX) | `USE_LOCAL_LLM=1` | Talks to a local `mlx_lm.server` over its OpenAI-compatible API. |

If both `USE_REAL_LLM` and `USE_LOCAL_LLM` are set, local wins.

### Running the local gemma4 backend

Start the model server once (leave it running in the background):

```bash
mlx_lm.server --model mlx-community/gemma-4-12B-it-4bit --port 8080
```

Config (`.env`): `LOCAL_LLM_URL` (default `http://localhost:8080/v1`), `LOCAL_LLM_MODEL` (default `mlx-community/gemma-4-12B-it-4bit`), `LOCAL_LLM_MAX_TOKENS` (default `300`).

gemma4 is a reasoning model — by default it burns tokens on an internal "thinking" channel before answering, which can leave `content` empty if `max_tokens` is too low. `llm.py` disables this per-request via `chat_template_kwargs: {"enable_thinking": false}`, and falls back to the raw reasoning text if `content` ever comes back `None` anyway, so `call_llm` never returns `None`.

## Logging

Both `loop_agent.py` and `graph_agent.py` (and `nse_trade_graph.py`) log through `logging_config.setup_logging(name)` instead of `print` — timestamped, one logger per module (`loop`, `graph`, `nse`), with the noisy `httpx` request logger quieted to `WARNING`. A `WARNING` is emitted whenever a run hits `MAX_ITERS` without a passing verdict.

## Running the toy agents

```bash
# stub, no network
uv run loop_agent.py
uv run graph_agent.py

# against local gemma4 (server must already be running, see above; set USE_LOCAL_LLM=1 in .env)
uv run loop_agent.py
uv run graph_agent.py

# against Claude (set USE_REAL_LLM=1 and ANTHROPIC_API_KEY in .env)
uv run loop_agent.py
```

Each run prints the answer/verdict per iteration and the final result.

## NSE trade graph (`nse_trade_graph.py`)

A real branching use case for the graph style: propose (never execute) an NSE stock trade, gated behind four independent checks that each fail differently.

```
fetch -> technical -> [BAD] -> technical_retry_guard -> fetch (retry) or abort
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

- **technical** — computes real indicators (SMA20, SMA50, RSI14, MACD(12,26,9), via `ta_analysis.compute_indicators`, **TA-Lib**-backed) independently for four timeframes — daily plus 30/15/5-minute (from `nse_data.get_multi_timeframe_history`) — and asks the LLM to interpret all four together (one prompt, one verdict) for a short-term buy signal. Retries (re-fetches data) up to `MAX_ITERS` on a `BAD` verdict, then aborts. Grounding the LLM in computed indicators instead of a raw OHLCV table gives consistent verdicts across retries instead of the model re-reading the same table differently each time. Deliberately not split into one node per timeframe — this project only splits nodes when the retry/remediation path genuinely differs, and all four timeframes share the same retry loop.
- **fundamental** — `fundamentals.get_fundamental_snapshot` pulls corporate announcements, corporate actions, shareholding pattern, yearwise returns, and a peer comparison (all via raw NSE `NextApi` endpoints, see `fundamentals.py`). A deterministic hard check runs first: negative EPS or PAT aborts immediately, same philosophy as `risk`'s circuit-limit check — an objective number isn't the LLM's job to hallucinate over. Otherwise the LLM reads the qualitative parts (corp actions, shareholding trend, peer standing) for a `GOOD`/`BAD` verdict; a soft `BAD` here routes to `flag_review`, same policy as `sentiment`, not an abort.
- **risk** — deterministic code, not an LLM call. Computes position size from `principal * risk_pct / 100`, validates `risk_pct` is sane (0-25%), and aborts if the stock's current low is within 2% of its lower circuit limit. Kept out of the LLM's hands on purpose: risk sizing is exactly the kind of check where a hallucinated "looks fine" verdict is the expensive failure mode.
- **sentiment** — LLM checks whether today's price move looks like a reasonable entry (not a crash or a spike), using the live quote's `changepct` and sector. Include the company's full name, not just the ticker, in the prompt — a bare ticker (e.g. `ACE`) can be genuinely ambiguous to the model and has been observed to send gemma4 into a repetitive non-terminating reasoning loop.
- **propose** — never calls a broker. Only ever produces a proposal string for a human to review and act on manually.

Known data quirks:
- `nsemine`'s `get_stock_live_quotes` returns `upper_circuit` and `lower_circuit` swapped (confirmed against the raw NSE `priceInfo.priceBand` field, which is always `"lower-upper"`). `node_risk` in `nse_trade_graph.py` corrects for this on read — don't trust those two field names at face value if you use `nsemine` elsewhere.
- `nsemine.live.get_index_constituents_live_snapshot`'s own docstring example calls its index parameter `index_name=`, but the real keyword is `index` — `nse_data.get_index_symbols` calls it correctly, just don't copy the docstring.
- The `getYearwiseData` fundamentals endpoint (only that one) needs the symbol suffixed with `EQN` (e.g. `HDFCBANKEQN`); every other fundamentals endpoint uses the bare symbol — see `fundamentals.py`.

### Index scan / batch mode

Besides one symbol or an explicit list, `nse_trade_graph.py` can resolve and scan an entire index's constituents via `nse_data.get_index_symbols` (wraps `nsemine.live.get_index_constituents_live_snapshot`). A per-symbol failure (e.g. one blocked/slow fetch) is caught and logged so it doesn't abort the rest of the batch. `cache.py` backs both the multi-timeframe history and fundamentals fetches with a per-key JSON file under `CACHE_DIR` — this matters at batch scale: a full-index scan is ~13 HTTP calls per symbol (quote + 4 historical timeframes + several fundamentals endpoints), and the `technical_retry_guard` loop would otherwise re-fetch all of it, including same-day fundamentals, on every retry.

### Config (`.env`)

`NSE_SYMBOL` (default `RELIANCE`, used only when no symbols are passed on the command line and `NSE_INDEX` is unset), `NSE_PRINCIPAL` (default `100000`), `NSE_RISK_PCT` (default `10`, meant to scale with your actual principal, not be hardcoded), `TRADE_LOG_PATH` (default `trade_log.jsonl`, gitignored), `NSE_INDEX` (e.g. `NIFTY 50` — if set and no symbols are passed on the command line, scans the index's constituents instead of falling back to `NSE_SYMBOL`), `NSE_SCAN_LIMIT` (optional, caps how many constituents are scanned), `NSE_SCAN_DELAY_SECONDS` (default `1`, pause between symbols in a batch/index scan), `CACHE_DIR` (default `.cache`, gitignored), `FUNDAMENTALS_CACHE_TTL_HOURS` (default `24`), `INTRADAY_CACHE_TTL_MINUTES` (default `5`).

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

Every run appends one JSON line to `trade_log.jsonl`: timestamp, symbol, principal, risk_pct, iters, computed technical indicators (nested per timeframe), each node's verdict (including `fundamental_verdict`), final status (`proposed` / `flagged_for_review` / `aborted`), and the proposal text. Inspect it with `jq`, e.g.:

```bash
jq -c '{symbol, status, technical_verdict}' trade_log.jsonl
```

This log is the intended way to eventually judge whether the checks are any good — log now, join against actual price outcomes later. No live grading is implemented; that's a deliberate second-phase feature, not an oversight.
