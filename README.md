# loop-vs-graph-eng

Small side-by-side comparison of two ways to structure an LLM retry/verify agent, plus a real branching use case built on the graph style.

- `loop_agent.py` — plain `while` loop, state in local vars, control flow via `if`/`break`.
- `graph_agent.py` — explicit node graph, state in a dict, control flow via `(next_node, state)` return values.
- `nse_trade_graph.py` — real use case for the graph style: an NSE (Indian stock exchange) trade-proposal pipeline with three independent, differently-failing checks.

Both toy agents implement the same task: answer a question, check the answer, retry up to `MAX_ITERS` times until the check passes.

See `NOTES.md` for the tradeoffs between the loop and graph styles.

## Setup

Requires [`uv`](https://github.com/astral-sh/uv).

```bash
uv sync
```

This creates `.venv` and installs the pinned dependencies (`anthropic`, `openai`, `nsemine`, `python-dotenv`).

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

A real branching use case for the graph style: propose (never execute) an NSE stock trade, gated behind three independent checks that each fail differently.

```
fetch -> technical -> [BAD] -> technical_retry_guard -> fetch (retry) or abort
              |
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

- **technical** — LLM checks recent OHLCV (from `nsemine`) for a short-term buy signal. Retries (re-fetches data) up to `MAX_ITERS` on a `BAD` verdict, then aborts.
- **risk** — deterministic code, not an LLM call. Computes position size from `principal * risk_pct / 100`, validates `risk_pct` is sane (0-25%), and aborts if the stock's current low is within 2% of its lower circuit limit. Kept out of the LLM's hands on purpose: risk sizing is exactly the kind of check where a hallucinated "looks fine" verdict is the expensive failure mode.
- **sentiment** — LLM checks whether today's price move looks like a reasonable entry (not a crash or a spike), using the live quote's `changepct` and sector. Include the company's full name, not just the ticker, in the prompt — a bare ticker (e.g. `ACE`) can be genuinely ambiguous to the model and has been observed to send gemma4 into a repetitive non-terminating reasoning loop.
- **propose** — never calls a broker. Only ever produces a proposal string for a human to review and act on manually.

Known data quirk: `nsemine`'s `get_stock_live_quotes` returns `upper_circuit` and `lower_circuit` swapped (confirmed against the raw NSE `priceInfo.priceBand` field, which is always `"lower-upper"`). `node_risk` in `nse_trade_graph.py` corrects for this on read — don't trust those two field names at face value if you use `nsemine` elsewhere.

### Config (`.env`)

`NSE_SYMBOL` (default `RELIANCE`), `NSE_PRINCIPAL` (default `100000`), `NSE_RISK_PCT` (default `10`, meant to scale with your actual principal, not be hardcoded), `TRADE_LOG_PATH` (default `trade_log.jsonl`, gitignored).

### Running

```bash
uv run nse_trade_graph.py
```

Every run appends one JSON line to `trade_log.jsonl`: timestamp, symbol, principal, risk_pct, iters, each node's verdict, final status (`proposed` / `flagged_for_review` / `aborted`), and the proposal text. Inspect it with `jq`, e.g.:

```bash
jq -c '{symbol, status, technical_verdict}' trade_log.jsonl
```

This log is the intended way to eventually judge whether the checks are any good — log now, join against actual price outcomes later. No live grading is implemented; that's a deliberate second-phase feature, not an oversight.
