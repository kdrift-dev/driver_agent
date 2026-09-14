# Experiment foundation — 2026-09-14

This package corrects evaluation contracts, connects the production pruner,
audits the frozen gate failures, and prepares an isolated experiment checkout
on retro. It does not rerun the research experiment or tune a method to win.

## Comparison v2

HF uses its real tokenizer and the configured backend for reference-based
selection. All methods pack the complete rendered prompt, including old source,
template and arm-specific guidance. Gate and scorer use exactly that source.
Insufficient mandatory budgets, model-window overflow and truncated scores are
explicit excluded rows; summaries expose excluded denominators. Mock counts
lexical occurrences and labels them as a non-model proxy.

Random selection uses a stable SHA-256-derived seed. Ordinary selectors cannot
read gold labels or reference edits; reference-based methods and
`oracle_obligations` are marked in rows and summaries. Evidence is ordered by
content identity rather than gold-first. The default locus is the full old
source; optional oracle localization tracks file boundaries and falls back
explicitly to full source when needed. Skeleton recall and costs use rendered
skeletons, not hidden bodies.

Limitations: candidate pools remain evaluation-mined; ordinary comparison
obligations approximate old-source/kernel-change overlap rather than running
the production builder. Coefficients are uncalibrated. These fixes do not
establish a selection-quality advantage.

## Production integration

The default-OFF flag `DRIVER_AGENT_CONTEXT_PRUNER` recognizes native
`affected_kernel_symbol`, semantic obligations and replacement candidates.
When enabled, it selects whole read-only blocks in obligation rank order,
retains complete mandatory editable/diagnostic/full-file source through
extraction and shrinking, and counts the actual rendered prompt in characters.
If mandatory input cannot fit, the run records `insufficient_budget` and sends
no LLM request. Directly evidence-linked expansion remains available after
symbol filtering; unrelated or transitive graph neighborhoods are not admitted.

The complete kept/dropped/reordered audit is in
`patch_generation.json.patch_input_budget.context_pruner.blocks`, outside
`input.json` and `prompt.md`. OFF output bytes match the prior implementation
on normal and severe-trimming regression cases. No new production import from
`ctxlab` or reference-derived evaluation data is introduced.

## Reproducibility

New sample manifests use `sha256-casepack-content-v2`: IDs and all case-pack
file contents, with a separate hash for each selected pack. Same IDs with changed
source fail validation. Existing v1 manifests remain readable as explicitly
labeled ID-only checks; their historical digest is not reinterpreted.

New PPL/comparison outputs refuse result-directory reuse and save:

- `run_metadata.started.json`: immutable initial code and selected-file identity;
- `run_metadata.json`: final identity, model revision when exposed by HF,
  environment/configuration, resolved kernel refs, and integrity comparisons;
- exact prompt/source/context artifacts (`comparison_rows.jsonl` or
  `scoring_inputs.jsonl`).

A changed code/input/manifest integrity flag invalidates reproducibility and
must be investigated. Floating model names alone are insufficient for a future
confirmatory protocol. The PPL harness deliberately retains its legacy scoring
semantics for replay and labels its character caps, gate/source mismatch and
tie treatment; comparison v2 is a separate protocol.

## Validation

- 45 comparison/miner/metric checks, including label poisoning, cross-process
  random determinism, exact prompt accounting, backend spies, overflow exclusion,
  file-aware localization and rendered-arm recall/costs.
- Six production-pruner groups: OFF byte parity, native schema, intact source,
  ranked budget retention, connected scope, and dry generation with a model-call
  trap on insufficient budget.
- Five provenance groups: content changes, bypass boundaries, immutable initial
  identity, legacy manifests, credential omission and symlink rejection.
- 1,486 audit assertions/groups, including all 1,000 historical rows against the
  actual frozen Git code and every missing-token evidence location. The deliberate
  corrupted-CSV check emits one expected mismatch and passes by failing closed.
