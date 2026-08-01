---
name: review-nse-ta
description: Audit this project's deterministic NSE equity technical-analysis inputs, indicators, policy scores, evidence ledger, and verdicts. Use when reviewing a symbol's TA result, investigating missing or zero TA values, validating bhavcopy delivery participation or benchmark-relative strength, changing TA indicators or policy thresholds, or checking whether data supplied to an LLM is grounded and complete.
---

# Review NSE technical analysis

Review the deterministic engine as an evidence system. Establish whether its input
data and calculations are valid before judging whether a verdict looks plausible.

Read [project-contract.md](references/project-contract.md) before reviewing a
symbol result or proposing a TA change.

## Preserve the architecture boundary

- Keep TA calculation, thresholding, confluence, and verdicts deterministic.
- Let an LLM summarize only `technical_fact_ledger`; never ask it to recompute
  indicators, compare numeric thresholds, or override the verdict.
- Use the existing NSE/bhavcopy/cache pipeline and TA-Lib. Do not introduce an
  OpenAlgo server, broker dependency, live feed, or another indicator library.
- Treat all outputs as research or proposals. Never place or imply an order.
- Inspect existing stored data first. If fetching is required, preserve the
  application's slow, cached NSE access pattern.

## Review workflow

1. Define one review target: a symbol/session, a recorded scan, or a specific
   code/policy change. Do not start with an index-wide scan.
2. Use codebase-memory graph tools first to locate and trace code. Start at
   `ta_analysis.evaluate_technical`, `ta_analysis.score_technical`,
   `ta_analysis.build_technical_fact_ledger`, or
   `nse_trade_graph.node_technical`.
3. Capture provenance: symbol, observation time, completion policy, TA policy
   ID and fingerprint, benchmark, and bhavcopy latest date.
4. Validate inputs before reading scores:
   - required timeframes `D`, `30`, `15`, and `5`;
   - at least 50 completed, finite `close`, `high`, and `low` values;
   - sorted, unique timestamps with plausible OHLC values;
   - no unfinished candle for the observation time;
   - stock and benchmark sessions aligned for the relative-strength window;
   - delivery history explicitly `ready`, `insufficient_history`,
     `missing_values`, or unavailable.
5. Recompute through `evaluate_technical` and build the fact ledger. Compare
   the assessment, persisted state, Slack text, and ledger; they must describe
   the same decision.
6. Check family semantics:
   - trend is SMA20 versus SMA50 distance;
   - momentum is MACD histogram scaled by ATR;
   - RSI is a caution family using the recorded adaptive band;
   - relative strength is stock return relative to the policy benchmark;
   - participation is neutral when delivery evidence is absent or
     unconfirmed.
7. Run a truncation check when calculation integrity is in doubt: recompute
   using data ending at several historical bars. Earlier outputs must not
   change when later bars are appended.
8. Report findings in priority order: invalidating defects, misleading
   evidence, calibration risks, then verified behavior. Include actual values
   and dates, not impressions.

## Interpret zero and missing values correctly

- A family score of zero may mean neutral, unavailable, or not engaged. Use
  `participation_state`, `data_status`, and the engagement threshold to
  distinguish them.
- Rising delivery percentage alone is not accumulation. Require rising
  delivery volume, expanded total volume, and compatible price behavior.
- Do not count four timeframes as four independent confirmations. Confluence
  is across families after each family is collapsed across timeframes.
- Do not promote a plausible narrative over an `invalid_data` assessment.

## Changes and tests

- Change policy parameters in `technical_policies.json`, not as hidden constants.
- Do not tune on one symbol or a hand-picked winner. Record a hypothesis and
  validate out of sample before changing production thresholds.
- Keep test additions minimal. Add a test only for a changed invariant, a
  reproduced defect, or an important boundary that lacked coverage.
- Prefer one focused test command before any broad suite.
- Do not change code when the user asks only for analysis or diagnosis.

## Review output

Return:

- target and exact observation/session;
- data-quality verdict and provenance;
- a compact table of family score, state, source evidence, and confidence;
- discrepancies between raw data, assessment, ledger, and notification;
- the smallest justified next action.
