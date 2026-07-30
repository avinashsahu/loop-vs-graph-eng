# Qualitative LLM evaluation

`qualitative_disclosures_v1.jsonl` is a small, versioned offline corpus for the
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
policy judgments and should be changed only with a rationale and a corpus
version bump.

Run the configured local model:

```bash
uv run run_llm_eval.py --output evals/results/current.json
```

Compare models by repeating `--model`. Use an empty no-think directive for
models that do not support Qwen's `/no_think` control. The `::` suffix sets
the directive per model, including an empty directive:

```bash
uv run run_llm_eval.py \
  --model 'hf.co/alexsabaka/ODA-Fin-RL-8B-GGUF:Q4_K_M::/no_think' \
  --model 'gemma4:12b-it-qat::' \
  --output evals/results/comparison.json
```

Result files are local and gitignored. They include per-case outcomes plus:

- exact verdict and reason-code rates;
- false-PASS rates overall and split by expected `REVIEW`/`REJECT`;
- grounded and acceptable citation rates;
- first-pass validity, final validity, and repair rate;
- runtime error rate, p50/p95 latency, and mean response size.

## Initial baseline

Measured locally on 2026-07-30 using prompt v6, schema v4, temperature zero,
and a 2048-token response ceiling:

| Model | Verdict | Reason code | False PASS | REJECT→PASS | REVIEW→PASS | First-pass valid | p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ODA-Fin-RL-8B Q4_K_M | 46.2% | 38.5% | 72.2% | 55.6% | 88.9% | 88.5% | 1.43s |
| Gemma 4 12B IT QAT | 76.9% | 76.9% | 5.6% | 0.0% | 11.1% | 100.0% | 2.69s |

Both models produced schema-valid final output and grounded citations for all
26 cases. That validates the guardrails, not the classifier semantics.

This first corpus is too small to authorize a production model switch by
itself. Expand it with reviewed real adverse disclosures, repeat each model run
to measure stability, and treat false `PASS` on explicit `REJECT` cases as the
primary safety metric.
