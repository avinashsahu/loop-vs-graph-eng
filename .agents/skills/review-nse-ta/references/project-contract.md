# NSE TA project contract

## Canonical implementation

| Concern | Canonical location |
|---|---|
| Input validation and assessment | `ta_analysis.evaluate_technical` |
| Indicator calculation | `ta_analysis.compute_indicators` |
| Family scoring and confluence | `ta_analysis.score_technical` |
| LLM-safe evidence | `ta_analysis.build_technical_fact_ledger` |
| Policy values and fingerprint input | `technical_policies.json` |
| Graph integration | `nse_trade_graph.node_technical` |
| Delivery trend | `bhavcopy.get_delivery_trend` |
| Completed market snapshot | `nse_data.get_market_snapshot` |

Use codebase-memory graph tools before text search when discovering or tracing
these functions.

## Required observations

- Timeframes: `D`, `30`, `15`, `5`.
- Minimum history: 50 completed bars per timeframe.
- Required finite columns: `close`, `high`, `low`.
- Revised-policy benchmark: the exact symbol in `technical_policies.json`.
- Relative-strength sessions: the stock and benchmark dates in the full
  lookback must match exactly.
- Delivery trend: latest 5 sessions versus the preceding 20 sessions.

Also audit properties not yet guaranteed by the primary validation:

- timestamps ordered, unique, and session-plausible;
- positive prices and `high >= max(open, close)` and
  `low <= min(open, close)` when open is available;
- no future or unfinished candle;
- delivery and benchmark dates no older than the assessed session;
- no corporate-action discontinuity mistaken for momentum.

## Evidence ledger

`technical_fact_ledger` is the only TA payload an explanatory LLM may
summarize. Its current schema is `technical-explanation-input-v2`.

Expected facts when ready:

- `TA_DATA_QUALITY`
- `TA_DECISION`
- `TA_TREND`
- `TA_MOMENTUM`
- `TA_RSI`
- `TA_TIMEFRAMES`
- `TA_RELATIVE_STRENGTH` when enabled and available
- `TA_PARTICIPATION` when enabled

For invalid data, the ledger should contain data quality and reason codes, not
a plausible-looking score narrative.

## Family semantics

- Trend: weighted SMA20/SMA50 gap across timeframes.
- Momentum: weighted MACD histogram divided by an ATR-based scale.
- RSI: daily adaptive-band caution; neutral contributes zero.
- Relative strength: stock return divided by benchmark return over the
  policy lookback, expressed as relative percentage return.
- Participation: bhavcopy evidence with liquidity and confirmation states.

Trend and momentum must remain positive for a `GOOD` verdict under both
persisted policies. Family scores are the independent confluence units;
timeframes are correlated observations within a family.

## Participation meanings

- `unavailable`: no ready delivery evidence; neutral score.
- `available_neutral`: evidence exists but is not directional.
- `possible_accumulation`: rising delivery volume, expanded total volume, and
  compatible positive price movement.
- `possible_distribution`: rising delivery volume with compatible negative
  price movement.
- `illiquid`: recent turnover is below the policy minimum; negative score.

Delivery percentage is a ratio of delivered volume to total volume. A rising
ratio alone does not identify buyer-initiated accumulation.

## Review failure codes

Prefer stable, specific codes in reports and tests:

- `STALE_OR_UNFINISHED_BAR`
- `MISALIGNED_TIMEFRAME`
- `MISALIGNED_BENCHMARK_SESSIONS`
- `INSUFFICIENT_HISTORY`
- `NON_FINITE_VALUE`
- `INVALID_OHLC`
- `STALE_BHAVCOPY`
- `PARTICIPATION_UNCONFIRMED`
- `LEDGER_ASSESSMENT_MISMATCH`
- `POLICY_IDENTITY_MISMATCH`
- `LOOKAHEAD_DETECTED`
