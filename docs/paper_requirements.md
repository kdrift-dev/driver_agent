# Paper-Facing Requirements and Evidence Policy

This document is the living source of truth for paper-facing claims, allowed inputs, evaluation-only artifacts, and currently unresolved design decisions. Update this file whenever the workflow boundary changes.

## Current Paper-Facing Scope

Driver-Agent targets in-tree Linux driver migration caused by kernel version evolution. The workflow starts from an old target driver module and old/new kernel source trees, then builds kernel-side evidence, affected functions, obligations/specs, and patch guidance.

The paper-facing workflow must not rely on target driver new-version code or other-driver migration examples as agent input.

## Agent Input Policy

Allowed by default:

- Old target module source.
- Old/new kernel-side definitions and declarations for symbols used by the old target module.
- Kernel-side definition excerpts, signature diffs, macro bodies, struct field diffs, and relevant kernel-side diff hunks.
- Kernel documentation, include comments, and kernel-side scripts only when they are part of the kernel-side evidence path and do not expose target-driver new-version changes.

Disabled by default:

- Kernel commit subjects, commit messages, changed-file lists, and commit diff excerpts.
- These may be enabled only as an explicit private/debug ablation with `--include-kernel-commit-info`.
- If enabled, driver paths must remain excluded from diff excerpts and changed-file lists.
- Synthetic replacement hints and case-specific patch fixups. These are private/debug only and must not be mixed into paper-facing results.

Forbidden as agent input:

- Target driver new-version source.
- Target driver reference diff.
- Other-driver migration examples or collateral driver evolution evidence.
- Commit diff hunks under `drivers/**`.
- Hand-written case-specific replacement hints or manually inferred correct patches.
- Evaluation case-pack `reference.diff` files.

## Evaluation-Only Policy

Evaluation scripts may use the full new version after patch generation, including:

- Target driver new source.
- Target driver reference diff.
- Upstream patch or commit with driver hunks.
- Semantic keys derived from reference behavior.
- Manual annotations.
- kselftest/QEMU results.

Evaluation-only artifacts must not appear in `llm_calls/*/input.json`.

## Source Context Policy

Analysis stages may read the full old target module source.

In Kbuild-target mode, source loading follows the owning translation units and
their literal relative quoted-header includes within the module boundary.
Recursive collection of unrelated sibling-driver headers is excluded. Missing,
unsupported or out-of-scope local includes remain explicit risks; the strict
kernel-evidence pool refuses unresolved ownership. This resolver does not infer
arbitrary compiler include directories or macro include operands.

Patch generation should be reported by source mode:

- `focused`: default paper-facing mode; edit-scope function slices plus read-only direct caller/callback/resource context.
- `full_module`: complete old target module source; fallback/debug mode when focused extraction fails or for explicit ablations.

The default workflow should use `focused` so patch generation is driven by structured migration context rather than full-module prompting. If focused extraction is empty, the implementation may fall back to `full_module` for pipeline continuity, and this must be reported as `full_module_fallback_used=true`.

## Affected Function Scope Policy

Affected driver functions are split into three edit scopes:

- `edit_required`: the function directly uses a changed kernel-side symbol and the compatibility obligation is evidence-backed.
- `edit_candidate`: a direct caller/callee or resource-paired function that may need a coordinated edit, with explicit evidence such as caller error handling or paired cleanup/resource ownership.
- `context_only`: lifecycle roots, callback registration context, same callback-chain neighbors, or cleanup/lifetime context used for reasoning only.

Patch generation policy:

- `edit_required` functions are default edit targets.
- `edit_candidate` functions may be edited only when the patch rationale cites the candidate evidence.
- `context_only` functions must not be edited by default.

Rationale:

- Expanding every caller/callback into editable scope creates unnecessary edits.
- Keeping only direct callsites misses cases where caller error handling or paired cleanup must change.
- The three-scope design preserves context while keeping the editable surface auditable.

## Commit Information Policy

Default paper-facing setting: no commit information.

Rationale:

- Commit messages can mention target drivers or high-level patch intent.
- Reviewer concerns about contamination are easier to address when commit text is excluded.
- Kernel definitions/diffs are the core allowed evidence.

Optional ablation:

- `--include-kernel-commit-info` may be used to study whether sanitized commit information improves evidence quality.
- Results with this flag must be reported separately and not mixed with default paper-facing results.
- Synthetic replacement hint experiments require `DRIVER_AGENT_ALLOW_SYNTHETIC_REPLACEMENT_HINTS=1`.
- Case-specific patch fixup experiments require `DRIVER_AGENT_ALLOW_CASE_SPECIFIC_FIXUPS=1`.
- These environment variables are not part of the public/default workflow.

