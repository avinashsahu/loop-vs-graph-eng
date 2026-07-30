# Future enhancements

This document records the work intentionally deferred after completing the
typed Scan Engine and decision-graph architecture. Priorities are ordered by
their effect on decision validity and practical usefulness, not by coding
convenience.

## Current baseline

- Batch and intraday scans share the typed `ScanRequest -> ScanResult`
  interface.
- Decisions persist policy/model identity, typed reasons, stage timings,
  failures, and family-level decision graphs.
- Technical policy replay, benchmark-relative strength, delivery
  participation, sector-aware fundamental checks, and bounded qualitative LLM
  interpretation are implemented.
- The evaluation ledger has historical decisions but does not yet have enough
  completed future-session outcomes to justify indicator, threshold, prompt,
  or model tuning.

## P0 — operate and validate the closed loop

Run the existing pipeline consistently before changing decision policy:

1. Refresh bhavcopy after publication.
2. Update eligible paper outcomes.
3. Run a small paced scan before expanding the universe.
4. Inspect data freshness, decision-graph attribution, durable failures, and
   alert eligibility.
5. Accumulate enough distinct symbol/date/policy outcomes before comparing
   technical families or model configurations.

Do not optimize weights from repeated same-day scans or a very small outcome
sample.

## P1 — point-in-time market-data correctness

The evaluator currently declares a `raw_unadjusted_bhavcopy` price basis. Before
using a long outcome history for calibration:

- detect splits, bonuses, consolidations, and other price-discontinuous
  corporate actions;
- either adjust historical OHLC consistently or mark affected outcome windows
  ineligible;
- persist point-in-time index membership so historical replays do not use
  today's constituents;
- record the adjustment/membership methodology in the evaluation identity;
- distinguish a reference entry from an actual fill.

Success means a corporate action or later index rebalance cannot silently
produce a false return, stop, target, or benchmark comparison.

## P1 — portfolio construction and operator feedback

Per-symbol sizing is not portfolio construction. Add a layer after eligible
scan results that:

- ranks candidates using deterministic, versioned policy;
- respects available cash and a portfolio-wide deployment cap;
- limits sector concentration and highly correlated simultaneous positions;
- resolves competing proposals without pretending every symbol can consume
  the full per-symbol allocation;
- records manual accept, reject, defer, and the operator's reason;
- records actual entry/exit fills when supplied;
- compares proposed, accepted, and executed cohorts separately.

This layer must consume typed scan results; it must not reach into the Scan
Engine's private mutable workflow state.

## P2 — independent qualitative-model evaluation

Trading returns alone cannot determine whether an LLM interpreted a disclosure
correctly. Build a small, reviewed corpus of NSE announcements containing:

- the expected `PASS`, `REVIEW`, or `REJECT` classification;
- the allowed reason code;
- required and forbidden evidence citations;
- ambiguous examples that must fail closed;
- adversarial text that resembles instructions inside evidence.

Track classification agreement, false-pass rate, invalid citations, unsupported
claims, schema/repair failures, latency, and token usage per prompt/model
identity. Keep deterministic numeric policy outside this benchmark.

## P2 — technical coverage still intentionally missing

The next technical additions should be evidence families, not more oscillators:

- benchmark regime and market breadth;
- explicit liquidity/impact eligibility;
- volatility-regime context;
- walk-forward and held-out policy comparison with data-snooping controls.

Add these only after outcome collection is operating. Preserve the current rule
that formulas remain internal details rather than decision-graph nodes.

## P2 — operational durability and health

Decisions currently have a JSONL durable fallback while decisions, scan runs,
alerts, caches, and outcomes span separate stores. Add an operational view that
reports:

- JSONL-to-SQLite reconciliation and missing decision identities;
- incomplete or recovered scan runs;
- NSE request failures, throttling, and data age;
- Aerospike availability, cache hit/miss/staleness, and queued XBRL work;
- LLM latency, timeout, repair, and fail-closed rates;
- alert delivery attempts and per-channel success.

Prefer one correlation identity across scan run, decision, alert transition,
operator action, and outcome. Do not remove the recoverable JSONL fallback
until the replacement has demonstrated equivalent failure recovery.

## P3 — immutable filing provenance

Shareholding XBRL has an immutable filing-oriented cache, while integrated
financial XBRL is primarily retained inside a daily normalized snapshot. Retain
raw financial filing hashes and immutable source metadata so new parsers can
rebuild historical normalized facts without refetching NSE or losing
reproducibility.

## Recommended sequence

1. Operate the existing closed loop and inspect small scans.
2. Make outcome data corporate-action and point-in-time safe.
3. Add portfolio selection and operator/fill feedback.
4. Establish the labelled qualitative-model benchmark.
5. Add regime, breadth, and liquidity evidence.
6. Consolidate operational health and filing provenance.

## Validation evidence — 30 July 2026

This is a one-run operational observation, not a model benchmark. The raw
records remain in the local, gitignored JSONL/SQLite ledgers under the run
labels below; the summary is not independently reproducible from this document
alone. The scan ledger also does not retain the index endpoint's ordered
constituent response or an index-membership as-of identity, so the recorded
first-ten ordering cannot be reconstructed later from the decision records.

A paced production scan used the first ten constituents returned by the live
`NIFTY NEXT 50` index endpoint:

`DIVISLAB`, `TVSMOTOR`, `HAL`, `ADANIPOWER`, `TMCV`, `CHOLAFIN`,
`CUMMINSIND`, `TORNTPHARM`, `BRITANNIA`, and `INDHOTEL`.

Run `validation_next50_first10_20260730_0745` persisted all ten requested
symbols with no infrastructure failures:

- seven failed technical confluence and correctly skipped fundamentals, LLM,
  risk, alert, and outcome eligibility;
- three passed technicals and correctly became `REVIEW` while their
  shareholding XBRL history was queued for warming;
- every decision carried the same eight meaningful decision-graph nodes and
  the expected policy/model identity.

After warming only the three deferred symbols, run
`validation_next50_warmed3_20260730_0755` reached complete fundamental coverage
and invoked the configured local ODA-Fin-RL model for all three. All three
failed closed to qualitative `REVIEW`, so the live risk/proposal path was not
reached:

- `DIVISLAB` needed the repair path and still ended as
  `invalid_model_response`;
- `TVSMOTOR` returned plausible but unconstrained missing-detail labels for a
  terse acquisition disclosure;
- `HAL` cited dividend evidence IDs and also returned those same IDs as
  “missing”, which is internally unhelpful;
- an isolated 512-token `DIVISLAB` retry still associated repeated dividends
  with promoter/dilution concern and returned a contradictory
  `REVIEW`/material-reject reason-code pair.

Conclusion: the Scan Engine, persistence, evidence gating, graph attribution,
and fail-closed adjudication behaved as designed. The configured qualitative
model failed this three-symbol validation sample. Increasing the response
budget from 384 to 512 tokens did not correct the observed semantic/schema
problem in one isolated `DIVISLAB` retry, so token count should not be the first
remedy; this does not establish that token budget never matters. Do not weaken
the fail-closed checks to make more proposals. Prioritize the labelled
qualitative benchmark and compare prompt/model candidates before trusting live
LLM passes.

This sample did not reach live risk sizing or proposal creation, and it did not
exercise Slack delivery, future-outcome collection, or an injected durable
infrastructure failure. Those paths remain covered by tests or await later
operational evidence; this run is not proof of the entire application
end-to-end.
