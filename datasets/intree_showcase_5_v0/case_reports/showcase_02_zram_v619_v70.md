# showcase_02_zram_v619_v70

## Case

- old_ref: `v6.19`
- new_ref: `v7.0`
- module_path: `drivers/block/zram`
- module_target: `zram.ko`
- target_name: `zram`
- case_type: `software_block_driver`

## Makefile/Kbuild Target Evidence

```make
zram-y	:=	zcomp.o zram_drv.o
zram-$(CONFIG_ZRAM_BACKEND_LZO)		+= backend_lzorle.o backend_lzo.o
zram-$(CONFIG_ZRAM_BACKEND_LZ4)		+= backend_lz4.o
zram-$(CONFIG_ZRAM_BACKEND_LZ4HC)	+= backend_lz4hc.o
zram-$(CONFIG_ZRAM_BACKEND_ZSTD)	+= backend_zstd.o
zram-$(CONFIG_ZRAM_BACKEND_DEFLATE)	+= backend_deflate.o
zram-$(CONFIG_ZRAM_BACKEND_842)		+= backend_842.o
obj-$(CONFIG_ZRAM)	+=	zram.o
```

Target-scoped old source files:

```text
drivers/block/zram/Kconfig
drivers/block/zram/Makefile
drivers/block/zram/backend_842.c
drivers/block/zram/backend_842.h
drivers/block/zram/backend_deflate.c
drivers/block/zram/backend_deflate.h
drivers/block/zram/backend_lz4.c
drivers/block/zram/backend_lz4.h
drivers/block/zram/backend_lz4hc.c
drivers/block/zram/backend_lz4hc.h
drivers/block/zram/backend_lzo.c
drivers/block/zram/backend_lzo.h
drivers/block/zram/backend_lzorle.c
drivers/block/zram/backend_lzorle.h
drivers/block/zram/backend_zstd.c
drivers/block/zram/backend_zstd.h
drivers/block/zram/zcomp.c
drivers/block/zram/zcomp.h
drivers/block/zram/zram_drv.c
drivers/block/zram/zram_drv.h
```

## Old-on-Old Build

Command:

```sh
make -j8 M=drivers/block/zram KBUILD_MODPOST_WARN=1 zram.ko
```

Result: `passed`

Evidence artifact: `runs/intree_showcase_5_v0_verify/v6_19__drivers_block_zram__zram.ko.json`

## Old-on-New Build

Command from P0 screening:

```sh
make -j8 M=drivers/block/zram KBUILD_MODPOST_WARN=1 zram.ko
```

Result: `failed`

P0 artifact: `runs/virtual_device_target_screen_fixed_20260602_204049/v619_to_v70/case_details/drivers_block_zram__zram.json`

## Compile Failure Excerpt

```text
/mnt/disk7/jw_ai4os/driver-agent/workflow/runs/virtual_device_target_screen_fixed_20260602_204049/v619_to_v70/worktrees/new_v7.0/include/linux/stddef.h:8:14: error: passing argument 3 of ‘zs_obj_read_begin’ makes integer from pointer without a cast [-Werror=int-conversion]
zram_drv.c:1948:57: note: in expansion of macro ‘NULL’
/mnt/disk7/jw_ai4os/driver-agent/workflow/runs/virtual_device_target_screen_fixed_20260602_204049/v619_to_v70/worktrees/new_v7.0/include/linux/zsmalloc.h:44:32: note: expected ‘size_t’ {aka ‘long unsigned int’} but argument is of type ‘void *’
zram_drv.c:1948:15: error: too few arguments to function ‘zs_obj_read_begin’
/mnt/disk7/jw_ai4os/driver-agent/workflow/runs/virtual_device_target_screen_fixed_20260602_204049/v619_to_v70/worktrees/new_v7.0/include/linux/zsmalloc.h:43:7: note: declared here
zram_drv.c:1952:49: error: passing argument 3 of ‘zs_obj_read_end’ mak
```

## Kernel-Side Cause Hypothesis

zsmalloc object read API signatures changed: zs_obj_read_begin/end in include/linux/zsmalloc.h now require size/local-copy arguments, so old zram callsites are ill-typed.

This hypothesis is derived from the old-on-new compile failure and kernel-side API/type names in the error log. Target new-driver diff is not used as agent input.

## Existing kselftest/KUnit/QEMU Availability

- existing test/runtime hook: tools/testing/selftests/zram via isolated QEMU runner
- qemu scope: qemu_sufficient

## Reference Diff Summary: Evaluation Only

- changed files: `drivers/block/zram/backend_deflate.c;drivers/block/zram/backend_lz4.c;drivers/block/zram/backend_lz4hc.c;drivers/block/zram/backend_zstd.c;drivers/block/zram/zcomp.c;drivers/block/zram/zram_drv.c;drivers/block/zram/zram_drv.h`
- modified LOC: `978`
- changed functions, best effort: `deflate_create;lz4_create;lz4hc_create;zstd_setup_params;zstd_create;zcomp_create;huge_class_size;zram_slot_lock_init;zram_slot_trylock;dev_to_zram;is_partial_io;init_pp_ctl;release_pp_slot;place_pp_slot;page_same_filled;disksize_show;mem_limit_store;mem_used_max_store;mark_idle;zram_wb_req;writeback_limit_enable_show;writeback_limit_store;writeback_limit_show;writeback_batch_size_store;writeback_batch_size_show;reset_bdev;backing_dev_store;out;zram_release_bdev_block;init_wb_ctl;...`

Reference diff is used only for evaluation notes and is forbidden as Driver-Agent input.

## Suitable for Paper Showcase

Yes.

Why: Software compressed RAM block driver; old source fails on zsmalloc signature evolution; has a strong existing kselftest oracle.

Caveats: Larger than ideal LOC and reference diff; use as runtime-validatable showcase/stress case.
