# Qualitative LLM evaluation

`qualitative_disclosures_v1.jsonl` is the versioned offline corpus for the
bounded announcement/corporate-action classifier. It is not a fundamental
quality dataset or a trading backtest.

The corpus contains:

- 16 bounded excerpts derived from the local NSE snapshot cache dated
  2026-07-29. They use the normalized fields the production prompt sees and
  are not represented as byte-for-byte source filings.
- 10 synthetic policy probes covering explicit adverse events and untrusted
  instruction text.
- 8 expected `PASS`, 9 expected `REVIEW`, and 9 expected `REJECT` cases.

Every synthetic case is labeled `synthetic_policy_probe`; it must never be
represented as an actual company disclosure. Expected labels are reviewable
policy judgments and should change only with a rationale and corpus version
bump.

## Finalized model

The production local model is Phi-4 14B Q4_K_M through Ollama/llama.cpp:

```bash
ollama pull phi4:14b-q4_K_M
uv run run_llm_eval.py \
  --model 'phi4:14b-q4_K_M::' \
  --output evals/results/current.json
```

The empty `::` suffix keeps the model-specific no-think directive empty.
Temperature is zero, the fundamental response ceiling is 2048 tokens, and the
strict JSON Schema remains enabled.

Measured locally on 2026-07-30 using prompt v6 and schema v4:

| Verdict | Reason code | False PASS | REJECT→PASS (95% Wilson) | REVIEW→PASS | First-pass valid | p50 |
|---:|---:|---:|---:|---:|---:|---:|
| 96.2% | 96.2% | 5.6% | 0/9 (0.0–29.9%) | 1/9 | 100.0% | 2.49s |

The sole miss was the terse TVS bonus/NCRPS disclosure, which was passed
instead of abstaining with `REVIEW`. The retained local evidence files are:

- `results/phi4_14b_q4km_v1.json` — full 26-case quality baseline.
- `results/bench_llamacpp.json` — concurrency/latency/VRAM measurement.

This corpus is too small to treat 0/9 observed `REJECT` false passes as zero
risk; its 95% Wilson upper bound is 29.9%. Expand with reviewed, deduplicated
real disclosures stratified by event family, targeting at least 100 `REJECT`
and 100 `REVIEW` cases. Keep false `PASS` on explicit `REJECT` cases as the
primary safety metric.

## Result contents

Evaluation result files are local and gitignored. They contain per-case
outcomes plus:

- exact verdict and reason-code rates;
- false-PASS rates overall and split by expected `REVIEW`/`REJECT`;
- two-sided 95% Wilson intervals;
- grounded and acceptable citation rates;
- first-pass validity, final validity, and repair rate;
- runtime error rate, p50/p95 latency, completion-token usage, and retained
  model reasoning when the server exposes it.

Build an unlabeled review queue from the local NSE cache with:

```bash
uv run build_llm_eval_candidates.py
```

The builder deduplicates snapshots and assigns event families but deliberately
does not create expected verdicts. A reviewer must check the source attachment,
assign a verdict/reason code, and promote the case into a versioned corpus.
