# showcase_04_vgem_v612_v70

## Case

- old_ref: `v6.12`
- new_ref: `v7.0`
- module_path: `drivers/gpu/drm/vgem`
- module_target: `vgem.ko`
- target_name: `vgem`
- case_type: `virtual_drm_driver`

## Makefile/Kbuild Target Evidence

```make
vgem-y := vgem_drv.o vgem_fence.o
obj-$(CONFIG_DRM_VGEM)	+= vgem.o
```

Target-scoped old source files:

```text
drivers/gpu/drm/vgem/Makefile
drivers/gpu/drm/vgem/vgem_drv.c
drivers/gpu/drm/vgem/vgem_drv.h
drivers/gpu/drm/vgem/vgem_fence.c
```

## Old-on-Old Build

Command:

```sh
make -j8 M=drivers/gpu/drm/vgem KBUILD_MODPOST_WARN=1 vgem.ko
```

Result: `passed`

Evidence artifact: `runs/intree_showcase_5_v0_verify/v6_12__drivers_gpu_drm_vgem__vgem.ko.json`

## Old-on-New Build

Command from P0 screening:

```sh
make -j8 M=drivers/gpu/drm/vgem KBUILD_MODPOST_WARN=1 vgem.ko
```

Result: `failed`

P0 artifact: `runs/virtual_device_target_screen_fixed_20260602_204049/v612_to_v70/case_details/drivers_gpu_drm_vgem__vgem.json`

## Compile Failure Excerpt

```text
vgem_fence.c:52:9: error: implicit declaration of function ‘del_timer_sync’ [-Werror=implicit-function-declaration]
vgem_fence.c:73:10: error: ‘const struct dma_fence_ops’ has no member named ‘fence_value_str’
vgem_fence.c:73:28: error: initialization of ‘void (*)(struct dma_fence *, ktime_t)’ {aka ‘void (*)(struct dma_fence *, long long int)’} from incompatible pointer type ‘void (*)(struct dma_fence *, char *, int)’ [-Werror=incompatible-pointer-types]
vgem_fence.c:73:28: note: (near initialization for ‘vgem_fence_ops.set_deadline’)
vgem_fence.c:74:10: error: ‘const struct dma_fence_ops’ has no member named ‘timeline_value_str’
vgem_fence.c:74:31: error: excess elements in struct initializer [-Werror]
vgem_fence.c:74:31: note: (near initialization for ‘vgem_fence_ops’)
vgem_fence.c:79:36: error: implicit declaration of function ‘from_timer’; did you mean ‘mod_timer’? [-Werror=implicit-
```

## Kernel-Side Cause Hypothesis

DRM/dma-fence interfaces changed: dma_fence_ops string callbacks were removed/replaced, drm_driver no longer has .date, and timer helpers require updated include/API usage.

This hypothesis is derived from the old-on-new compile failure and kernel-side API/type names in the error log. Target new-driver diff is not used as agent input.

## Existing kselftest/KUnit/QEMU Availability

- existing test/runtime hook: modprobe vgem + /dev/dri/render node smoke or IGT if available
- qemu scope: qemu_sufficient

## Reference Diff Summary: Evaluation Only

- changed files: `drivers/gpu/drm/vgem/vgem_drv.c;drivers/gpu/drm/vgem/vgem_fence.c`
- modified LOC: `56`
- changed functions, best effort: `vgem_open;vgem_gem_create_object;vgem_driver;vgem_init;vgem_fence_release;vgem_fence_create`

Reference diff is used only for evaluation notes and is forbidden as Driver-Agent input.

## Suitable for Paper Showcase

Yes.

Why: Small virtual DRM/GEM module; old source fails on dma_fence ops and drm_driver field evolution; no physical GPU required.

Caveats: Runtime oracle is weaker than zram/netdevsim unless paired with IGT or a render-node smoke check.
