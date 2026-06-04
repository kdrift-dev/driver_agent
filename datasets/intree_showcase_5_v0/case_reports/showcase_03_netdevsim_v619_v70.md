# showcase_03_netdevsim_v619_v70

## Case

- old_ref: `v6.19`
- new_ref: `v7.0`
- module_path: `drivers/net/netdevsim`
- module_target: `netdevsim.ko`
- target_name: `netdevsim`
- case_type: `software_network_simulator`

## Makefile/Kbuild Target Evidence

```make
obj-$(CONFIG_NETDEVSIM) += netdevsim.o
netdevsim-objs := \
netdevsim-objs += \
netdevsim-objs += ipsec.o
netdevsim-objs += psample.o
netdevsim-objs += psp.o
netdevsim-objs += macsec.o
```

Target-scoped old source files:

```text
drivers/net/netdevsim/Makefile
drivers/net/netdevsim/bpf.c
drivers/net/netdevsim/bus.c
drivers/net/netdevsim/dev.c
drivers/net/netdevsim/ethtool.c
drivers/net/netdevsim/fib.c
drivers/net/netdevsim/health.c
drivers/net/netdevsim/hwstats.c
drivers/net/netdevsim/ipsec.c
drivers/net/netdevsim/macsec.c
drivers/net/netdevsim/netdev.c
drivers/net/netdevsim/netdevsim.h
drivers/net/netdevsim/psample.c
drivers/net/netdevsim/psp.c
drivers/net/netdevsim/udp_tunnels.c
```

## Old-on-Old Build

Command:

```sh
make -j8 M=drivers/net/netdevsim KBUILD_MODPOST_WARN=1 netdevsim.ko
```

Result: `passed`

Evidence artifact: `runs/intree_showcase_5_v0_verify/v6_19__drivers_net_netdevsim__netdevsim.ko.json`

## Old-on-New Build

Command from P0 screening:

```sh
make -j8 M=drivers/net/netdevsim KBUILD_MODPOST_WARN=1 netdevsim.ko
```

Result: `failed`

P0 artifact: `runs/virtual_device_target_screen_fixed_20260602_204049/v619_to_v70/case_details/drivers_net_netdevsim__netdevsim.json`

## Compile Failure Excerpt

```text
dev.c:159:9: error: implicit declaration of function ‘bin2hex’ [-Werror=implicit-function-declaration]
dev.c:200:15: error: implicit declaration of function ‘hex2bin’ [-Werror=implicit-function-declaration]
netdev.c:863:35: error: initialization of ‘int (*)(struct net_device *, struct netdev_queue_config *, void *, int)’ from incompatible pointer type ‘int (*)(struct net_device *, void *, int)’ [-Werror=incompatible-pointer-types]
netdev.c:863:35: note: (near initialization for ‘nsim_queue_mgmt_ops.ndo_queue_mem_alloc’)
netdev.c:865:35: error: initialization of ‘int (*)(struct net_device *, struct netdev_queue_config *, void *, int)’ from incompatible pointer type ‘int (*)(struct net_device *, void *, int)’ [-Werror=incompatible-pointer-types]
netdev.c:865:35: note: (near initialization for ‘nsim_queue_mgmt_ops.ndo_queue_start’)
```

## Kernel-Side Cause Hypothesis

netdev/devlink/ethtool helper contracts changed: devlink reporter, queue-management callbacks, and helper/header usage such as bin2hex/hex2bin/debugfs_real_fops differ across versions.

This hypothesis is derived from the old-on-new compile failure and kernel-side API/type names in the error log. Target new-driver diff is not used as agent input.

## Existing kselftest/KUnit/QEMU Availability

- existing test/runtime hook: tools/testing/selftests/drivers/net/netdevsim
- qemu scope: qemu_sufficient

## Reference Diff Summary: Evaluation Only

- changed files: `drivers/net/netdevsim/bpf.c;drivers/net/netdevsim/bus.c;drivers/net/netdevsim/dev.c;drivers/net/netdevsim/fib.c;drivers/net/netdevsim/hwstats.c;drivers/net/netdevsim/netdev.c;drivers/net/netdevsim/netdevsim.h;drivers/net/netdevsim/psample.c;drivers/net/netdevsim/psp.c`
- modified LOC: `89`
- changed functions, best effort: `nsim_bpf_create_prog;nsim_bpf_map_alloc;nsim_bus_dev_new;nsim_bus_dev_max_vfs_write;nsim_dev_traps_init;nsim_rate_node_new;__nsim_dev_port_add;nsim_drv_probe;nsim_fib4_rt_create;nsim_fib6_rt_nh_add;nsim_fib6_rt_create;nsim_fib6_event_init;nsim_fib_event_schedule_work;nsim_nexthop_create;nsim_fib_create;nsim_dev_hwstats_enable_ifindex;nsim_forward_skb;nsim_queue_alloc;nsim_queue_mem;nsim_queue_mem_free;nsim_queue_init;netdevsim;nsim_dev_psample_init;nsim_do_psp;nsim_get_stats`

Reference diff is used only for evaluation notes and is forbidden as Driver-Agent input.

## Suitable for Paper Showcase

Yes.

Why: Network device simulator with dedicated in-tree selftests; old source breaks on netdev/devlink/ethtool/helper API evolution across several files.

Caveats: Source scope is larger than ideal; existing selftest hook is available but the current isolated runner may need target-specific wiring.
