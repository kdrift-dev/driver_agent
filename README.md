# Driver Agent Pipeline

`driver_agent_pregen` now supports both:
- pre-generation migration analysis
- patch generation + validation loop (apply, compile, post-reason, test generation, optional qemu command execution)

## Scope

Implemented now:
- stage 1..10 pre-generation pipeline
- git repo + old/new ref as first-class input
- old/new source tree fallback mode
- dry-run LLM support with full artifact persistence
- patch generation agent (`llm_calls/03_patch_generation` + patch line fixer)
- validation agent:
  - apply patch on old ref worktree
  - stage patched files into new ref worktree
  - compile module directories
  - post-patch reasoner (`llm_calls/04_post_reasoner`) as stated-spec consistency check, not proof of correctness
  - system test generation (`llm_calls/05_system_test_generation`)
  - optional suite command execution / qemu-triggered command capture

## Layout

```text
workflow/
  pregen_driver_agent.py
  driver_agent_pregen/
    __init__.py
    cli.py
    repo_utils.py
    module_source_loader.py
    symbol_scanner.py
    definition_locator.py
    kernel_diff_builder.py
    impact_graph_builder.py
    obligation_builder.py
    llm_client.py
    prompts.py
    spec_agent.py
    reasoner_agent.py
    patch_line_fixer.py
    patch_generation.py
    validation.py
    context_packet.py
    report.py
    utils.py
```

## Usage

### Preferred: git repo + old/new refs

```bash
python pregen_driver_agent.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --module-path drivers/dax/hmem \
  --out-dir runs/pregen \
  --jobs 16 \
  --enable-llm \
  --llm-provider openrouter \
  --llm-model anthropic/claude-sonnet-4.6
```

DeepSeek is also supported through its OpenAI-compatible chat API:

```bash
python pregen_driver_agent.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0 \
  --module-path drivers/dax/hmem \
  --out-dir runs/pregen \
  --jobs 8 \
  --enable-llm \
  --llm-provider deepseek \
  --llm-model deepseek-v4-pro \
  --config-ini /mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini
```

Set `DEEPSEEK_API_KEY=...` or `DEEPSEEK_KEY=...` in `config.ini` or the environment.

### Dry-run

```bash
python pregen_driver_agent.py \
  --repo /path/to/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --module-path drivers/dax/hmem \
  --out-dir runs/pregen \
  --dry-run
```

### Optional runtime execution switches

- `--patch-candidates N`: request N patch candidates from LLM (default `1`)
- `--patch-generation-mode {agent|baseline_source_only|baseline_kernel_evidence}`:
  - `agent` (default): current Driver-Agent patch input with obligations, affected graph, pre-reasoning, context packet, and old source.
  - `baseline_source_only` (B0): fixed baseline prompt with old target-module source only; no kernel evidence, affected graph, obligations, specs, or reasoning.
  - `baseline_kernel_evidence` (B1): fixed baseline prompt with old target-module source plus compact kernel-side evidence; no affected graph, obligations, specs, or reasoning.
  - All modes use the same patch post-processing, line fixing, apply/compile/post-reasoning validation path.
- `--patch-source-mode {full_module|focused}`:
  - `focused` (default): provide edit-scope function slices plus read-only caller/callback/resource context.
  - `full_module`: provide full old-module source files; use as fallback/debug mode, not the paper-facing default.
- `--patch-source-with-lineno` / `--patch-source-no-lineno`:
  - control whether patch-generation source snippets include `L<line>:` prefixes for location hints.
- Affected-function edit scopes:
  - `edit_required`: direct changed-symbol use with evidence-backed obligation; default edit target.
  - `edit_candidate`: caller/callee/resource-paired context; editable only with cited candidate evidence.
  - `context_only`: lifecycle/callback/resource context for reasoning; not editable by default.
- `--include-kernel-commit-info`:
  - disabled by default; enables sanitized kernel commit subjects/messages/diff excerpts for private/debug ablations only.
  - paper-facing runs should leave this disabled.
  - synthetic replacement hints and case-specific patch fixups are also disabled by default and require private/debug environment flags.
- `--enable-qemu`: allow validation stage to execute suite commands that invoke `kunit.py run` / `qemu-system-*`
- `--qemu-timeout-sec SEC`: timeout for suite/qemu commands (default `3600`)
- `--skip-system-test-generation`: skip LLM system-test generation and suite/qemu execution (still runs patch apply/compile/post-reasoning)
- `--enable-host-suite`: explicitly run LLM-generated suite commands on the host. This is disabled by default because generated driver suites may contain `modprobe`, `mount`, suspend/resume, or hardware-specific operations. QEMU/KUnit execution only runs standalone `kunit.py run` / `qemu-system-*` commands.
- `--cleanup-worktrees`: remove temporary old/new git worktrees after validation

Out-of-tree QEMU validation:

- `--oot-pipeline-mode integrated`: route OOT source through the full Driver-Agent analysis/generation/validation pipeline.
- `--oot-qemu-check-script PATH`: use a caller-provided in-QEMU shell script.
- `--oot-qemu-test-mode {auto|custom|llm|default}`:
  - `auto` (default): use custom script if provided, otherwise generate an LLM in-QEMU script when LLM is enabled, otherwise use deterministic fallback.
  - `llm`: generate an in-QEMU script from patched OOT source and evidence; invalid scripts fall back to deterministic smoke.
  - `default`: deterministic load/unload or device-node smoke.
