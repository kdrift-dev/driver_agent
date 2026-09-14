# Driver-Agent Workflow

Driver-Agent is a Linux-driver migration workflow for adapting old driver code to a newer kernel version. It is intentionally **not** a generic repository repair agent. The paper-facing input boundary is:

```text
old target driver source
+ old/new Linux kernel source
+ kernel-side definitions/diffs used by the old driver
```

The workflow must not use the target driver's new-version diff as agent input. Reference diffs are allowed only for evaluation and manual case-pack labeling.

## What The Pipeline Does

Driver-Agent currently supports:

- Pre-generation migration analysis.
- Kernel-side definition location and diff evidence extraction.
- Affected-function discovery with edit-scope tiers.
- Obligation-specific FM-Agent-style local specs for patch guidance.
- Patch generation with optional repair iterations.
- Validation as a layered evidence vector, not as a single proof of correctness.
- In-tree and out-of-tree driver validation paths.
- Four-layer experiments for comparing source-only baselines, kernel-evidence baselines, and the full Driver-Agent pipeline.

The public/default workflow uses kernel-side evidence only. Collateral driver migration examples, semantic-obligation tables built from other drivers, target new-driver diffs, and hand-written case-specific replacement hints are disabled by default.

## Case Definition

### In-Tree Cases

An in-tree case is preferably scoped at the **Kbuild module target** level:

```text
(module_path, kbuild_target, old_ref, new_ref)
```

Example:

```text
module_path   = drivers/block/zram
kbuild_target = zram.ko
old_ref       = v6.19
new_ref       = v7.0
```

This is stricter than directory-only cases. A directory may build multiple `.ko` modules, so using only `module_path` can mix sibling drivers and inflate source scope. In Kbuild-target mode, Driver-Agent resolves the target's owning source files from the old tree Makefile/Kbuild and follows their literal relative quoted includes to load local headers. It does not recursively collect headers from sibling drivers. Unresolved local includes remain explicit scope risks; the strict evidence-pool adapter rejects them.

If a directory contains exactly one relevant target, directory mode is still supported.

### Out-of-Tree Cases

An OOT case is scoped to either an OOT repository or a selected subdirectory/module inside that repository, plus an old source ref and target Linux kernel ref. OOT validation is integrated into the same generation/validation framework, but may additionally use compile diagnostics and explicit QEMU smoke scripts.

## Main Entry Point

```bash
python pregen_driver_agent.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0 \
  --module-path drivers/block/zram \
  --kbuild-target zram.ko \
  --out-dir runs/pregen \
  --jobs 8 \
  --kernel-config-target allmodconfig \
  --enable-llm \
  --llm-provider openrouter \
  --llm-model anthropic/claude-sonnet-4.6 \
  --config-ini /mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini \
  --patch-candidates 1 \
  --max-patch-iterations 3 \
  --patch-source-mode focused \
  --patch-prompt-strength strict \
  --enable-qemu \
  --qemu-timeout-sec 1800 \
  --cleanup-worktrees
```

Use `--dry-run` to persist prompts/inputs/artifacts without calling an LLM or generating a patch.

For extraction without builds, use `tools/extract_kernel_evidence.py`. It runs
only the four production extraction stages and guards source access and process
execution. The ordinary CLI's `--dry-run` can still enter validation when a
candidate exists. See the [NTB preparation protocol](docs-impl/27_ntb_preparation_protocol.md).

For offline paired kernel-evidence selection, reuse a production extraction:

```bash
python3 tools/prepare_paired_context.py \
  --extracted-dir /path/to/run/extracted \
  --repo /path/to/linux \
  --out-dir runs/paired_context_example \
  --generation-model anthropic/claude-sonnet-4.6 \
  --counter message_chars
```

This opt-in tool saves full old source, one verified kernel-evidence pool and
first-fit/ranked requests under a shared cap. It never sends model requests.
Built-in character and cached HF counters are diagnostics and refuse generation
without verified provider/model/framing binding. See the
[paired protocol](docs-impl/16_paired_context_protocol.md) and
[current results](docs-impl/17_paired_context_results.md).

