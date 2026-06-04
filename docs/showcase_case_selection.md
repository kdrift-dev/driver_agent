# Showcase Case Selection v0

This document records paper/demo-facing case criteria after LLM-generated tests were removed from the default validation story.

## Selection Criteria

A strong showcase case should satisfy most of the following:

1. Target kernel is `v7.0` or later enough to avoid model-training-date ambiguity.
2. P0 failure is explicit: old target source fails on the new kernel before patching.
3. Failure is caused by kernel API/definition evolution, not missing config, missing hardware, or Makefile-only setup.
4. Case boundary is Kbuild module target level when possible: `module_path + module_target`.
5. Runtime validation uses an existing or deterministic hook, not LLM-generated tests:
   - in-tree kselftest,
   - KUnit,
   - deterministic QEMU smoke for virtual/software driver,
   - explicit OOT QEMU script.
6. Input size is within focused/full-module prompt limits and can be audited manually.
7. The case demonstrates a distinct Driver-Agent capability: kernel evidence extraction, affected-function slicing, iterative repair, target-level Kbuild boundary, OOT adaptation, or QEMU validation.

## Ready / Near-Ready In-Tree Cases

### 1. zram

- `old_ref`: `v6.19`
- `new_ref`: `v7.0`
- `module_path`: `drivers/block/zram`
- `module_target`: `zram.ko`
- Runtime: existing `tools/testing/selftests/zram` through isolated QEMU runner.
- Why strong: virtual/software block device, old target fails on new kernel, repair iteration uses compile diagnostics and focused source slices.
- Current artifact:
  - `workflow/runs/virtual_qemu_zram_deepseek_zrambuiltin_20260602_214457/...`
  - `workflow/runs/virtual_qemu_zram_validationpatch_qemupass_20260602_225742/...`
- Caveat: current minimal guest has utility warnings; the runner observes kselftest exit 0 and QEMU pass marker. Present as runtime smoke/kselftest evidence, not complete proof.

### 2. netdevsim

- `old_ref`: `v6.19`
- `new_ref`: `v7.0`
- `module_path`: `drivers/net/netdevsim`
- `module_target`: `netdevsim.ko`
- Runtime: existing `tools/testing/selftests/drivers/net/netdevsim` candidate.
- Why strong: virtual network simulator with dedicated selftests.
- Current status: candidate is strong, but previous full run did not fully pass without additional repair/autofix. Needs one focused rerun after current pipeline cleanup.

### 3. iio_dummy

- `old_ref`: `v6.12` or earlier candidate from dataset
- `new_ref`: `v7.0`
- `module_path`: `drivers/iio/dummy`
- `module_target`: `iio_dummy.ko`
- Runtime: configfs/sysfs IIO dummy smoke.
- Why strong: synthetic no-hardware driver, small input, good for showing focused patch source and target-level boundary.
- Current status: candidate; rerun needed with current default validation policy.

### 4. vgem / vkms

- `module_path`: `drivers/gpu/drm/vgem` or `drivers/gpu/drm/vkms`
- Runtime: DRM virtual device smoke or KUnit candidate.
- Why useful: demonstrates larger virtual driver cases.
- Current status: not first-choice; previous vkms attempts exposed complexity and compile failures. Use only after zram/netdevsim/iio_dummy are stable.

## Ready / Near-Ready OOT Cases

### 1. v4l2loopback

- Real OOT repository: `https://github.com/v4l2loopback/v4l2loopback`
- Target: `v7.0`
- Runtime: QEMU smoke with dependency `.ko` packaging and virtual video-device checks.
- Why strong: real widely used OOT virtual device, demonstrates out-of-tree adaptation and QEMU validation.
- Current artifact:
  - `workflow/runs/oot_v4l2loopback_validate_patch_qemu_targets_20260527_160310/...`
  - `workflow/runs/oot_v4l2loopback_full_qemu_targets_20260527_160738/...`
  - `workflow/runs/showcase_v4l2loopback_validation_fixed_20260604_194658/oot_v4l2loopback_.__v6.14__v7.0/20260604_194658`
- Current verified status: patched `v4l2loopback.ko` builds against `v7.0`, the QEMU initramfs packages required media dependencies (`mc.ko`, `videodev.ko`, `v4l2-async.ko`, `v4l2-dv-timings.ko`, `v4l2-fwnode.ko`), and the in-guest script observes `/dev/video42`, the `DriverAgent` sysfs label, and `DRIVER_AGENT_QEMU_SMOKE_PASS`.
- Caveat: this is deterministic virtual-device runtime evidence, not a proof of complete V4L2 behavioral correctness.

### 2. Linux-Device-Driver eg_22_lddbus

- Repository: `https://github.com/d0u9/Linux-Device-Driver.git`
- Source subdir: `eg_22_lddbus`
- Runtime: sysfs/bus smoke in QEMU.
- Why useful: small and reliable for demonstrating OOT integrated path.
- Caveat: tutorial/demo repository, weaker as paper headline case than v4l2loopback.

### 3. Packt dummy-char / Embetronicx character driver cases

- Repositories listed in `workflow/datasets/oot_hardbreak_small_10_v0/manifest.csv`.
- Runtime: interface smoke via `/dev/*` where device node exists.
- Why useful: small P0 hard-break OOT cases for controlled debugging.
- Caveat: tutorial repositories; use as workflow debugging, not main paper evidence unless paired with a real OOT case.

## Current Policy

- Do not use LLM-generated tests as default runtime validation.
- Strong cases should rely on existing tests or deterministic/explicit scripts.
- `spec_consistency` remains diagnostic only; it is not a correctness metric.
- Reference diffs remain evaluation-only and must not appear in LLM input.