- `--oot-qemu-test-level {load_unload|interface_smoke|semantic_scenario}`: request the depth of generated OOT runtime validation.
- Generated OOT scripts are saved under `validation/oot_qemu_generated_check.sh` and their prompt/input/output under `llm_calls/05_oot_qemu_test_generation/`.

FM-Agent-style specs:

- `01_pre_spec` generates obligation-specific local specs for patch guidance.
- `02_pre_reasoner` and `04_post_reasoner` check consistency with stated obligations/specs.
- A post-reasoning `PASS` is not a proof of full driver correctness; correctness must be reported with compile, semantic keys, kselftest/QEMU when meaningful, CBMC/KUnit/manual verdict, or other external evidence.

## Output Tree

```text
runs/pregen/<case_id>/<timestamp>/
  metadata.json

  extracted/
    ... pregen artifacts ...

  llm_calls/
    01_pre_spec/
    02_pre_reasoner/
    03_patch_generation/
    04_post_reasoner/
    05_system_test_generation/

  patch_runs/
    <candidate_id>/
      01_raw_patch.diff
      02_fixed_patch.diff
      patch_fix_report.json

  generated_tests/
    test_manifest.json
    run_generated_tests.sh

  validation/
  patch_generation.json
  validation.json
  validation_summary_v2.json
  validation_summary_v2.md
  context_packet.json
  context_packet.md

  reports/
    pre_generation_report.md
    pre_generation_report.json
```

## Notes

- Symbol scanning / call graph / callback extraction currently use regex-first heuristics.
- The patch line fixer repairs hunk counts and attempts line-number alignment from old module sources.
- Validation currently uses `make defconfig + olddefconfig + modules_prepare` before module compile.
- In-tree QEMU execution depends on generated suite commands; if none include qemu/kunit run, qemu stage is reported but not actively booted. OOT integrated validation can generate and execute an in-guest initramfs script.
- `validation_summary_v2.json` reports an evidence vector, not a single correctness oracle. `post_reasoning` is treated as `spec_consistency`: it checks whether the patch satisfies generated local specs/stated obligations, not whether the spec is complete.

## Validation Summary v2

The validation summary can be generated by the main pipeline or rebuilt for existing runs:

```bash
python tools/build_validation_summary_v2.py \
  --run-dir runs/pregen/<case_id>/<timestamp>
```

For read-only or root-owned runs, write summaries to a separate directory:

```bash
python tools/build_validation_summary_v2.py \
  --run-dir runs/pregen/<case_id>/<timestamp> \
  --out-dir runs/validation_summary_v2
```

The first implemented checks are intentionally conservative:
- Level 0: patch apply/no-op/staged files/edit-scope sanity.
- Level 1: target-module build correctness.
- Level 2: runtime hooks, with skipped suite/QEMU separated from real PASS.
- Level 3: deterministic migration checks for symbol replacement/edit scope only.
- Level 4: LLM post reasoning as spec consistency only.
- Level 5-7: static checker, CBMC, and evaluation-only evidence placeholders.

## Spec Gap Evaluation Harness

Use `tools/analyze_spec_gap.py` to audit FM-Agent-style spec/reasoning gaps on existing run artifacts.
The goal is to find cases where the generated specs/obligations are incomplete or misleading; this harness should not be interpreted as proving patch correctness.

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

Outputs:
- `spec_gap_cases.csv`
- `spec_gap_summary.json`
- `spec_gap_report.md`
- `case_reports/<case_id>.md`
- `8case_spec_gap_table.csv` (when `--emit-8case-table` is enabled)

For targeted `AFFECTED_FUNCTION_MISS` tracing:

```bash
python tools/analyze_affected_miss.py \
  --baseline-dir workflow/runs/spec_gap_analysis_20260512_8cases_v2 \
  --out-dir workflow/runs/affected_miss_analysis_20260513_baseline
```

Outputs:
- `affected_miss_root_cause.csv`
- `affected_miss_root_cause_summary.json`
- `affected_miss_report.md`
- `case_reports/<case_id>_affected_miss.md`

## In-Tree Mini Dataset

Build the 10-case evaluation-only mini dataset:

```bash
python tools/build_intree_mini_dataset.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --cases-csv /mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv \
  --out-dir datasets/intree_mini_v0 \
  --force
```

Analyze Driver-Agent runs against that dataset:

```bash
python tools/analyze_intree_mini_results.py \
  --dataset-dir datasets/intree_mini_v0 \
  --runs-dir runs \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --out-dir runs/intree_mini_v0_analysis
```

Reference diffs inside the dataset are evaluation-only and must not be used as Driver-Agent input.

Reviewer feedback tracking:

- `docs/paper_requirements.md`: living paper-facing input/evidence/correctness policy.
- `docs/reviewer_feedback_response.md`: detailed mapping from OSDI reviews to current status and required revisions.
- `docs/reviewer_feedback_matrix.csv`: machine-readable action matrix for paper/workflow gaps.
