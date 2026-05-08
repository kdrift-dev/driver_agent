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
- `--enable-qemu`: allow validation stage to execute suite commands that invoke `kunit.py run` / `qemu-system-*`
- `--qemu-timeout-sec SEC`: timeout for suite/qemu commands (default `3600`)
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