## Correctness Reporting Policy

Do not report reference-patch similarity as correctness.

Report metrics separately:

- Patch apply status.
- No-op status.
- Compile status.
- Semantic-key match.
- Function-level reference overlap as diagnostic only.
- FM-Agent-style post reasoning verdict as consistency signal only.
- kselftest/QEMU result when available.
- Manual verdict for curated mini dataset cases.

## Runtime Test Generation Policy

LLM-generated driver tests are disabled by default and are not part of the
paper-facing workflow or correctness story.

Default runtime validation may use:

- existing in-tree kselftest targets,
- existing KUnit targets,
- deterministic module-load/interface-smoke scripts for virtual/software drivers,
- caller-provided explicit OOT QEMU scripts.

LLM-generated test scripts may be enabled only for private/debug exploration
with `--enable-llm-test-generation`. Results from that mode must be reported
separately and must not be used as a primary correctness metric.

Rationale:

- Linux driver tests often require hardware, firmware, device tree/ACPI setup,
  device nodes, mounted filesystems, or subsystem-specific orchestration.
- LLM-generated scripts can be plausible but non-executable or can test only a
  superficial load/unload path.
- Paper-facing validation should prefer deterministic or existing tests whose
  scope can be audited.

## QEMU Dynamic Validation Scope

QEMU-based dynamic validation is strong evidence only when the driver exposes
the migrated behavior through virtual or software-visible interfaces.

For virtual or software-only drivers, Driver-Agent can execute existing
kselftest/KUnit targets or deterministic/explicit in-guest QEMU checks against
sysfs, procfs, debugfs, configfs, character-device, netdev, block-device, or
kselftest-visible interfaces. In that setting, a QEMU test can provide strong
functional evidence for the exercised interface.

For hardware-dependent drivers, QEMU validation usually provides only smoke
evidence, such as load/unload, dmesg regression scanning, or basic sysfs
existence checks. It does not prove that MMIO, DMA, IRQ, firmware, power
management, display/media/audio paths, or board-specific Device Tree behavior
are correct.

Report QEMU evidence by scope:

- `qemu_sufficient`: virtual/software-only driver or path with a meaningful
  software-visible interface or kselftest/KUnit target that can be exercised in
  QEMU.
- `qemu_partial`: driver can be built and maybe loaded in QEMU, but core
  behavior depends on unavailable hardware; QEMU provides smoke evidence only.
- `hardware_required`: meaningful validation requires real hardware,
  subsystem CI, or a dedicated simulator.

`PASS_ALL_VALIDATION` means all currently enabled validation layers passed. It
does not mean the patch is a proof of full driver correctness. A report should
still name the exercised QEMU interface and explain whether that interface
covers the migrated behavior.

## Deferred Semantic Modernization Policy

Source-compatible semantic or idiomatic migrations are out of scope for the default workflow.

Example:

- `kzalloc(sizeof(*x), GFP_KERNEL)` to `kzalloc_obj(*x)`.
- Both APIs exist across the release pair.
- The old API remains source-compatible.
- No kernel-side definition-level break necessarily exists.
- Upstream may still migrate call sites to the newer helper as a safety or idiom update.

Default workflow treatment:

- Do not create a required compatibility obligation from this pattern unless kernel-side evidence shows a concrete contract change.
- Do not mark a generated patch wrong only because it omits such modernization.
- Do not include these cases in the main required-repair success denominator.
- Record them as optional modernization or future-work cases.

Paper treatment:

- Use as a case study to explain the boundary between required compatibility repair and optional semantic modernization.
- State that supporting this class would require an explicit optional mode, likely with commit-level, script-level, or broader evolution evidence.
- Keep this separate from the default paper-facing workflow, which uses old target driver source plus old/new kernel-side definitions/diffs.

## FM-Agent-Style Spec Policy

FM-Agent-style specs are auxiliary artifacts, not correctness oracles.

Role in Driver-Agent:

- Pre-spec generates obligation-specific local specs for affected functions.
- The specs help patch generation preserve data flow, return/error handling, ownership, cleanup, and locking assumptions visible in the provided evidence.
- Pre/post reasoning use these specs as consistency checks against the stated compatibility obligations.

What post reasoning can claim:

- The patch appears consistent with the generated local spec.
- The patch appears to satisfy the stated obligations present in the input evidence.
- The patch preserves local data-flow/resource/error-path properties that the spec explicitly captured.

What post reasoning cannot claim:

- The patch is globally correct.
- The generated spec is complete.
- All true kernel/driver semantic obligations were discovered.
- Hardware, firmware, interrupt, Device Tree, timing, or concurrency behavior is correct.

Therefore, `post_reasoning=PASS` means only:

