# showcase_01_iio_dummy_v612_v70

## Case

- old_ref: `v6.12`
- new_ref: `v7.0`
- module_path: `drivers/iio/dummy`
- module_target: `iio_dummy.ko`
- target_name: `iio_dummy`
- case_type: `virtual_dummy_iio_driver`

## Makefile/Kbuild Target Evidence

```make
obj-$(CONFIG_IIO_SIMPLE_DUMMY) += iio_dummy.o
iio_dummy-y := iio_simple_dummy.o
iio_dummy-$(CONFIG_IIO_SIMPLE_DUMMY_EVENTS) += iio_simple_dummy_events.o
iio_dummy-$(CONFIG_IIO_SIMPLE_DUMMY_BUFFER) += iio_simple_dummy_buffer.o
obj-$(CONFIG_IIO_DUMMY_EVGEN) += iio_dummy_evgen.o
```

Target-scoped old source files:

```text
drivers/iio/dummy/Kconfig
drivers/iio/dummy/Makefile
drivers/iio/dummy/iio_dummy_evgen.h
drivers/iio/dummy/iio_simple_dummy.c
drivers/iio/dummy/iio_simple_dummy.h
drivers/iio/dummy/iio_simple_dummy_buffer.c
drivers/iio/dummy/iio_simple_dummy_events.c
```

## Old-on-Old Build

Command:

```sh
make -j8 M=drivers/iio/dummy KBUILD_MODPOST_WARN=1 iio_dummy.ko
```

Result: `passed`

Evidence artifact: `runs/intree_showcase_5_v0_verify/v6_12__drivers_iio_dummy__iio_dummy.ko.json`

## Old-on-New Build

Command from P0 screening:

```sh
make -j8 M=drivers/iio/dummy KBUILD_MODPOST_WARN=1 iio_dummy.ko
```

Result: `failed`

P0 artifact: `runs/virtual_device_target_screen_fixed_20260602_204049/v612_to_v70/case_details/drivers_iio_dummy__iio_dummy.json`

## Compile Failure Excerpt

```text
iio_simple_dummy_events.c:186:32: error: implicit declaration of function ‘IIO_EVENT_CODE’; did you mean ‘IIO_MOD_EVENT_CODE’? [-Werror=implicit-function-declaration]
iio_simple_dummy.c:289:17: error: implicit declaration of function ‘iio_device_claim_direct_scoped’; did you mean ‘iio_device_claim_direct’? [-Werror=implicit-function-declaration]
iio_simple_dummy.c:289:48: error: expected expression before ‘return’
iio_simple_dummy.c:289:73: error: expected ‘;’ before ‘{’ token
iio_simple_dummy.c:317:48: error: expected expression before ‘return’
iio_simple_dummy.c:317:73: error: expected ‘;’ before ‘{’ token
iio_simple_dummy.c:516:31: error: initialization of ‘int (*)(struct iio_dev *, const struct iio_chan_spec *, enum iio_event_type,  enum iio_event_direction,  bool)’ {aka ‘int (*)(struct iio_dev *, const struct iio_chan_spec *, enum iio_event_type,  enum iio_event_direction,  _Bool)’}
```

## Kernel-Side Cause Hypothesis

IIO event/direct-mode API changed: IIO_EVENT_CODE is no longer accepted, iio_device_claim_direct_scoped call form is unavailable in v7.0 for the old source, and write_event_config now expects bool.

This hypothesis is derived from the old-on-new compile failure and kernel-side API/type names in the error log. Target new-driver diff is not used as agent input.

## Existing kselftest/KUnit/QEMU Availability

- existing test/runtime hook: deterministic QEMU configfs/sysfs IIO dummy smoke
- qemu scope: qemu_sufficient

## Reference Diff Summary: Evaluation Only

- changed files: `drivers/iio/dummy/iio_simple_dummy.c;drivers/iio/dummy/iio_simple_dummy.h;drivers/iio/dummy/iio_simple_dummy_buffer.c;drivers/iio/dummy/iio_simple_dummy_events.c`
- modified LOC: `174`
- changed functions, best effort: `iio_dummy_channels;iio_dummy_read_raw;iio_dummy_probe;iio_simple_dummy_write_event_config;fakedata;iio_simple_dummy_trigger_h;iio_simple_dummy_event_handler`

Reference diff is used only for evaluation notes and is forbidden as Driver-Agent input.

## Suitable for Paper Showcase

Yes.

Why: Synthetic IIO dummy driver with no physical hardware; compact multi-site API/callback migration; runtime interface can be smoke-tested in QEMU.

Caveats: Kbuild directory has an additional iio_dummy_evgen target; selected module_target is iio_dummy.ko.
