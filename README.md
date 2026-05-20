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
  - post-patch reasoner (`llm_calls/04_post_reasoner`)
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
- `--patch-source-mode {full_module|focused}`:
  - `full_module` (default): provide full old-module source files to patch generation.
  - `focused`: provide only affected functions and code that references them (direct callers / callback roots).
- `--patch-source-with-lineno` / `--patch-source-no-lineno`:
  - control whether patch-generation source snippets include `L<line>:` prefixes for location hints.
- `--semantic-obligation-table <path>`: query a prebuilt semantic-obligation table to enrich obligation evidence/strength during stage 6.
- `--affected-func-signal-mode {kernel_only|kernel_plus_other_drivers}`:
  - `kernel_only` (default): strictly kernel-side evidence; excludes driver-tree diffs from migration hint mining.
  - `kernel_plus_other_drivers`: opt-in ablation/debug mode that allows migration hints from non-target drivers; target module path is still excluded.
  - In `kernel_only` mode, `--semantic-obligation-table` is ignored so affected-function detection is not enriched by prior driver-side semantic tables.
- `--enable-qemu`: allow validation stage to execute suite commands that invoke `kunit.py run` / `qemu-system-*`
- `--qemu-timeout-sec SEC`: timeout for suite/qemu commands (default `3600`)
- `--skip-system-test-generation`: skip LLM system-test generation and suite/qemu execution (still runs patch apply/compile/post-reasoning)
- `--cleanup-worktrees`: remove temporary old/new git worktrees after validation

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
- QEMU execution depends on generated suite commands; if none include qemu/kunit run, qemu stage is reported but not actively booted.

## Spec Gap Evaluation Harness

Use `tools/analyze_spec_gap.py` to audit FM-Agent-style spec/reasoning gaps on existing run artifacts.

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

## Experimental Semantic Obligation Table Builder

This is an opt-in experiment helper. The default pipeline uses `kernel_only`
affected-function detection and does not query this table.

Build reusable kernel-side + driver-side semantic obligation tables from existing run artifacts:

```bash
python tools/build_semantic_obligation_table.py \
  --runs-dir workflow/runs/pregen_fixcycle_20260513_e_152948 \
  --out-dir workflow/runs/semantic_obligation_table_20260514 \
  --old-ref v6.19 \
  --new-ref v7.0-rc1
```

Outputs:
- `semantic_obligation_table.json`
- `kernel_semantic_obligation_table.csv`
- `driver_semantic_obligation_table.csv`
