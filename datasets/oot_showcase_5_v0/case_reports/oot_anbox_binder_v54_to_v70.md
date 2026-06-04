# oot_anbox_binder_v54_to_v70

## Rank

Selected case, rank 5.

## Repo Identity

- Repository: https://github.com/anbox/anbox-modules.git
- Ref: 98f0f3b3b1eeb5a6954ca15ec43e150b76369086
- Ref SHA: 98f0f3b3b1eeb5a6954ca15ec43e150b76369086
- Source subdir: `binder`
- Module: `binder_linux.ko` / `binder_linux`
- Source size: 5 source files, 4793 LOC
- Repository quality: real-public-legacy-android-ipc-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v5.4> M=<repo>/binder KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/binder KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v5.4`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: signature_mismatch;missing_function;type_access_change

Excerpt:

```text
security_binder_set_context_mgr expected const struct cred * but argument is struct task_struct *; binder.c:3436:23: error: assignment of read-only member vm_flags; binder.c:3486:23: error: implicit declaration of function get_files_struct; binder.c:4371:24: error: too many arguments to function class_create
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v5.4 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: interface_smoke
- Interface: /dev/binder misc/char device
- Device node: /dev/binder
- Deterministic script possible: true

Planned in-guest checks:

- insmod binder_linux.ko
- test -c /dev/binder
- optional open-only smoke
- rmmod binder_linux

## Evidence Artifacts

- runs/oot_showcase_probe_v5x_v70_20260604/anbox_binder_v5_4_to_v7_0.json

## Showcase Suitability

- Strong enough for paper showcase: Yes, as a stress/secondary case
- Why selected: Real OOT Android IPC driver with several kernel API breaks; useful stress case beyond one-line API migrations.
- Caveats: Same repository family as ashmem; larger patch and full Binder semantics require Android userspace tests.
