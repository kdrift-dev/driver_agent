# oot_acpi_call_nix_v1_2_1_v61_to_v70

## Rank

Backup case, rank 1.

## Repo Identity

- Repository: https://github.com/nix-community/acpi_call.git
- Ref: v1.2.1
- Ref SHA: not recorded
- Source subdir: `.`
- Module: `acpi_call.ko` / `acpi_call`
- Source size: 3 source files, 667 LOC
- Repository quality: real-public-acpi-oot-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v6.1> M=<repo>/. KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/. KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v6.1`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: stricter_warning_or_prototype

Excerpt:

```text
acpi_call.c:151:4: error: no previous prototype for decodeHex [-Werror=missing-prototypes]
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v6.1 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: interface_smoke
- Interface: /proc/acpi/call
- Device node: /proc/acpi/call
- Deterministic script possible: true

Planned in-guest checks:

- insmod acpi_call.ko
- test -e /proc/acpi/call
- rmmod acpi_call

## Evidence Artifacts

- runs/oot_showcase_tag_probe_v61_v70_20260604/acpi_call_nix_v1.2.1.json

## Showcase Suitability

- Strong enough for paper showcase: Backup only
- Why selected: Backup only: clean old/new compile split and QEMU-visible procfs interface.
- Caveats: Weaker hard-break: mostly stricter warning/prototype rather than rich API migration.
