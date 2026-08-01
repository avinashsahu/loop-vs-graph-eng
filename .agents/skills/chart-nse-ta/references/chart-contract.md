# NSE TA chart contract

## Required provenance

Every chart must display:

- NSE symbol;
- assessment/session time in IST;
- completed-bar policy;
- technical policy ID and fingerprint;
- benchmark symbol when relative strength is shown;
- bhavcopy latest date when participation is shown.

If any provenance field is absent, mark it `unconfirmed`; do not infer it.

## Core panels

1. Daily price
   - Candlesticks from completed daily bars.
   - SMA20 and SMA50 overlays from the same history.
   - No generic support/resistance or BUY/SELL annotations.
2. Momentum
   - MACD, signal, and histogram.
   - Zero reference line.
3. RSI
   - RSI14.
   - Recorded adaptive lower and upper bands from `TA_RSI`.
4. Family evidence
   - Trend, momentum, RSI, relative strength, and participation scores when
     present.
   - Shade the engagement deadzone.
   - Mark required-positive families without pretending scores are
     probabilities.

## Optional panels

Relative strength:

- Align stock and benchmark on identical sessions.
- Normalize both close series to 100 at the start of the policy lookback.
- Annotate stock, benchmark, and relative return from
  `TA_RELATIVE_STRENGTH`.

Participation:

- Plot delivery percentage as a ratio.
- Plot absolute delivery volume and total volume on compatible volume axes.
- Show recent and baseline windows distinctly.
- State `participation_state` and `data_status`.
- Do not label delivery-percentage growth as accumulation without the required
  absolute-volume and price confirmation.

Timeframes:

- Use a small heatmap for trend and momentum scores across `D`, `30`, `15`,
  and `5`.
- Present it as a decomposition, not four independent votes.

## Invalid and missing states

- `invalid_data`: show reason codes instead of normal indicator panels.
- `unavailable`: use a neutral gray panel with source status.
- `neutral_or_not_engaged`: show a valid zero/deadzone state.
- non-finite value: stop normal rendering and expose the failing field.
- stale evidence: retain the chart only with an explicit stale-data banner.

## Verification

Before handoff:

- compare the last close and final indicator values with
  `technical_indicators`;
- compare all family scores and states with `technical_fact_ledger`;
- confirm the final timestamp is a completed NSE bar;
- confirm stock and benchmark arrays use the same sessions;
- confirm displayed participation dates match bhavcopy evidence;
- report whether the chart and ledger agree.
