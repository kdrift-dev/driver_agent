# Context engineering — status, 2026-09-14

The current package is on `codex/identity-contract-ntb`: production identity
contract `87b7aa2`, owned-source extraction/module controls `c092786`, and
explicit excerpt provenance `f112edc`. It follows exact-substring v2
generation `0e5ed41` and a separate post-hoc identity evaluator `de8ef8f`
on `codex/exact-substring-pilot` (report `bfce019`).
It follows `codex/structured-edit-pilot` (`6c9fac6`, report `5f4ef23`),
`codex/paired-character-pilot` (`e13e895`, report `271eb2c`) and `codex/paired-context-budget`
(`446e42c`, report `c452c18`), `codex/xilinx-module-pilot` (`9e1f7bd`, report
`921b9af`), the build-eligibility package (`d02ed15`, Nix `c69f0b3`, report
`22b0604`), gate v2 (`431c7a1`, report `e63f482`) and foundation (`12940c0`,
report `0c7e6be`).
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

The scoped Xilinx continuation now has **three strict positive `.ko` controls
and one reproduced old-on-new compiler API mismatch**. Two independently built
kernels use byte-identical configurations; all module imports resolve, and
restoring the new source reproduces the new `.ko` byte for byte. This extends
the Xilinx evidence to a complete module build, with CRC versioning and runtime
explicitly untested. See [module controls](14_xilinx_module_controls.md).

The earlier production dry-run shows that the
native pruner chooses read-only old-source blocks, while OFF/ON also change
source retention and budget accounting. All six optional focused blocks have
zero obligation overlap in this case. A direct flag comparison would therefore
not isolate useful kernel-evidence selection. Real generation prompts also
exceed the earlier 4096-token comparison budget under its Qwen diagnostic
counter; those counts cannot transfer to a different generation model.
See [measured preparation and prerequisites](15_xilinx_generation_protocol.md).

The separate paired harness now holds full old source, kernel-evidence pool,
renderer and budget accounting constant. On Xilinx, 62 verified optional blocks
produce different first-fit/ranked sets under the same cap. Both retain the
changed API block. Independent replay passes 1731 checks and reproduces 3986
complete-message counts/selection variants. The Qwen-framed diagnostic counts
are 9033/8966 under a 9153-token cap; they are not Claude input-token counts.
Strict token-budget admission remains **PROVIDER_COUNTER_BINDING_REQUIRED**.
The subsequent character-budget diagnostic is reported separately below. See [protocol](16_paired_context_protocol.md)
and [results](17_paired_context_results.md).

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

## Fourth package

- Xilinx's full old target is one C file, 430 lines, with no local headers.
  Two fresh kernel builds and four module phases complete on retro. Three
  strict `.ko` controls pass; old-on-new fails on the changed public API type.
  All 231 independent artifact-audit checks pass; historical runs stay intact.
- Production dry-run and seven diagnostic re-renders expose the current flag's
  source-truncation and accounting confounds. No model requests or generated
  patches exist. An independent source/provenance audit checks 93 artifacts and
  126 kernel excerpts; it finds no prohibited reference or driver-diff input.
- Two new oracle contract groups pass locally and on retro. Production code,
  pruner defaults, the reserved pressure set and previous pre-registration are
  unchanged. Build checkout: `/home/arina/work/driver_agent_xilinx_pilot`.
- Machine-readable catalogs: [module controls](../datasets/xilinx_module_controls_v1.json)
  and [generation preparation](../datasets/xilinx_generation_preparation_v1.json).

## Fifth package

- A strict production adapter admits only old target source and verified
  kernel-side excerpts from allowlisted extraction documents. Both conditions
  use the same full mandatory source, minimal obligation signal and pool order.
- One request builder and full-message counter serve first-fit and ranked
  selection. Every trial re-renders the complete messages; overflow preserves
  source and refuses dispatch. The native source-pruner default remains OFF.
- Provider audits retain exact request bodies, response/model/usage/finish
  metadata and actual attempts. SDK retries and worker-error resubmission are
  disabled; timed-out or crashed calls retain explicit count uncertainty.
- Ten client groups, 42 evidence checks, three paired-context groups and the
  existing pruner/ctxlab checks pass locally and on retro. Independent audit
  reproduces both canonical runs; all 93 prior dry-run artifacts are unchanged.
- Current experiment checkout: `/home/arina/work/driver_agent_paired_context`.
  This package uses no model inference, kernel rebuilds or runtime tests.
  See the [catalog](../datasets/paired_context_preparation_v1.json).

