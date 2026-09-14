# Context engineering — status, 2026-09-14

The first implementation package is on `codex/experiment-foundation`, based on
`ctx-impl` commit `041e60c`. Historical run artifacts and the frozen
pre-registration are unchanged.

## Current conclusion

The August 26 run completed: 200 cases, five conditions, 1000 scored rows.
Gold beats random in 140/200 cases (70%, including 44 ties in the denominator)
and none in 183/200 (91.5%). **The pre-registered stop-rule was tripped:**
gold passes the expressibility gate in only 116/200 cases (58%, required ≥80%).
These observations do not establish a successful pre-registered result or
validate PPL as a migration-quality proxy. The previous SUCCESS claim is
withdrawn. See [corrected result](06_ppl_signal_result.md).

The independent [gate audit](07_gate_audit.md) exactly reproduces all historical
rows before classifying failures. The gate checks identifier mentions, including
new driver-local names; a failure does not by itself prove missing kernel evidence.
The historical gate also used more old source than the scorer received.

The historical 40-case comparison found no selection advantage for `ours`, but
its backend, budget and oracle-boundary defects prevent attributing the outcome
solely to cases fitting the budget. It remains an archived diagnostic.

## First package

- Comparison v2: actual selection backend, model-token prompt budgets for HF,
  deterministic random ordering, one exact renderer for selection and scoring,
  explicit oracle methods, label-hidden ordinary selectors, and explicit
  insufficient-budget/window exclusions. Candidate pools are still evaluation-mined.
- Production pruner: native obligation fields, ranked whole-block selection,
  intact mandatory edit source, prompt-character accounting, directly connected
  scope expansion, per-block decisions outside the model input. Default remains OFF.
- Reproducibility: v2 manifests hash case-pack contents; legacy ID-only manifests
  remain readable with an explicit warning. New runs save initial and final code,
  input, backend and kernel identities, detect changes, and refuse output reuse.
- All 84 historical gold gate failures are audited separately from measurement
  changes. No reference-derived information is added to production prompts.
- Retro setup uses its existing kernel clone and cached CPU Qwen environment;
  the dirty `evolve-search` checkout is preserved. See [implementation/host report](08_experiment_foundation.md).

## Validation commands

```bash
python3 tools/run_ctxlab_checks.py
python3 tools/run_context_pruner_checks.py
python3 tools/run_provenance_checks.py
python3 tools/run_gate_audit_checks.py \
  --dataset-dir runs/gate_audit_input_20260914 \
  --manifest runs/oacd_results/ppl_sample_manifest.json \
  --audit-report runs/gate_audit_20260914/gate_audit.json
```

These check contracts and failures, not an assertion that a preferred method
must win. HF smoke tests demonstrate executable machinery, not effect sizes.
No new 200-case PPL run or end-to-end generated-patch experiment is part of
this package.

## Next TODO, in order

1. Resolve the gate's target definition using the audited cases: distinguish
   external requirements from newly introduced local identifiers; verify uncertain
   kernel evidence and extraction cutoffs. Do not tune labels to reach 80%.
2. Freeze a new PPL protocol: exact prompt/source, model tokens, actual
   length-matched controls, tie treatment, model revision, content hashes,
   commit-level dependence and explicit handling of oversized cases. Preserve the
   old pre-registration as history; write a new document before new scoring.
3. Build a split with genuine evidence-budget pressure. Separate production-derived
   candidate pools/obligations from evaluation oracles; split by migration commit
   before coefficient calibration and held-out evaluation.
4. Run the new PPL and method experiments on retro after those definitions are
   fixed. Report all exclusions and uncertainty; no win is assumed.
5. Evaluate patch generation at equal budgets with pruner OFF/ON, then module
   builds and applicable validation. PPL alone cannot establish patch correctness.

Evolve/fitness experiments stay a separate workstream until this foundation is
stable. No destructive reset of their checkout or historical worktrees is needed.

## Historical milestones

| Milestone | Commit | Report |
|---|---|---|
| M0 miner | `4167e4b` | [00](00_miner.md) |
| M1 data | `94b17e5` | [01](01_data_layer.md) |
| M2 metrics | `0a17e34` | [02](02_metric_layer.md) |
| M3 methods | `6a89b30` | [03](03_method_zoo.md) |
| M4 comparison | `ba77583` | [04](04_comparison_harness.md) |
| M5 analysis | `988dc83` | [05](05_analysis.md) |
| Frozen sample | `924a8b5` | unchanged pre-registration |

Reports 00–05 and the saved notebook describe earlier implementations; this
status and report 08 describe the current contracts.