- Python compilation and `git diff --check` pass. Fixture and mock rows are
  implementation checks, not new scientific evidence.

The gate audit and its 479 occurrence records are documented in
[07_gate_audit.md](07_gate_audit.md). Follow-up experiments are ordered in
[STATUS.md](STATUS.md).

## Retro host

SSH: `ssh retro` (`arina@100.96.241.17`). NixOS 25.11; Ryzen 7 5700U,
16 logical CPUs, 32 GB RAM, roughly 783 GiB free at inspection. CPU inference;
KVM is accessible for later QEMU validation.

- Existing Linux clone: `/home/arina/repos/linux`, checkout `248951ddc14d`.
  Fetch completed; the checkout was not reset. `v6.19` resolves to
  `05f7e89ab9731565d8a62e3b5d1ec206485eeb0b`, `v7.0-rc1` to
  `6de23f81a5e08be8fbf5e8d7e9febc72a5b5f27f`.
- HF Python: `/home/arina/ppl-env/bin/python` (torch 2.9.1,
  transformers 4.57.1); `HF_HOME=/home/arina/hf_cache` already contains Qwen.
- Historical context checkout: `/home/arina/work/driver_agent_ctx`.
- Dirty evolve checkout: `/home/arina/work/driver_agent`; its three modified
  fitness/validator files are preserved.
- New experiment checkout: `/home/arina/work/driver_agent_foundation`.
- Launcher: `/home/arina/bin/driver-agent-shell`; `DRIVER_AGENT_ROOT` selects
  the checkout, `REPO` selects Linux, `PPL_PY` selects HF Python, `JOBS` defaults
  to 8. Nix needs `--extra-experimental-features 'nix-command flakes'`.

The devshell now also searches zlib's `share/pkgconfig` directory. Host tools
previously passed actual C compile/link checks against zlib, libelf and OpenSSL,
as well as QEMU/KVM and offline Qwen smoke. Run through the launcher, e.g.:

```bash
ssh retro 'DRIVER_AGENT_ROOT=/home/arina/work/driver_agent_foundation \
  /home/arina/bin/driver-agent-shell python3 tools/run_context_pruner_checks.py'
```

Use a fresh output directory for each experiment. Historical summaries and
caches under the old checkout remain archived; this package does not overwrite
them or launch a full kernel build.


## Verified deployment and real HF smoke

Code commit **`12940c01478335ef426eecec1755fdd7400d2222`** was transferred by
Git bundle to the dedicated `codex/experiment-foundation` worktree. All
45 comparison checks, six pruner groups and five provenance groups passed on
retro as well as locally. A direct Nix devshell check locates zlib, libelf and
OpenSSL with the corrected pkg-config path.

The real HF smoke used one existing frozen case,
`a41e0ab394e4__drivers_firmware_efi_libstub_efi_stub_entry_c`, chosen for small
input size. Nine methods × budgets 32/2048 × full/skeleton produced **36 rows**:
18 scored intact, 18 explicitly `insufficient_budget` without scoring.
All three reference-edit oracle methods identify `HFTeacherForcedBackend` as
their selection backend. Every stored prompt was independently recounted by
the HF tokenizer; counts equal scorer counts, all source text is preserved,
and no valid row reports truncation. Initial/final input, code and manifest
integrity checks all pass. The copied pack hash matches the audited local pack.

Model revision: `8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301`.
Executable source digest:
`6c88abdb3d0c8771505561af40df104a545c67222ca5970b221b12d04acb7369`.
HF did not expose a tokenizer commit in its loaded metadata; that field is
explicitly null. A future confirmatory run must pin tokenizer revision too.

Artifacts on retro: `runs/foundation_hf_smoke/` in the new checkout.
Local copy: `runs/foundation_hf_smoke_retro/` (rows, summary, aggregates,
initial/final metadata and cache). The smoke validates execution only; one
size-selected case provides no method-quality estimate. The launcher defaults
to the new checkout; the old context checkout and dirty evolve diff remain
unchanged.
