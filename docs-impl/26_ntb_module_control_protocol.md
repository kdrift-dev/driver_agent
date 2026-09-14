# Strict NTB module controls

Freeze this protocol and executable code before new builds in a clean retro
checkout. This is evaluation-only qualification of a second migration commit,
not a model experiment. Preserve the earlier file-object census and all Xilinx
results. Do not reuse a historical partial kernel or transplant only `msi.c`.

## Case and complete source scope

The reserved census case `7bd27439a578__drivers_ntb_msi_c` used only `msi.o`.
Its owner is the composite target `drivers/ntb/ntb.ko`:

- OLD: `b1f4077465b2110d8a486cd6f1aed0c2569c339a`.
- NEW: `7bd27439a578bb7724a9f09f240337ab95d68d2b`, whose first parent is OLD.
- Owned sources: `drivers/ntb/core.c` and `drivers/ntb/msi.c`.
- Module members: `core.o` and `msi.o`. No local headers are owned by this
  target. OLD comprises 18,147 bytes and 725 lines.
- `drivers/ntb/Makefile` and `drivers/ntb/Kconfig` must be unchanged across the
  pair. Track their hashes independently.

`include/linux/ntb.h` is a public kernel-side interface, not an OLD source to
transplant. Keep it at the kernel worktree's own revision throughout. Save its
hash and bytes as kernel input evidence. NEW target sources are read only for
pristine/restoration evaluation controls, stored under an explicitly marked
evaluation-only path. They never enter production preparation or model input.

The prior observed failure is missing public prototypes under warning-as-error
compilation. Keep this subtype separate from consumer argument-type migration
and from runtime incompatibility. Do not infer that the old module is generally
unbuildable under every compiler/configuration or fails on hardware.

## Frozen environment and build recipe

Use two fresh detached kernel worktrees from the existing Git object store.
Record code identity, the complete recipe, exact source hashes, Git/toolchain
versions, commands and logs. Reject inherited build flag/tool overrides and
output reuse. Keep `GIT_OPTIONAL_LOCKS=0` for Git inspection. Use the pinned Nix
development shell, x86-64, GCC, GNU LD, verbose commands and eight jobs.

Start from `defconfig`, then explicitly require `MODULES=y`, `COMPILE_TEST=y`,
`WERROR=y`, `PCI=y`, `PCI_MSI=y`, `NTB=m`, `NTB_MSI=y` and `DEBUG_INFO_NONE=y`.
Disable unrelated NTB hardware/transport/test modules and the inherited strict
control exclusions: debug/BTF/signing/plugin configuration, local-version
suffix inference, symbol trimming, CRC module versioning and permissive
namespace/section-mismatch behavior, as enumerated in the executable recipe.
Record every resolved required/disabled value after `olddefconfig`.

Build full `vmlinux` plus `modules_prepare` separately at OLD and NEW. Preserve
`.config`, `vmlinux`, `vmlinux.o`, `vmlinux.symvers`, kernel command records and
the generated release files. Pin `KERNELRELEASE` to the prepared release for
each module command, so the temporary OLD-source transplant cannot alter its
vermagic through Git dirty-state suffixes. Require identical OLD/NEW configuration bytes,
nonempty built-in exports and exclusion of the modular NTB owner's export.
The kernel store's original HEAD and all historical checkouts stay unchanged.

## Four controls and evidence gates

Run OLD-on-OLD, NEW-on-NEW, complete OLD-owned module on NEW, then restored
NEW-on-NEW. For the transplant, replace both owned files with exact OLD Git
bytes while retaining the NEW kernel/public header. Restore both NEW files in
a `finally` block. Preserve failures and partial run metadata.

Before every phase, remove both constituent objects, composite object, module,
module metadata/object and their command sidecars, plus the module symbol-table
outputs. Require fresh compiler commands for both sources, exact constituent
membership, a fresh composite link, strict modpost using the built `vmlinux.o`,
and a fresh final `.ko` link. No relaxed unresolved-symbol or namespace options
may establish a pass. Check x86-64 ELF artifacts, module name and vermagic, and
resolve strong imports against the built kernel export table. Also snapshot
the kernel-side `.module-common.o`, its command file and `scripts/module.lds`
when present; bind these shared final-link inputs independently from the two
NTB-owned constituents. Freeze the support source and final-link Makefile.

Check exact owned-source hashes and stable kernel/config/release inputs before
and after each phase. Require all three positive module controls to pass and
the restored NEW `.ko` to equal the pristine NEW `.ko` byte for byte. Final
worktrees must have their original tracked sources. Preserve source, config,
object, module and command hashes separately.

Classify OLD-on-NEW only after positive controls and integrity checks pass.
For this recipe a compiler break requires a real failing command and a
diagnostic in an owned source with both constituent compiler invocations
present. Unrelated host-tool errors, missing evidence, relaxed modpost,
timeouts, changed sources/configs and failed positive controls must not count
as a demonstrated migration break. Link errors and infrastructure/unknown
outcomes remain separate. Retain exact diagnostic text for interpretation.

Index all saved control artifacts while explicitly excluding full worktrees.
Independently verify the index, source/config identities, all four outcomes,
fresh commands, import/release checks and restoration. No patches are generated,
no LLM/PPL calls occur, no module is loaded and no runtime test runs. Successful
module linking would establish build evidence only; physical NTB behavior and
CRC versioning are untested.
