# OOT Showcase 5 v0

This dataset contains five selected out-of-tree Linux driver migration cases for Driver-Agent showcase and short-paper evaluation, plus three backups in `backup_cases.csv`.

## Scope

Each selected case is an OOT tuple:

```text
(repo_url, repo_ref, source_subdir, module_ko, old_kernel_ref, new_kernel_ref)
```

The target kernel is fixed to `v7.0`. Old kernels vary across `v5.x` and `v6.x` only when needed to establish a clean old-on-old pass and old-on-new failure.

## Input Boundary

Agent input may include old OOT source, old/new kernel source, kernel-side definitions/diffs, and compile diagnostics. Reference/new OOT fixes are evaluation-only and must not be used as agent input.

## Selection Criteria

- Old OOT source builds against the old kernel.
- The same old OOT source fails against `v7.0`.
- The failure is a kernel API/type/signature evolution, not missing packages or unavailable hardware.
- The module has a deterministic or plausible QEMU smoke interface.
- Real public repositories are preferred over tutorial code.

## Selected Cases

| Rank | Case | Why it matters |
|---:|---|---|
| 1 | `oot_v4l2loopback_v0_14_0_v614_to_v70` | Headline case: real virtual video driver, QEMU-visible `/dev/video42`, non-trivial V4L2/timer migration. |
| 2 | `oot_evdi_v1_14_0_v61_to_v70` | Real DisplayLink DRM OOT driver, multiple DRM callback/struct-field breaks. |
| 3 | `oot_vhba_20240202_v61_to_v70` | Small real virtual SCSI driver, clean SCSI API hard break, QEMU-visible control device. |
| 4 | `oot_anbox_ashmem_v54_to_v70` | Real legacy Android memory driver, manageable size, multiple MM/shrinker API breaks. |
| 5 | `oot_anbox_binder_v54_to_v70` | Real legacy Android IPC driver, larger stress case with multiple API changes. |

## Backups

Backups are recorded in `backup_cases.csv` and have reports under `case_reports/`.

- `oot_acpi_call_nix_v1_2_1_v61_to_v70`: QEMU-visible procfs interface but weaker prototype-warning failure.
- `oot_evdi_v1_14_10_v61_to_v70_backup`: larger EVDI stress variant.
- `oot_lddbus_tutorial_v61_to_v70_backup`: tutorial sysfs bus case with clean QEMU script; useful for debugging, not main showcase.

## Evidence Sources

The build evidence comes from local probe artifacts under `runs/` and existing OOT QEMU case assets under `datasets/oot_qemu_cases/`. Case reports list exact evidence paths.

## Current Caveats

- `v4l2loopback` is currently the strongest exercised OOT case: the patched module builds against `v7.0`, required media dependency modules are packaged into the isolated QEMU initramfs, and the in-guest check passes on `/dev/video42` plus the sysfs card label.
- EVDI/VHBA/Anbox QEMU plans are deterministic smoke plans but should still be implemented and exercised before claiming runtime evidence.
- Anbox binder and ashmem share one repository family; if independence matters, report this explicitly.
- VHBA is a public SourceForge repository rather than GitHub; keep it because it is a strong real virtual driver, but note the caveat.
