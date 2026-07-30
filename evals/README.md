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
- two-sided 95% Wilson intervals for each false-PASS rate;
- grounded and acceptable citation rates;
- first-pass validity, final validity, and repair rate;
- runtime error rate, p50/p95 latency, and mean response size.

Build an unlabeled review queue from the local NSE cache:

```bash
uv run build_llm_eval_candidates.py
```

The builder deduplicates snapshots and assigns event families, but deliberately
does not create expected verdicts. Its output remains under `evals/results/`
until a reviewer checks the source attachment, assigns a verdict/reason code,
and promotes the case into a versioned corpus.

## Initial baseline

Measured locally on 2026-07-30 using prompt v6, schema v4, temperature zero,
and a 2048-token response ceiling:

| Model | Verdict | Reason code | False PASS | REJECT→PASS (95% Wilson) | REVIEW→PASS | First-pass valid | p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen3-8B Q4_K_M | 57.7% | 57.7% | 44.4% | 22.2% (6.3–54.7%) | 66.7% | 100.0% | 1.42s |
| ODA-Fin-SFT-8B Q4_K_M | 42.3% | 38.5% | 55.6% | 66.7% (35.4–87.9%) | 44.4% | 100.0% | 1.34s |
| ODA-Fin-RL-8B Q4_K_M | 46.2% | 38.5% | 72.2% | 55.6% (26.7–81.1%) | 88.9% | 88.5% | 1.43s |
| Gemma 4 12B IT QAT | 76.9% | 76.9% | 5.6% | 0.0% (0.0–29.9%) | 11.1% | 100.0% | 2.69s |

All four models produced schema-valid final output and grounded citations for all
26 cases. That validates the guardrails, not the classifier semantics.

The three Q4_K_M checkpoints form the published lineage
Qwen3-8B → ODA-Fin-SFT-8B → ODA-Fin-RL-8B. On this task, most of the regression
appears at SFT: exact verdict accuracy falls from 57.7% to 42.3%, and explicit
REJECT false-PASS rises from 2/9 to 6/9. RL recovers one exact verdict and one
REJECT case but does not restore the base behavior.

None of the three Qwen-lineage checkpoints emits `REVIEW` on this corpus.
Their predicted `PASS` counts rise from 16/26 at the base checkpoint to 18/26
after SFT and 21/26 after RL, although only 8/26 labels are `PASS`. This is
consistent with an increasingly strong binary/PASS shortcut and loss of
abstention behavior.

That does not establish classic reward hacking. The
[published training mixture](https://arxiv.org/html/2603.07223v1) is dominated
by financial QA and sentiment, while specialized risk-analysis tasks are
negligible. The RL subset further retains concise, verifier-friendly answers.
The observed failure is therefore more consistent with negative
transfer/objective mismatch than a learned preference for `PASS`.

This first corpus is too small to authorize a production model switch by
itself. Gemma's observed 0/9 REJECT false-PASS still has a 29.9% upper Wilson
bound. Expand with reviewed, deduplicated real disclosures stratified by event
family, target at least 100 REJECT and 100 REVIEW cases, repeat runs to measure
stability, and keep false `PASS` on explicit `REJECT` cases as the primary
safety metric.
