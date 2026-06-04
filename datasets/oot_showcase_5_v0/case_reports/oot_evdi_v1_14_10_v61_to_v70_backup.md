# oot_evdi_v1_14_10_v61_to_v70_backup

## Rank

Backup case, rank 2.

## Repo Identity

- Repository: https://github.com/DisplayLink/evdi.git
- Ref: v1.14.10
- Ref SHA: not recorded
- Source subdir: `module`
- Module: `evdi.ko` / `evdi`
- Source size: 32 source files, 5058 LOC
- Repository quality: real-public-displaylink-drm-oot-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v6.1> M=<repo>/module KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/module KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v6.1`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: signature_mismatch;struct_field_change

Excerpt:

```text
evdi_modeset.c:346:39: error: struct drm_device has no member named struct_mutex; evdi_modeset.c:372:41: error: struct drm_device has no member named struct_mutex
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v6.1 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: load_unload_or_sysfs_smoke
- Interface: /sys/module/evdi
- Device node: none required
- Deterministic script possible: true

Planned in-guest checks:

- insmod evdi.ko
- test -d /sys/module/evdi
- rmmod evdi

## Evidence Artifacts

- runs/oot_showcase_tag_probe_v61_v70_20260604/evdi_v1.14.10.json

## Showcase Suitability

- Strong enough for paper showcase: Backup only
- Why selected: Backup/stress variant of EVDI with more files and similar kernel API break.
- Caveats: Do not count together with main EVDI case unless reporting per-version variants.
