# In-tree Showcase 5 v0

This dataset contains five high-value in-tree Linux driver migration cases for Driver-Agent showcase and short-paper evaluation.

Boundary: each case is `(old_ref, new_ref=v7.0, module_path, module_target)`. The selected `module_target` is a loadable `.ko` target declared by the module Makefile/Kbuild.

Input discipline: target new-driver source/reference diff is evaluation-only and must not be used as Driver-Agent input. Agent input remains old target source plus old/new kernel-side evidence.

## Selected Cases

| rank | case_id | target | old -> new | LOC | runtime hook | P0 cause | caveat |
|---:|---|---|---|---:|---|---|---|
| 1 | `showcase_01_iio_dummy_v612_v70` | `iio_dummy.ko` | `v6.12 -> v7.0` | 1125 | deterministic QEMU configfs/sysfs IIO dummy smoke | missing_function_or_symbol;signature_or_callback_mismatch | Kbuild directory has an additional iio_dummy_evgen target; selected module_target is iio_dummy.ko. |
| 2 | `showcase_02_zram_v619_v70` | `zram.ko` | `v6.19 -> v7.0` | 3749 | tools/testing/selftests/zram via isolated QEMU runner | signature_or_callback_mismatch | Larger than ideal LOC and reference diff; use as runtime-validatable showcase/stress case. |
| 3 | `showcase_03_netdevsim_v619_v70` | `netdevsim.ko` | `v6.19 -> v7.0` | 7323 | tools/testing/selftests/drivers/net/netdevsim | missing_function_or_symbol;signature_or_callback_mismatch | Source scope is larger than ideal; existing selftest hook is available but the current isolated runner may need target-specific wiring. |
| 4 | `showcase_04_vgem_v612_v70` | `vgem.ko` | `v6.12 -> v7.0` | 409 | modprobe vgem + /dev/dri/render node smoke or IGT if available | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch | Runtime oracle is weaker than zram/netdevsim unless paired with IGT or a render-node smoke check. |
| 5 | `showcase_05_dummy_hcd_v612_v70` | `dummy_hcd.ko` | `v6.12 -> v7.0` | 2571 | modprobe dummy_hcd + gadget/UDC sysfs smoke | missing_function_or_symbol;struct_field_or_type_change | Target lives in a broad UDC directory with many sibling targets; target-specific source scope is only dummy_hcd plus boundary Makefile/Kconfig. |


## Rejected Near-Misses

- `drivers/gpu/drm/vkms` / `vkms.ko`: virtual and KUnit-relevant, but the reference diff is very large and color-pipeline heavy; better as a stress/limitation case than one of the first five showcase cases.
- `drivers/dca` / `dca.ko`: clean hard-break and compact, but no strong QEMU/runtime oracle; keep as compile-level baseline candidate.
- `drivers/platform/goldfish` / `goldfish_pipe.ko`: virtual-device flavored and compact, but QEMU validation requires a Goldfish-style device environment rather than a generic x86 smoke.
- `drivers/dma-buf/heaps` / `system_heap.ko`, `cma_heap.ko`: selftest-friendly, but current target P0 failure is dominated by module metadata/modpost setup rather than a clean API hard break, so exclude from this hard-break showcase set.

## Artifacts

- `manifest.csv`: compact table for experiments and slides.
- `case_reports/*.md`: per-case audit reports with Kbuild evidence, P0 build evidence, failure excerpts, runtime hook, and evaluation-only reference summary.
- P0 source screen: `runs/virtual_device_target_screen_fixed_20260602_204049`.
- old-on-old verification: `runs/intree_showcase_5_v0_verify/old_on_old_build_results.json`.
