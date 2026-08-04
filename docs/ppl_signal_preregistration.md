# Pre-registration: PPL context-value signal check (Step 1)

Status: **DRAFT** — becomes binding when frozen by a commit made *before* the first
full scoring run on mined cases. The smoke runs on `datasets/intree_mini_v0`
(mechanics only, expressibility-only scorer) do not count as scoring runs.

## Question

Does teacher-forced perplexity of the reference edit, conditioned on a candidate
context, rank context quality correctly? If yes, PPL becomes the cheap dense proxy
for context selection (the expensive OCD-style oracle step becomes cheap). If no,
the method keeps only its deterministic half (expressibility gate +
obligation-symbol overlap + submodular coverage under budget).

## Fixed protocol

- Harness: `tools/run_ppl_signal_experiment.py`; scorer:
  `tools/ppl_context_scorer.py` `hf` backend, model `Qwen/Qwen2.5-Coder-0.5B`,
  CPU, deterministic, sha256 disk cache. Prompt template and conditions as
  implemented at the freezing commit.
- Sample: the first **200** cases from `datasets/mined_migration_bench` with
  ≥1 gold symbol, stratified by `treewide_scale` (mined by
  `tools/mine_migration_bench.py`, to be built — Step 0).
- Conditions per case, size-matched to `PPL_CONTEXT_BUDGET` (default 8000 chars):
  `gold`, `gold_distractors`, `distractors_only`, `random`, `none`.
- Provider/infra failures are excluded from denominators, never counted as losses.
- Metric: per-case pairwise wins on `nll_per_token` (lower wins); two-sided
  binomial sign test.

## Pre-registered criteria

- **SUCCESS**: PPL(gold) < PPL(random) in ≥ 70% of cases AND
  PPL(gold) < PPL(none) in ≥ 60% of cases, each with sign-test p < 0.01.
- **KILL**: PPL(gold) < PPL(random) win rate < 60% → drop PPL from the method;
  proceed with the deterministic half only.
- **Ambiguous (60–70%)**: exactly one retry allowed — swap to a ~1.5B model and
  score diff bodies without hunk headers — then apply the same thresholds.
  No other tuning (no prompt search, no threshold movement, no case filtering
  beyond the pre-stated ≥1-gold-symbol rule).

## Gate side-channel (validated independently of PPL)

Expected expressibility-gate profile: gold ≈ 100% pass; none/distractors low.
Observed smoke profile on intree_mini_v0 (5 cases, 2026-08-04): gold 0.978 mean
coverage / 80% pass vs none 0.654 / 0% — the one gold miss is a label-quality gap
(symbol absent from the case's kernel_evidence), which is exactly what the gate is
for. If the gold gate pass rate on mined cases is < 80%, fix the miner's labels
before interpreting any PPL result.

## Declared limitations

- A 0.5B ranking signal may understate what frontier models extract from context;
  this experiment tests whether the signal *exists*, not its final strength.
- Atomic-commit mining over-samples mechanical migrations; results are scoped to
  the mined case distribution.
- Length is controlled by size-matching; `nll_per_token` is the primary metric,
  `ppl` is reported for readability.
