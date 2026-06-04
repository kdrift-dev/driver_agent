# oot_anbox_ashmem_v54_to_v70

## Rank

Selected case, rank 4.

## Repo Identity

- Repository: https://github.com/anbox/anbox-modules.git
- Ref: 98f0f3b3b1eeb5a6954ca15ec43e150b76369086
- Ref SHA: 98f0f3b3b1eeb5a6954ca15ec43e150b76369086
- Source subdir: `ashmem`
- Module: `ashmem_linux.ko` / `ashmem_linux`
- Source size: 5 source files, 857 LOC
- Repository quality: real-public-legacy-android-memory-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v5.4> M=<repo>/ashmem KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/ashmem KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v5.4`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: missing_function;stricter_warning_or_prototype;type_signature_change

Excerpt:

```text
deps.c:6:5: error: no previous prototype for shmem_zero_setup; ashmem.c:393:23: error: assignment of read-only member vm_flags; ashmem.c:403:64: error: incompatible type for argument 3 of shmem_file_setup; ashmem.c:877:9: error: implicit declaration of function register_shrinker
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v5.4 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: interface_smoke
- Interface: /dev/ashmem misc device
- Device node: /dev/ashmem
- Deterministic script possible: true

Planned in-guest checks:

- insmod ashmem_linux.ko
- test -c /dev/ashmem
- optional open/read-only smoke if busybox supports it
- rmmod ashmem_linux

## Evidence Artifacts

- runs/oot_showcase_probe_v5x_v70_20260604/anbox_ashmem_v5_4_to_v7_0.json

## Showcase Suitability

- Strong enough for paper showcase: Yes
- Why selected: Small real OOT Android memory driver, old-on-old builds and new kernel fails through multiple kernel API changes.
- Caveats: Repository is archived/legacy; smoke can validate module load and device node, not full Android ashmem behavior.
