# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Driver-Agent is a Linux-driver migration workflow that adapts an old driver's source to a newer kernel version. It is **not** a generic repository-repair agent. The agent's allowed input boundary is the *old* target driver source plus *old/new* kernel-side definitions and diffs. It must **never** consume the target driver's own new-version diff; reference diffs exist only for evaluation and manual case-pack labeling. Read `README.md` and `docs/paper_requirements.md` for the full input/control policy before changing pipeline behavior.

## Environment & Commands

The dev environment is provided by a Nix flake (`flake.nix` + `nix/*.nix`). There is no `requirements.txt` or `setup.py`; the only Python dependency is the `openai` SDK, plus a kernel-build toolchain (gcc, make, bison, flex, openssl, elfutils, qemu, busybox, etc.) that the devshell pins.

```bash
nix develop                 # enter devshell with Python + kernel-build + QEMU toolchain
treefmt                     # format nix + bash (NOT python, see below)
nix flake check             # run treefmt + pre-commit hooks
```

`treefmt` deliberately has **no Python formatter** (no black/ruff): do not reformat the existing Python sources. Only `*.nix` and `*.sh` are formatted/linted (nixfmt, deadnix, statix, shellcheck). `datasets/`, `runs/`, and `*.diff` are excluded from formatting.

There is **no test suite** in this repo. "Validation" refers to the pipeline's own layered driver-build/runtime evidence, not unit tests.

### Running the pipeline

Single case via the env-overridable wrapper (preferred for local repro):

```bash
MODE=dry  scripts/repro_case.sh    # stages 1-9, no LLM, no compile
MODE=llm  scripts/repro_case.sh    # OpenRouter patch-gen + module compile
MODE=qemu scripts/repro_case.sh    # + kselftest/KUnit/QEMU runtime hooks
# override any default via env: MODULE_PATH=... OLD_REF=v6.19 NEW_REF=v7.0 MODE=llm scripts/repro_case.sh
```

In the devshell these are also exposed as the `da-dry-run` and `da-run` commands.

Direct entry point (full flag surface is in `driver_agent_pregen/cli.py::build_parser`):

```bash
python pregen_driver_agent.py --repo <linux.git> --old-ref v6.19 --new-ref v7.0 \
  --module-path drivers/block/zram --kbuild-target zram.ko --out-dir runs/pregen \
  --enable-llm --llm-provider openrouter --llm-model anthropic/claude-sonnet-4.6 \
  --config-ini config.ini --kernel-config-target allmodconfig --patch-source-mode focused
```

Key flag semantics:
- `--dry-run` persists prompts/inputs/artifacts without calling an LLM or generating a patch. `--enable-llm` is required to make real LLM calls (default off).
- `--kbuild-target <foo.ko>` scopes source loading and validation to one Kbuild target instead of the whole `--module-path` directory. Prefer this: a directory can build multiple `.ko`s and inflate scope.
- `--patch-source-mode focused` (default, paper-facing) sends edit-scope function slices + read-only context; `full_module` sends whole files (noisier, for debugging/ablations).
- `--max-patch-iterations N` enables repair loops; each iteration is a fresh LLM call (not a chat session) emitting a complete replacement patch against the original old source.
- `--include-kernel-commit-info` is **off by default**: the paper-facing workflow uses definitions/diffs only, no commit messages.

### LLM configuration

Providers: `openrouter`, `deepseek`, `openai`. Keys come from env vars or `config.ini` (e.g. `OPEN_ROUTER_KEY=...`, `DEEPSEEK_API_KEY=...`). Never commit real keys. A provider auth/quota error must be labeled a **provider failure**, not a model/pipeline patch failure.

## Architecture

The pipeline is a linear sequence of stage functions orchestrated by `driver_agent_pregen/cli.py::main` (each stage is a `run_*` function in its own module, called in order around line 759+). Each stage reads/writes JSON artifacts under `runs/pregen/<case_id>/<timestamp>/`, so stages are loosely coupled through the filesystem rather than through in-memory objects.

Stage order and owning modules:

