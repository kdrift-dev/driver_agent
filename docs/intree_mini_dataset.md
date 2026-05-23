# In-Tree Mini Dataset v0

## Purpose

This mini dataset is an auditable workflow-refinement set for Linux driver migration cases.
It is intentionally small: 10 in-tree driver modules with `modified_loc <= 50` for
`v6.19 -> v7.0-rc1`. Each case is manually selected because the target driver reference
patch is plausibly caused by kernel version evolution.

Input boundary for Driver-Agent remains strict:

- allowed: old target driver source
- allowed: old/new kernel source and kernel-side definitions
- disabled by default: kernel commit subjects/messages/diff excerpts
- forbidden as agent input: target driver new-version source/diff/reference patch
- forbidden by default: other-driver/collateral migration evidence

Reference driver diffs in the case packs are evaluation-only.

## Case Pack Contents

Run:

```bash
python tools/build_intree_mini_dataset.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --cases-csv /mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv \
  --out-dir datasets/intree_mini_v0 \
  --force
```

Generated artifacts:

- `manifest.csv` / `manifest.json`: case metadata and semantic keys.
- `cases/<case>/reference.diff`: upstream target driver diff for evaluation only.
- `cases/<case>/old_module_sources/`: old target module source snapshot.
- `cases/<case>/kernel_evidence.json`: kernel-side symbol locations and excerpts. Kernel commit info is disabled by default.
- `cases/<case>/reference.diff`: evaluation-only target driver diff; never use this as agent input.
- `cases/<case>/expected_semantic_keys.json`: lightweight correctness keys such as expected replacement symbols.
- `cases/<case>/workflow_artifacts.json`: imported Driver-Agent run summary when available.
- `kselftest_v0.csv` / `kselftest_v0.json`: kselftest-friendly candidate list.

## Result Analysis

After running Driver-Agent on any subset of the 10 cases, analyze run outputs with:

```bash
python tools/analyze_intree_mini_results.py \
  --dataset-dir datasets/intree_mini_v0 \
  --runs-dir runs \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --out-dir runs/intree_mini_v0_analysis
```

Outputs:

- `mini_spec_gap_table.csv`
- `mini_spec_gap_summary.json`
- `mini_spec_gap_report.md`
- `spec_gap_case_reports/<case>.md`

The analysis treats FM-Agent-style post reasoning as a consistency signal only. It checks whether the patch satisfies the generated obligation-specific local specs and stated obligations; it is not a correctness proof.
A generated patch is checked against semantic keys and function-level reference coverage for diagnosis.

Some mini-dataset cases contain source-compatible semantic modernization patterns, such as `kzalloc(...)` to `kzalloc_obj(...)`.
These are useful for workflow stress testing and paper case studies, but they should not be counted as required-repair failures when the old API remains valid and no kernel-side contract break is observed.
See `docs/semantic_modernization_case_study.md`.

## Correctness Metrics

Use these metrics together rather than treating any single one as sufficient:

- patch apply and no-op status
- compile status
- function-level reference overlap, diagnostic only
- semantic key overlap, e.g. new helper appears and old call pattern is removed
- obligation satisfaction from Driver-Agent reasoning
- FM-Agent-style post reasoning as stated-spec consistency only
- kselftest result when an in-tree test target exists
- QEMU boot and dmesg regression scan as smoke tests
- manual case-pack verdict for mini dataset v0
- future CBMC/KUnit assertions for local resource-pairing/error-path obligations

`post_reasoning=PASS` should never be reported as final correctness. It only means the generated patch is consistent with the obligations/specs that were actually given to the reasoner.

## kselftest Candidates

- `drivers/dma-buf/heaps`: best first candidate; targets `dmabuf-heaps` and `drivers/dma-buf`.
- `drivers/nfc/nxp-nci`: has `nci` selftests, but IRQ trigger semantics are hardware/DT-dependent; use as limitation evidence, not a clean proof.
- `drivers/block/zram`: strong kselftest coverage but outside `modified_loc <= 50`; keep in a separate kselftest-only dataset.
- `drivers/net/netconsole`: promising network namespace/QEMU candidate when a matching driver migration case exists.
- `devices/error_logs`, `devices/probe`, `devices/exist`: generic runtime smoke checks, not semantic proof.

## Related Work Positioning

The paper should not frame Driver-Agent as a generic SWE agent. The better framing is:

Driver-Agent studies kernel-evolution-grounded driver migration, using old driver usage and old/new kernel definitions to derive affected functions, obligations, specs, and constrained patch guidance.

Relevant work to position against:

- Coccinelle, USENIX ATC 2018: semantic patch language for Linux evolution.
- SPINFER, USENIX ATC 2020: semantic patch inference for Linux from examples.
- Prequel/gcc-reduce, USENIX ATC 2017: retrieval of forward/backporting information for Linux drivers.
- DSAC, USENIX ATC 2018, and DCUAF, USENIX ATC 2019: kernel driver static analyses that model context/lifecycle constraints.
- AutoCodeRover, ISSTA 2024, SWE-bench, ICLR 2024, and SWE-agent, NeurIPS 2024: repo-level LLM repair/agent baselines, but not kernel-evolution-specific.
- nl2postcondition, FSE 2024: relevant to the risk that generated specs are incomplete.

## Reviewer Feedback Mapping

Addressed or partially addressed:

- Data contamination: target new driver diff is evaluation-only and excluded from agent input.
- Similarity metric concern: patch similarity is diagnostic; semantic keys, compile, kselftest, and manual verdict are added.
- Evaluation clarity: case packs expose source, kernel evidence, reference diff, and manual causal notes.
- Design clarity: the workflow is scoped as evidence-grounded migration analysis, not a general terminal agent.

Still far:

- Dataset construction needs explicit deduplication, taxonomy derivation, annotation counts, and label agreement.
- Any LPA/GMA split must be removed or justified by data; sub-second latency should not be claimed without motivation.
- Static score weights should be removed or justified with ablation.
- FM-Agent-style spec is auxiliary patch guidance and consistency checking, not a proof of correctness without additional formal/dynamic/manual validation.
- Hardware-dependent runtime validation remains a limitation.
- Source-compatible semantic modernization should be reported separately from required compatibility repair.
