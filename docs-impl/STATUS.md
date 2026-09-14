# Context engineering — status, 2026-09-14

The current package is on `codex/pressure-build-eligibility`: runner `d02ed15`,
corrected Nix environment `c69f0b3`. It follows `codex/gate-v2-pressure`
(`431c7a1`, report `e63f482`) and `codex/experiment-foundation`
(`12940c0`, report `0c7e6be`).
Historical run artifacts and the frozen pre-registration are unchanged.

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

Gate v2 and a source-backed calibration review are now implemented. The review
covers 24 commits and 223 occurrences, but is sparse and assistant-authored.
With complete gold context, the diagnostic gate reports 10 fail / 14 unknown /
0 pass. It is not yet a confirmatory quality filter; the old stop-rule remains
tripped. See [gate definition](../docs/gate_v2_definition.md) and
[current results](11_gate_v2_pressure_results.md).

The canonical context-pressure census on retro is frozen: 48 eligible cases
at 4096 prompt tokens, split into 37 calibration and 11 reserved cases
(29 and 10 migration commits). At 1024/2048 tokens the counts are 7/18.
Commit assignments are consistent across budgets, and all historically scored
commits are forced to calibration. The [catalog](../datasets/context_pressure_v1.json)
pins the six selections and their artifacts. This is a token-budget dataset;
production retrieval remains unverified.

The reserved 11-case build census is complete: **eight old files compile on
the target kernel, two have controlled compiler errors, one was removed**.
Xilinx has a changed API argument type; NTB fails on two removed prototypes
under the warning-as-error policy. All 30 pristine/restoration controls pass.
This establishes file-object evidence only, not module linking or runtime
correctness. The [build report](13_pressure_build_results.md) and
[result catalog](../datasets/pressure_build_results_v1.json) record all outcomes.
The pressure set is not yet a required-repair benchmark for method comparison.

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

## Second package

- Gate v2 distinguishes confirmed external requirements, proven local names,
  lexical exclusions and unresolved occurrences. It uses exact input hashes,
  strict patch reconstruction, occurrence-specific annotations and conservative
  scope checks. Historical gate and production defaults are unchanged.
- The source review verifies all 223 judgments against packed input and Git
  source locations; five negative controls detect invalid annotation bindings.
  Reviewed sibling-driver dependencies remain evaluation-only.
- Context-pressure v1 counts exact full-source prompts using the pinned cached
  tokenizer. Split assignment precedes eligibility; selection uses no gate,
  PPL, win-rate, or gold-presence filter. Canonical output and all input hashes
  verify on both retro and the local copy. No inference is used.
- Current experiment checkout: `/home/arina/work/driver_agent_gate_v2`.
  The previous foundation and evolve checkouts are preserved.

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
either package. For the second package, 45 existing fixture checks, 34 new gate
checks, three pressure contract groups, the source-review verifier, and the
canonical artifact verification pass. Commands and results are in reports
[09](09_gate_review.md), [10](10_context_pressure_set.md), and
[11](11_gate_v2_pressure_results.md).

## Third package

- Exact-parent Kbuild/Kconfig mapping covers all 11 reserved cases. The runner
  uses fresh worktrees, active resolved configuration, old/pristine/restored
  controls, verified packed Git bytes and fresh ELF/compiler-command evidence.
- The corrected retro run completed 40 source compilations, with 38 successful
  objects and two old-on-new compiler failures. Source, configuration and
  artifact hashes are retained. GCC latent-entropy binary variation is recorded.
- Three new synthetic contract groups pass locally and on retro. The initial
  environment failure is preserved; host GCC plugin development headers were
  fixed without changing driver code or case configuration. Nix checks pass for
  the changed file.
- Build checkout: `/home/arina/work/driver_agent_build_eligibility`. No model,
  PPL, module-link or runtime experiments were run in this package. See
  [protocol](12_pressure_build_protocol.md) and [results](13_pressure_build_results.md).

## Next TODO, in order

1. Take the Xilinx API mismatch into a scoped pilot: complete the old-target
   source boundary and strict module-link controls, then freeze actual
   generation prompts, budgets, model and validation before patch generation.
   Keep NTB's removed-prototype diagnostic separate; acquire more qualified
   migration commits before statistical comparison. Preserve the pressure set.
2. Complete gate calibration before using it as a quality filter: extend sparse
   occurrence review and unsupported scope/preprocessing handling, distinguish
   extraction misses from unnecessary requirements, and retain explicit unknowns.
   Do not tune labels or definitions to recover the historical 80% threshold.
3. Freeze a new PPL protocol: exact prompt/source, model tokens, actual
   length-matched controls, tie treatment, model revision, content hashes,
   commit-level dependence and explicit handling of oversized cases. Preserve the
   old pre-registration as history; write a new document before new scoring.
4. Separate production-derived candidate pools/obligations from evaluation
   oracles, then run new PPL/method experiments on the appropriate frozen
   calibration set. Respect commit-level separation and report all exclusions.
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
status and reports 08–13 describe the current contracts.
