---
name: chart-nse-ta
description: Create or review evidence-driven charts for this project's deterministic NSE equity technical analysis. Use when visualizing one symbol's completed-bar indicators, explaining trend/momentum/RSI/relative-strength/participation families, comparing a chart with the technical fact ledger, or designing TA validation graphs without changing the trading decision.
---

# Chart NSE technical evidence

Visualize what the deterministic engine actually observed and decided. Charts
are diagnostic views over grounded evidence, never an independent signal engine.

Read [chart-contract.md](references/chart-contract.md) before generating or
reviewing a chart.

## Preserve the evidence boundary

- Source values from completed OHLC histories, `technical_assessment`,
  `technical_indicators`, `technical_fact_ledger`, benchmark history, and
  bhavcopy delivery history.
- Do not let chart code calculate a different verdict or silently substitute
  indicators.
- Use TA-Lib if indicator recomputation is unavoidable. Do not require
  OpenAlgo, a broker connection, or a live WebSocket.
- Never invent missing delivery, benchmark, indicator, target, or stop values.
  Render unavailable evidence explicitly.
- Never place orders. Avoid decorative BUY/SELL markers; if a deterministic
  proposal is shown, label it `proposal` with its observation time.

## Chart workflow

1. Work on one symbol and one observation first.
2. Verify the symbol, as-of time, completed-bar policy, TA policy fingerprint,
   benchmark, and bhavcopy date.
3. Refuse to chart a normal-looking decision when the assessment is
   `invalid_data`. Render the reason codes instead.
4. Confirm timestamps are ordered and unique and indicator arrays align
   one-to-one with their price series.
5. Build the minimum useful panels:
   - daily candlesticks with SMA20 and SMA50;
   - MACD, signal, and histogram;
   - RSI14 with the recorded adaptive lower and upper bands;
   - family scores with the engagement deadzone and required-positive labels.
6. Add optional evidence only when available:
   - normalized stock-versus-benchmark performance for relative strength;
   - delivery percentage, delivery volume, total volume, and price context;
   - a timeframe heatmap for trend and momentum breakdown.
7. Annotate the chart with policy ID/fingerprint, as-of session, data
   freshness, benchmark, and participation state.
8. Cross-check at least the final close, SMA values, RSI, and family scores
   against the ledger/assessment before delivering the artifact.

## Rendering rules

- Keep price-scale overlays on the price axis and bounded oscillators in their
  own panels.
- Show gaps between NSE sessions without manufacturing candles.
- Use the adaptive RSI band from evidence, not hard-coded 30/70 lines.
- Distinguish `neutral_or_not_engaged`, `unavailable`, `invalid_data`, and a
  true numeric zero visually and in text.
- Plot delivery percentage and absolute delivery volume separately; they have
  different meanings and scales.
- Label tick data as ticks. Never compare a tick-window EMA with a bar EMA.
- Prefer a self-contained HTML chart. Write temporary artifacts under
  `/tmp/nse-stock-picker-ta/` unless the user requests a retained project path.

## Dependencies and repository changes

- Reuse an installed plotting library. If no plotting dependency exists, stop
  before modifying `pyproject.toml` and explain the smallest option.
- Do not add Dash, Streamlit, WebSocket, or dashboard infrastructure for a
  single diagnostic chart.
- Keep generated HTML, images, and CSV files out of source directories.
- If implementing reusable chart code, separate data assembly from rendering
  so the evidence payload can be tested without a browser.
- Add only a focused test for changed data assembly or an identified charting
  defect; visual polish alone does not require a broad test suite.

## Chart handoff

Provide the artifact path plus a short legend stating:

- what is deterministic;
- what is unavailable or unconfirmed;
- the observation/session represented;
- whether the chart agrees with the stored fact ledger.