A separate opt-in `tools/run_paired_character_pilot.py` supports at most two
real requests under the frozen character cap. Its default only prepares files;
`--execute` is required for generation. This diagnostic preserves the strict
token-admission refusal and reports actual usage afterward. See its
[protocol](docs-impl/18_paired_character_pilot_protocol.md) and
[observed format/patch failures](docs-impl/19_paired_character_pilot_results.md).

The subsequent opt-in `tools/run_structured_edit_pilot.py` requests schema-bound
JSON edits and constructs diffs from unique exact source spans. It also defaults
to preparation only and limits `--execute` to two attempts. Its whole-line v1
contract rejected both observed edit sets; no generated patch reached a build.
See the [protocol](docs-impl/20_structured_edit_pilot_protocol.md) and
[results](docs-impl/21_structured_edit_pilot_results.md).

The same runner accepts `--edit-contract exact_substrings_v2` for unique exact
substring edits; v1 remains the default. Both observed v2 outputs contain a
no-op and remain rejected. A separate evaluation-only helper,
`tools/prepare_identity_edit_forensics.py`, preserves and interprets their entire
raw edit lists with identity operations allowed. Two strict module builds pass
under that explicitly post-hoc policy. See the
[v2 protocol](docs-impl/22_exact_substring_pilot_protocol.md),
[post-hoc policy](docs-impl/23_identity_edit_forensic_protocol.md) and
[combined results](docs-impl/24_substring_and_identity_results.md).

The explicit `identity_substrings_v3` contract now retains and validates every
submitted operation, including identities. Identity-only output has its own
status and produces no patch or build candidate. This production policy uses
generic checks and does not change earlier results. Its runner still requires
the existing preparation anchor; fresh cases need qualification and their own
frozen input protocol. See the [v3 contract](docs-impl/25_identity_edit_contract.md).

## LLM Configuration

Supported providers include:

- `openrouter`
- `deepseek`
- `openai`

Credentials should be supplied through environment variables or `config.ini`. Do not commit real API keys.

Example `config.ini` keys:

```ini
OPEN_ROUTER_KEY=...
DEEPSEEK_API_KEY=...
```

For paper-facing experiments, use the configured paper model consistently. If the provider returns an auth/quota error, that run should be labeled as provider failure, not as a model or pipeline patch failure.

LLM stages retain `request_body.json` and `provider_audit.json` with response
metadata and per-attempt accounting. Configured retries are explicit; timeouts
or worker crashes may leave the exact request count unknown. Check truncation
and provider status separately from JSON parse success.

## Pipeline Stages

A normal run writes all intermediate artifacts under:

```text
runs/pregen/<case_id>/<timestamp>/
```

Main stages:

1. `module_source_loader`
   - Reads old target source only.
   - In Kbuild-target mode, resolves target-owned sources from old Makefile/Kbuild.
   - Records whether the same path exists in the new kernel, but does not use the new driver source.

2. `symbol_scanner`
   - Extracts driver-local functions, kernel-side symbol usage, includes, callbacks, and an approximate call graph.
   - Distinguishes driver-local symbols from kernel-side symbols when possible.

3. `definition_locator`
   - Locates old/new definitions or declarations for non-local kernel symbols.
   - Uses lightweight source search fallbacks when stronger tooling is unavailable.

4. `kernel_diff_builder`
   - Builds focused kernel-side evidence for symbols actually used by the old driver.
   - Detects removed symbols, include dependency changes, struct/field changes, and multi-line function signature changes.
   - Kernel commit information is disabled by default.

5. `impact_graph_builder`
   - Builds function-to-symbol impact edges.
   - Produces edit-scope tiers:
     - `edit_required`: direct changed-symbol use with an evidence-backed obligation.
     - `edit_candidate`: caller/callee/resource-paired context that may need edits.
     - `context_only`: lifecycle/callback/resource context for reasoning only.

6. `obligation_builder`
   - Converts kernel-side changes into deterministic compatibility obligations.
   - Outputs hard checks, semantic checks, confidence, and evidence.

7. `pre_spec` and `pre_reasoner`
   - Generate obligation-specific local specs and pre-patch guidance.
   - These specs are guidance/consistency artifacts, not correctness proofs.

8. `patch_generation`
   - Generates a unified diff patch candidate.
   - Supports focused source mode by default.
   - Applies line-number/hunk repair to model output.

