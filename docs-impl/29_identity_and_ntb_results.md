# Identity contract and NTB qualification

This package adds an explicit production identity-edit contract and qualifies
NTB as the second module-sized development case. No new model requests or PPL
scores were produced. All earlier Xilinx generation refusals and its separate
post-hoc build results remain unchanged.

## Implemented contracts

`87b7aa2` adds `compile_identity_edits()` and the explicit
`identity_substrings_v3` runner mode. It preserves every submitted operation,
including identities, with original indices, effects and counts. All spans
must remain unique and disjoint. Identity-only output has its own status and
does not emit a patch or count as a repair/build success. V1 and v2 retain
their previous behavior and default binding. No historical response is reused
as successful v3 evidence. See [protocol 25](25_identity_edit_contract.md).

`c092786` fixes Kbuild source scope and adds independent no-build extraction
and module-control entry points. The production loader follows owned sources'
literal relative quoted includes rather than recursively collecting sibling
headers. Unsupported custom include flags, unresolved headers and macro
includes remain explicit scope failures. The pool checks the exact union of
translation units and loaded headers. Generic fixtures cover transitive/cyclic
headers, comments, continuation lines, restricted scope and forbidden reads.

The extraction wrapper runs only the existing loader, scanner, locator and
kernel diff builder. It restricts underlying searches before reading hits,
guards source reads, rejects network/non-read-only-Git processes, freezes code
and protocol, and audits the copied OLD bytes again at completion. The ordinary
CLI's `--dry-run` can reach validation when a candidate exists, so it is not
used as a no-build guarantee. See [protocol 27](27_ntb_preparation_protocol.md).

## NTB source and production evidence

The original census row is `7bd27439a578__drivers_ntb_msi_c`, but the complete
owner is `drivers/ntb/ntb.ko`: `core.c` and `msi.c`, 18,147 OLD bytes/725 lines,
with no local headers. OLD is `b1f4077465b2110d8a486cd6f1aed0c2569c339a`;
NEW is `7bd27439a578bb7724a9f09f240337ab95d68d2b`.
The public `include/linux/ntb.h` remains at the kernel revision, outside the
OLD module transplant.

The first frozen production extraction at `c092786` completed all four stages
and source checks, then refused the pool: `excerpt path cannot be bound
unambiguously`. For `device (struct)`, the producer selected
`arch/alpha/include/asm/device.h:3`; the same matched comment appears at line 3
in ARM and PowerPC headers. Only Alpha matches the complete six-line excerpt.
The adapter refused before checking full Git bytes because the chosen location
had not been persisted. This is a provenance loss, not evidence of a wrong
driver patch. The original refusal remains indexed and unchanged.

`f112edc` makes the producer save its actual OLD/NEW selected locations. The
pool requires both fields together, validates membership in the corresponding
locator row, and verifies the entire excerpt against Git. Legacy ambiguous
rows still refuse. This changes neither location selection nor evidence text;
the new run uses a separate clean checkout and [protocol 28](28_explicit_excerpt_provenance.md).

The second extraction admits **99 Git-verified blocks from 128 symbol/kind
rows, but zero changed-symbol requirements**. It records zero model/build
calls, no forbidden source reads or imports, and unchanged code, source,
protocol and kernel HEAD. The evidence pool is attributable to source bytes;
that does not prove its approximate definitions are semantically complete or
relevant to the configured architecture.

An independent audit passes 4,291 checks. Every recorded read hash and complete
excerpt matches an allowed Git blob. Removing only the two added location
fields makes the two runs' definition rows equal; old sources, locator
inventories, excerpt text and migration flags are unchanged.

## Paired preparation, without generation

Both arms preserve the complete OLD module, the same 99-block pool and empty
requirements list. The full-message character counts are:

| Condition | Characters | Blocks |
|---|---:|---:|
| Mandatory source/instructions | 22,001 | 0 |
| Full evidence pool | 92,335 | 99 |
| Frozen common cap | 39,585 | — |
| First-fit | 39,513 | 26 |
| Obligation-ranked | 39,026 | 33 |

The sets share 12 blocks: 14 occur only in first-fit and 21 only in ranked.
Mechanical selection pressure is present, but every obligation overlap is
zero. Ranked selection therefore reduces to the existing marginal-size tie
break. This does not compare useful obligation-guided selection. There are no
provider-token counts, model outputs, generated patches or runtime results.
`generation_ready=false` and `actual_token_budget_comparison=false` remain.
The structured v3 runner still retains its historical Xilinx preparation
anchor; the new NTB preparation is not silently admitted for model dispatch.

## Module controls

The independent evaluation uses [protocol 26](26_ntb_module_control_protocol.md)
and frozen code `c092786`, two fresh full kernels, `NTB=m`, `NTB_MSI=y`, explicit
`WERROR=y`, complete OLD source transplantation and strict module linking.
The kernel release is `6.19.0+`; the frozen configuration SHA-256 is
`fddb303d790d43ffa08f93af3ae5acb18bfa2fce7ac8bef9c1ad06228ac0fdcb`.

Control execution and its independent audit are still being completed; this
section will be finalized from their saved artifacts before publication.

## Validation and next step

Eleven validation scripts pass on the frozen c092786 checkout, including
unchanged v1/v2 regressions and new v3 transport/compiler, source-scope,
extraction and composite-module checks. The explicit-provenance correction
passes 58 pool checks and five real synthetic extraction groups locally and
on retro. All fixture transport/build simulations are separate from real
provider or kernel execution. Logs preserve an initial in-progress fixture
API mismatch separately from the final successful checks.

The next required change is evidence coverage for kernel declarations of
functions defined by the OLD module. The existing scanner/locator path found
no changed requirement for NTB. Investigate this using OLD source and permitted
OLD/NEW kernel interfaces, keeping compiler diagnoses and NEW driver code on
the evaluation side. Do not inject case-specific obligations, claim a ranking
advantage from zero overlap, or spend another pair of model calls merely to
exercise the new output format.