## Sixth package

- Two real Sonnet 4.6 requests completed with Anthropic-only routing, zero
  retries, unchanged character-arm messages and a common 26,532-character cap.
  Actual input usage was 11,639/11,475 tokens; total reported cost USD 0.094692.
- Both outputs were rejected for prose/fenced JSON. Separate post-hoc validation
  of the unmodified embedded diffs also rejected both: hunk coordinates 56/57
  do not match the exact source context starting at 60. No repairs, further
  generation, compiler/link steps or runtime tests followed these failures.
- All 13 new offline check groups pass locally and on retro. The independent
  generation audit verifies the freeze, two invocations and original inputs.
  This establishes an executed diagnostic, not a method advantage or successful
  patch count. The strict token-budget flags remain false.
- Current checkout: `/home/arina/work/driver_agent_character_pilot`.
  See [protocol](18_paired_character_pilot_protocol.md),
  [results](19_paired_character_pilot_results.md) and
  [catalog](../datasets/paired_character_pilot_v1.json).

## Seventh package

- Structured JSON and an exact-edit compiler are implemented and frozen before
  two real Sonnet calls. Both responses satisfy the JSON schema; both violate
  the whole-line edit contract by omitting final LF. Ranked also emits a no-op.
  Both stay rejected, with no generated diff, apply, compilation, link or runtime.
- The common messages/schema character cap is 27,399; selected inputs are
  27,183/26,803 characters and actual input usage is 12,030/11,866 tokens.
  Total reported cost is USD 0.089613. The token-budget admission flags stay false.
- All 17 focused offline check groups pass locally and on retro. Independent
  generation audit passes 596 checks. Read-only preflight found and fixed the
  private-copy kernel-release suffix issue; no real generated-patch build
  exercised that change in this package. Previous runs and source boundaries are preserved.
- Current checkout: `/home/arina/work/driver_agent_structured_edits`.
  See [protocol](20_structured_edit_pilot_protocol.md),
  [results](21_structured_edit_pilot_results.md) and
  [catalog](../datasets/structured_edit_pilot_v1.json).

## Eighth package

- Exact-substring v2 preserves source bytes outside unique original spans and
  keeps v1 unchanged. Both fresh model responses contain the same substantive
  type change plus a no-op; canonical v2 rejects both. The common cap is 27,598
  characters, input usage 12,079/11,915 tokens and total reported cost USD 0.090792.
- A separately frozen post-hoc policy executes each entire original edit list,
  including identity operations. Both private validations pass exact apply,
  fresh compilation and strict module linking. The two `.ko` files are identical,
  23,176 bytes each; donor, kernel/config and release remain unchanged. Physical
  runtime and symbol CRC versioning are untested. No additional model calls occur.
- All 31 focused check groups pass across the two code packages. Independent
  audits bind original responses, unchanged canonical failures and literal
  post-hoc reconstruction. This does not establish a selection advantage or
  equal model-token budgets. See [results](24_substring_and_identity_results.md),
  [v2 protocol](22_exact_substring_pilot_protocol.md),
  [post-hoc protocol](23_identity_edit_forensic_protocol.md) and
  [catalog](../datasets/substring_edit_pilot_v2.json).
- Current checkout: `/home/arina/work/driver_agent_substring_edits`.

## Next TODO, in order

1. Extend production evidence coverage to kernel declarations of functions
   defined by the OLD module. NTB's verified pool has 99 blocks but no changed
   requirements; with zero overlap, ranked selection reduces to size ordering.
   Validate any coverage change using OLD target and OLD/NEW kernel-side
   evidence, keeping NEW target source and build diagnoses evaluation-only.
   The explicit v3 identity contract is implemented; do not rerun Xilinx just
   to exercise it. Preserve canonical v1/v2 failures and post-hoc successes
   separately. Qualify more independent commits before method comparison.
   A future NTB dispatch needs a frozen case/input anchor and meaningful
   evidence coverage; the structured runner still retains its Xilinx anchor.
   For a confirmatory token-budget comparison,
   a verified provider/model counter remains required; OpenRouter exposes no
   documented pre-generation token counter. Character caps and post-hoc usage
   must not be relabeled as equal model-token budgets. Acquire more qualified
   migration commits before statistical comparison; preserve the pressure set.
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
5. Evaluate the frozen context-selection conditions at equal budgets, then
   module builds and applicable validation. Report apply, compilation, link,
   semantic review and runtime separately. PPL alone cannot establish patch correctness.

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
status and reports 08–29 describe the current contracts.
