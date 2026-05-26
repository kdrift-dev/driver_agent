# In-Tree Single-Module Hard-Break Dataset v0

Purpose: a small auditable dataset for Driver-Agent workflow refinement and paper experiments.

Case definition: each case is an in-tree Linux driver case `<old_ref, new_ref, target_module>` where `target_module` is a single Kbuild-owned module/source unit. The old target module source fails to compile when staged into the new kernel. The failure is caused by kernel-side API/interface evolution, not by using the target driver's new-version diff.

Input policy:

- Agent input may use old target module source and old/new kernel-side definitions/diffs.
- Reference/new target driver diff is evaluation-only.
- Other-driver migration examples are disabled by default.
- Commit messages/diffs are not required for these dataset entries.

Release pair:

- `old_ref`: `v5.0`
- `new_ref`: `v7.0`

Selected cases:

1. `drivers/target/loopback`: SCSI/target-core API evolution; best main case.
2. `drivers/firmware/efi/test`: simple `no_llseek` removal case.
3. `drivers/platform/goldfish`: page pinning plus platform remove callback evolution.
4. `drivers/dca`: `class_create()` signature change.
5. `drivers/xen/xenfs`: VFS new mount API migration.

P0 evidence was produced by the P0-only screener, not by Driver-Agent patch generation:

`runs/case_selection_single_module_v50_to_v70_20260526_124050/p0_screen/`

Use `manifest.csv` as the source of truth for experiment commands.
