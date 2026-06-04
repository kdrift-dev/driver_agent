# oot_lddbus_tutorial_v61_to_v70_backup

## Rank

Backup case, rank 3.

## Repo Identity

- Repository: https://github.com/d0u9/Linux-Device-Driver.git
- Ref: 11f29803ccd051a9cc960f6da772a9bf87ba2e51
- Ref SHA: not recorded
- Source subdir: `eg_22_lddbus`
- Module: `lddbus.ko` / `lddbus`
- Source size: 4 source files, 390 LOC
- Repository quality: tutorial-public-sysfs-bus-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v6.1> M=<repo>/eg_22_lddbus KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/eg_22_lddbus KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v6.1`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: class_create_signature_change

Excerpt:

```text
class_create old two-argument call is incompatible with the v7.0 one-argument API
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v6.1 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: semantic_scenario
- Interface: custom sysfs bus under /sys/bus/ldd
- Device node: none required
- Deterministic script possible: true

Planned in-guest checks:

- insmod lddbus.ko
- run datasets/oot_qemu_cases/lddbus_sysfs_check.sh in guest
- rmmod lddbus

## Evidence Artifacts

- datasets/oot_hardbreak_small_10_v0/manifest.csv
- datasets/oot_qemu_cases/lddbus_sysfs_check.sh

## Showcase Suitability

- Strong enough for paper showcase: Backup only
- Why selected: Backup/debugging case with clean QEMU sysfs script and known integrated validation path.
- Caveats: Tutorial repository; not suitable as the main paper showcase.