1. `module_source_loader.py`: load old target source only (resolves Kbuild-target-owned files from old Makefile/Kbuild). `run_external_module_source_loader` handles the OOT path.
2. `symbol_scanner.py`: extract driver-local functions, kernel-side symbol usage, includes, callbacks, approximate call graph (regex/tree-sitter-style, lightweight).
3. `definition_locator.py`: locate old/new definitions/declarations for non-local kernel symbols (source-search fallbacks, optimized for git refs).
4. `kernel_diff_builder.py`: build focused kernel-side evidence (removed symbols, include changes, struct/field changes, multi-line signature changes). Largest/most central evidence module.
5. `impact_graph_builder.py`: function→symbol impact edges + edit-scope tiers: `edit_required`, `edit_candidate`, `context_only`.
6. `obligation_builder.py`: convert kernel changes into deterministic compatibility obligations (hard checks, semantic checks, confidence, evidence).
7. `spec_agent.py` (`pre_spec`) + `reasoner_agent.py` (`pre_reasoner`): generate obligation-specific local specs and pre-patch guidance. These are guidance/consistency artifacts, **not** correctness proofs.
8. `context_packet.py`: assemble the context packet handed to patch generation.
9. `patch_generation.py`: generate a unified-diff patch candidate, then `patch_line_fixer.py` repairs line numbers/hunks in model output. Largest module; this is the core generation logic.
10. `validation.py` (in-tree) / `oot_validation.py` (out-of-tree): apply patch to old-ref worktree, stage into new-ref worktree, build the target, run runtime hooks, optionally loop repair using compile/runtime feedback.
11. `report.py` + `validation_summary.py`: write context packets and markdown/JSON reports.

Cross-cutting modules: `llm_client.py` (provider abstraction + retries), `repo_utils.py` (git worktree/ref access, `RepositoryAccessor`), `prompts.py` (all prompt templates), `semantic_obligation_validator.py` (deterministic Level-3 obligation checks), `utils.py` (JSON/path/slug helpers), `kbuild_target.py` (Kbuild target → source resolution).

### Validation is an evidence vector, not a pass/fail oracle

There is no single oracle for driver correctness, so validation reports layered evidence (Level 0 sanity → Level 1 build → Level 2 runtime hooks/QEMU → Level 3 deterministic obligation checks → Level 4 spec consistency → Level 5-7 placeholders). Critically: `spec_consistency PASS` means the patch satisfies the *generated* local obligations; it does **not** prove the spec is complete or the driver globally correct. Always report skipped runtime validation separately from passed runtime validation. Runtime evidence is only meaningful when a registered hook actually exercises the target behavior; hardware-dependent drivers may only get build/QEMU-smoke evidence.

## Tools, Datasets, Experiments

- `tools/` holds experiment drivers, dataset builders, and analysis harnesses (none are unit tests). Notable: `run_four_layer_experiment.py` (compares P0 repro / B0 source-only / B1 source+kernel-evidence / D full-pipeline on one case), `build_validation_summary_v2.py`, `analyze_spec_gap.py`, `run_oot_qemu_case.py`.
- `datasets/` holds case catalogs (in-tree Kbuild-target, OOT, showcase, QEMU-testable, etc.). Dataset artifacts **may include reference diffs and manual notes; these are evaluation-only and must never enter the agent pipeline.**
- `docs/` holds paper requirements, reviewer-feedback responses, and validation plans; consult these for policy/scope questions.

## Conventions To Respect

- **Input-boundary discipline is the single most important invariant.** When adding evidence sources, default them off and keep them out of the paper-facing input set unless explicitly an evaluation harness. The "Paper-Facing Policy" section of `README.md` is the authority.
- Don't reformat existing Python (the formatter is intentionally Python-free).
- Many machine-specific defaults (e.g. `/mnt/disk7/...` paths) live in `tools/*.py`; use `scripts/repro_case.sh` with env overrides rather than editing those defaults.
- QEMU validation may require root or a narrow sudo rule; busybox for the initramfs is reached via `DRIVER_AGENT_BUSYBOX` (a static binary), not from PATH.