```text
the patch satisfies the stated obligations/specs supplied to the reasoner
```

It does not mean:

```text
the patch is correct with respect to the full upstream reference behavior
```

Correctness claims require additional evidence:

- deterministic semantic-key checks,
- compile results,
- kselftest/QEMU execution when meaningful,
- CBMC/KUnit/formal checks for local obligations,
- or manual case-pack verdict.

## Paper Claims Allowed Today

Safe claims:

- The default workflow excludes target driver new-version code and reference diffs from agent input.
- The workflow constructs auditable kernel-side evidence from old/new kernel definitions and old driver usage.
- The mini dataset provides evaluation-only case packs for workflow refinement.
- FM-Agent-style specs guide patch generation and consistency checking, especially for local data-flow/resource/error-path properties.
- Post reasoning is not treated as a proof of correctness.

Unsafe claims until further evidence exists:

- The system generally fixes Linux drivers.
- FM-Agent-style post reasoning proves correctness.
- LLM-generated specs are complete correctness specifications.
- Similarity to upstream patch measures correctness.
- Other-driver/collateral migration evidence is part of the main workflow.
- Commit messages are part of the default agent input.
- Source-compatible semantic modernization is treated as required repair.
- Two-agent architecture or sub-second latency is necessary.

## Related-Work Baseline Boundary: MigGPT

MigGPT is related but not an isomorphic baseline for the default Driver-Agent task.

Observed input difference from the public MigGPT repository:

- MigGPT benchmark rows contain `s_old`, `s_old_p`, `file_new`, `s_new_gt*`, and `s_new_p_gt*`.
- The migration prompt explicitly provides the old code snippet and the corresponding patched old snippet.
- Therefore MigGPT solves a patch-migration problem: migrate an existing old patch onto a new kernel snippet.
- Driver-Agent's default task starts earlier: infer the needed driver update from old target source plus old/new kernel evidence, without an existing target patch.

Paper-facing policy:

- Treat MigGPT as closely related/complementary work, especially for OOT kernel patch migration.
- Do not use MigGPT as the main direct baseline unless the experiment is explicitly reframed as old-patch migration.
- If a sanity comparison is included, report the input boundary clearly and do not mix it with Driver-Agent's default source-only/evidence-grounded setting.
- Do not feed `s_old_p`, `s_new_gt*`, or `s_new_p_gt*` into Driver-Agent runs, because these correspond to old-patch/reference-patch information outside the default input boundary.

## Open Decisions

- Whether to include a strict no-commit ablation and an explicit commit-info ablation.
- How to formalize dataset deduplication and annotation agreement.
- Which kselftest cases can be executed reliably in QEMU.
- Whether to add a future optional modernization mode for source-compatible semantic migrations.

## Current ATC-Gap TODOs

These are design TODOs, not requests to add more weak validation layers.

- Semantic-modernization statistics: measure source-compatible modernization patterns such as `kzalloc -> kzalloc_obj` as motivation only. Do not feed these signals into the default pipeline or report them as required correctness repairs.
- In-tree case scope: align the case definition with the actual analysis scope. A case may be a marker directory, but the source scope should use deepest-marker owned files rather than recursive parent paths. Record code LOC and source file count for every case.
- Input size boundary: define the supported source-context budget for LLM patch generation. Large modules should use affected slices plus caller/resource context by default; full-module source is fallback/debug only.
- Dataset construction: build the final dataset only after case-scope cleanup. The mini dataset remains a workflow-debugging set, not the final evaluation set.
- Correctness validation: strengthen deterministic semantic-key/edit-scope/build/runtime evidence where it answers a real correctness question. Do not add Sparse/Smatch/CBMC/KUnit layers unless they are meaningful for the selected case class.
- OOT case studies: add small but non-trivial out-of-tree drivers that fail on the new kernel, can be patched, and expose observable QEMU-testable behavior.
- Related-work comparison: compare against source-only LLM, source+kernel-diff LLM, and deterministic Coccinelle/regex-style baselines where applicable. Reference overlap remains diagnostic, not correctness.
- Spec consistency: report FM-Agent-style post reasoning as `spec_consistency`, never as a correctness metric.

## Change Log

- 2026-05-24: Added ATC-gap TODOs and renamed post reasoning's paper-facing role to spec consistency.
- 2026-05-22: Source-compatible semantic modernization is documented as future work and excluded from the default required-repair workflow.
- 2026-05-22: Default policy set to no kernel commit information. Commit info is now an explicit private/debug ablation via `--include-kernel-commit-info`.
- 2026-05-23: QEMU dynamic validation scope documented as strong only for virtual/software-visible interfaces; hardware-dependent drivers remain smoke-only or hardware-required.