9. `validation`
   - Applies the patch to an old-ref worktree.
   - Stages patched files into a new-ref worktree.
   - Builds the target directory or Kbuild target.
   - Runs deterministic runtime hooks when available.
   - Optionally performs repair iterations using compile/runtime feedback.

10. `report`
   - Writes context packets and markdown/JSON reports.

## Patch Source Modes

`--patch-source-mode focused` is the default paper-facing mode.

Focused mode provides:

- Editable function slices for `edit_required` and supported `edit_candidate` functions.
- Read-only caller/callback/resource context.
- Line-numbered snippets for patch localization.
- Full file/module source only when focused extraction is empty or when explicitly requested.

`--patch-source-mode full_module` provides full old target-module source files. Use it for debugging, difficult multi-file migrations, or ablations; it is noisier and can encourage unnecessary edits.

## Patch Iteration

Enable iterative repair with:

```bash
--max-patch-iterations 3
```

Each iteration is a new LLM call with the previous patch-generation metadata and validation feedback. It is not a persistent chat session. Artifacts are saved under:

```text
llm_calls/03_patch_generation/
llm_calls/03_patch_repair_iter_02/
llm_calls/03_patch_repair_iter_03/
patch_iterations/
```

If patch generation fails because the provider returns an API/auth/quota error, the run should be interpreted as provider failure. It should not be counted as a no-op success or a semantic model failure.

## Four-Layer Experiments

Use `tools/run_four_layer_experiment.py` to compare input/control settings on the same case:

- `P0`: problem reproduction. Old target source staged into the new kernel must fail.
- `B0`: source-only LLM baseline.
- `B1`: old source plus compact kernel evidence baseline.
- `D`: full Driver-Agent pipeline.

Example:

```bash
python tools/run_four_layer_experiment.py \
  --case-kind intree \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0 \
  --module-path drivers/block/zram \
  --kbuild-target zram.ko \
  --out-dir runs/four_layer_zram \
  --jobs 8 \
  --kernel-config-target allmodconfig \
  --enable-llm \
  --llm-provider openrouter \
  --llm-model anthropic/claude-sonnet-4.6 \
  --config-ini /mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini \
  --patch-candidates 1 \
  --max-patch-iterations 3 \
  --patch-source-mode focused \
  --patch-prompt-strength strict \
  --methods B0,B1,D \
  --runtime-policy d-only \
  --qemu-timeout-sec 1800 \
  --layer-timeout-sec 10800 \
  --cleanup-worktrees
```

`P0` is always evaluation/setup evidence, not agent input.

## Validation Philosophy

Linux driver correctness has no single universal oracle. Driver-Agent therefore reports validation as an evidence vector.

Current evidence levels:

- Level 0: patch sanity, no-op detection, staged files, edit-scope policy.
- Level 1: target module build correctness.
- Level 2: deterministic runtime hooks, kselftest/KUnit/QEMU smoke when registered.
- Level 3: deterministic migration obligation checks for supported symbol/edit patterns.
- Level 4: spec consistency via FM-Agent-style post reasoning.
- Level 5-7: placeholders for static checker, CBMC/formal checks, and evaluation-only/manual evidence.

Important caveat:

```text
spec_consistency PASS means the patch satisfies stated/generated local obligations.
It does not prove that the generated spec is complete or that the driver is globally correct.
```

Runtime validation is meaningful only when the registered hook actually exercises the target behavior. Hardware-dependent drivers may only get build/smoke evidence in QEMU.

## Runtime Hooks

Driver-Agent can run deterministic or explicit runtime hooks:

- In-tree kselftest targets.
- In-tree KUnit targets.
- QEMU boot smoke tests.
- OOT custom in-guest shell scripts.
- OOT deterministic load/unload or device-node smoke.

LLM-generated tests are disabled by default and are not part of the paper-facing correctness metric. Enable them only for private/debug exploration with:

```bash
--enable-llm-test-generation
```

Host-side suite execution is disabled by default because generated or subsystem tests may invoke privileged operations. Use QEMU/KUnit hooks or explicit scripts for controlled runtime validation.

## Output Tree

