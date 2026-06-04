# oot_v4l2loopback_v0_14_0_v614_to_v70

## Rank

Selected case, rank 1.

## Repo Identity

- Repository: https://github.com/v4l2loopback/v4l2loopback.git
- Ref: v0.14.0
- Ref SHA: bc29e7da18454ca3bcdd9457d13e99363dc04680
- Source subdir: `.`
- Module: `v4l2loopback.ko` / `v4l2loopback`
- Source size: 13 source files, 7057 LOC
- Repository quality: real-public-virtual-video-driver

This is an out-of-tree module because the module source is maintained outside the Linux tree and is built with `M=<external module dir>` against a prepared kernel build tree.

## Build Commands

Old source against old kernel:

```bash
make -j$(nproc) -C <linux-v6.14> M=<repo>/. KBUILD_MODPOST_WARN=1 modules
```

Old source against new kernel:

```bash
make -j$(nproc) -C <linux-v7.0> M=<repo>/. KBUILD_MODPOST_WARN=1 modules
```

`KBUILD_MODPOST_WARN=1` is used so missing full-kernel `Module.symvers` dependencies do not turn modpost warnings into false build failures. The recorded P0 failures are compile/type/signature errors, not missing-package failures.

## Old-On-Old Build Result

- old_kernel_ref: `v6.14`
- result: true

## Old-On-New Build Failure

- new_kernel_ref: `v7.0`
- result: false
- failure kind: missing_function;signature_mismatch

Excerpt:

```text
v4l2loopback.c:1791:9: error: implicit declaration of function del_timer_sync; v4l2loopback.c:2329:9: error: too few arguments to function v4l2_fh_add; v4l2loopback.c:2378:9: error: too few arguments to function v4l2_fh_del; v4l2loopback.c:2896:9: error: implicit declaration of function setup_timer
```

## Kernel-Side Cause Hypothesis

The failure is consistent with kernel API/type/signature evolution between v6.14 and v7.0. Driver-Agent should use old OOT source plus old/new kernel-side definitions and compile diagnostics to identify the affected call sites and generate a migration patch. Reference/new driver code is evaluation-only and must not be used as agent input.

## QEMU Validation Plan

- Test level: semantic_scenario
- Interface: /dev/video42 plus /sys/devices/virtual/video4linux/video42/name
- Device node: /dev/video42
- Deterministic script possible: true

Planned in-guest checks:

- Build patched v4l2loopback.ko and dependency modules into initramfs.
- insmod v4l2loopback.ko video_nr=42 card_label=DriverAgent exclusive_caps=1
- test -c /dev/video42
- grep DriverAgent /sys/devices/virtual/video4linux/video42/name
- rmmod v4l2loopback

## Verified QEMU Result

- Status: passed.
- Artifact: `workflow/runs/showcase_v4l2loopback_validation_fixed_20260604_194658/oot_v4l2loopback_.__v6.14__v7.0/20260604_194658`
- Validation summary: `PASS_ALL_VALIDATION`.
- Packaged dependency modules:
  - `drivers/media/mc/mc.ko`
  - `drivers/media/v4l2-core/videodev.ko`
  - `drivers/media/v4l2-core/v4l2-async.ko`
  - `drivers/media/v4l2-core/v4l2-dv-timings.ko`
  - `drivers/media/v4l2-core/v4l2-fwnode.ko`
- Guest evidence:

```text
DRIVER_AGENT_V4L2LOOPBACK_TEST_START
video42
DriverAgent
DRIVER_AGENT_QEMU_SMOKE_PASS
```

This validates module loading and the expected virtual video-device/sysfs surface in QEMU. It is runtime evidence for the showcased adaptation, not a complete proof of all V4L2 behavior.

## Evidence Artifacts

- runs/oot_v4l2_kernel_matrix_20260527/v6.14_v0.14.0_old.json
- runs/oot_v4l2_kernel_matrix_20260527/v6.14_v0.14.0_new.json
- runs/showcase_v4l2loopback_validation_fixed_20260604_194658/oot_v4l2loopback_.__v6.14__v7.0/20260604_194658/validation_summary_v2.json
- runs/showcase_v4l2loopback_validation_fixed_20260604_194658/oot_v4l2loopback_.__v6.14__v7.0/20260604_194658/validation.json

## Showcase Suitability

- Strong enough for paper showcase: Yes
- Why selected: Best headline case: real OOT virtual video driver, clean P0 hard break, deterministic QEMU-visible device node and sysfs check.
- Caveats: Source is above ideal 3000 LOC; QEMU validation requires packaging video4linux dependency modules.