```text
runs/pregen/<case_id>/<timestamp>/
  metadata.json

  extracted/
    module_source.json
    old_module_file_list.txt
    old_module_sources/
    driver_used_symbols.json
    driver_includes.json
    driver_functions.json
    driver_callbacks.json
    call_graph.json
    kernel_definition_locations.json
    kernel_definition_diffs.json
    kernel_relevant_diff.patch
    impact_graph.json
    callback_graph.json
    compatibility_obligations.json
    affected_function_slices/

  llm_calls/
    01_pre_spec/
    02_pre_reasoner/
    03_patch_generation/
    03_patch_repair_iter_02/
    03_patch_repair_iter_03/
    04_post_reasoner/
    05_system_test_generation/

  patch_runs/
    <candidate_id>/
      01_raw_patch.diff
      02_fixed_patch.diff
      patch_fix_report.json

  patch_iterations/
    iter_01/
    iter_02/
    iter_03/

  validation/
  patch_generation.json
  patch_iterations.json
  validation.json
  validation_summary_v2.json
  validation_summary_v2.md
  context_packet.json
  context_packet.md

  reports/
    pre_generation_report.md
    pre_generation_report.json
```

## Datasets And Case Catalogs

Current dataset directories live under `datasets/`.

Useful datasets include:

- `intree_kbuild_target_hardbreak_20_v0`: Kbuild target-level hard-break cases.
- `intree_virtual_qemu_hardbreak_5_v0`: virtual/software in-tree drivers with QEMU-oriented validation potential.
- `intree_showcase_5_v0`: selected in-tree showcase candidates.
- `oot_showcase_5_v0`: selected out-of-tree showcase candidates.
- `oot_hardbreak_small_10_v0`: smaller OOT hard-break candidates.

Dataset artifacts may include reference diffs and manual notes. Those are evaluation-only and must not be passed into the agent pipeline.

## Validation Summary Tool

Rebuild a validation evidence summary for an existing run:

```bash
python tools/build_validation_summary_v2.py \
  --run-dir runs/pregen/<case_id>/<timestamp>
```

For read-only or root-owned runs:

```bash
python tools/build_validation_summary_v2.py \
  --run-dir runs/pregen/<case_id>/<timestamp> \
  --out-dir runs/validation_summary_v2
```

## Spec Gap Analysis

Use `tools/analyze_spec_gap.py` to audit cases where generated specs/reasoning may be incomplete or misleading. This is an evaluation harness; it may use reference diffs, but those diffs must not become agent input.

```bash
python tools/analyze_spec_gap.py \
  --runs-dir workflow/runs/pregen_small8_20260511_152828 \
  --reference-root /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --modules-file workflow/runs/pregen_small8_20260511_152828/modules.txt \
  --out-dir workflow/runs/spec_gap_analysis_20260512_8cases \
  --emit-8case-table
```

Typical outputs:

- `spec_gap_cases.csv`
- `spec_gap_summary.json`
- `spec_gap_report.md`
- `case_reports/<case_id>.md`
- `8case_spec_gap_table.csv`

For targeted affected-function misses:

```bash
python tools/analyze_affected_miss.py \
  --baseline-dir workflow/runs/spec_gap_analysis_20260512_8cases_v2 \
  --out-dir workflow/runs/affected_miss_analysis
```

## Paper-Facing Policy

- Default input: old target driver source plus old/new kernel-side definitions/diffs.
- Default no: target new-driver diff, other-driver migration examples, hand-written per-case hints.
- Kernel commit messages/diffs are disabled by default.
- Reference diffs are evaluation-only.
- LLM-generated tests are disabled by default.
- Post reasoning is `spec_consistency`, not correctness proof.
- Report skipped runtime validation separately from passed runtime validation.

See also:

- `docs/paper_requirements.md`
- `docs/reviewer_feedback_response.md`
- `docs/reviewer_feedback_matrix.csv`

## Implementation Notes

- Symbol scanning, call graph extraction, and callback detection are currently lightweight and regex/tree-sitter-style where available.
- Definition lookup uses source search fallbacks and is optimized for old/new git refs.
- Multi-line kernel function prototypes are compared at the signature-block level, not only by first matched line.
- Kbuild target compilation may fall back to directory module builds when explicit target invocation is not supported by the local Kbuild command shape.
- `make defconfig` may not enable the target module. Use `--kernel-config-target allmodconfig` for cases such as `zram` where target coverage matters.
- QEMU validation may require root or a narrow sudo rule for the pipeline/QEMU commands.
