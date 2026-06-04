# showcase_05_dummy_hcd_v612_v70

## Case

- old_ref: `v6.12`
- new_ref: `v7.0`
- module_path: `drivers/usb/gadget/udc`
- module_target: `dummy_hcd.ko`
- target_name: `dummy_hcd`
- case_type: `software_usb_controller`

## Makefile/Kbuild Target Evidence

```make
obj-$(CONFIG_USB_DUMMY_HCD)	+= dummy_hcd.o
```

Target-scoped old source files:

```text
drivers/usb/gadget/udc/Kconfig
drivers/usb/gadget/udc/Makefile
drivers/usb/gadget/udc/dummy_hcd.c
```

## Old-on-Old Build

Command:

```sh
make -j8 M=drivers/usb/gadget/udc KBUILD_MODPOST_WARN=1 dummy_hcd.ko
```

Result: `passed`

Evidence artifact: `runs/intree_showcase_5_v0_verify/v6_12__drivers_usb_gadget_udc__dummy_hcd.ko.json`

## Old-on-New Build

Command from P0 screening:

```sh
make -j8 M=drivers/usb/gadget/udc KBUILD_MODPOST_WARN=1 dummy_hcd.ko
```

Result: `failed`

P0 artifact: `runs/virtual_device_target_screen_fixed_20260602_204049/v612_to_v70/case_details/drivers_usb_gadget_udc__dummy_hcd.json`

## Compile Failure Excerpt

```text
dummy_hcd.c:1155:10: error: ‘struct platform_driver’ has no member named ‘remove_new’; did you mean ‘remove’?
dummy_hcd.c:1789:44: error: implicit declaration of function ‘from_timer’; did you mean ‘mod_timer’? [-Werror=implicit-function-declaration]
dummy_hcd.c:1789:67: error: ‘timer’ undeclared (first use in this function); did you mean ‘ktimerd’?
dummy_hcd.c:1789:67: note: each undeclared identifier is reported only once for each function it appears in
dummy_hcd.c:2481:9: error: implicit declaration of function ‘hrtimer_init’; did you mean ‘hrtimers_init’? [-Werror=implicit-function-declaration]
dummy_hcd.c:2772:10: error: ‘struct platform_driver’ has no member named ‘remove_new’; did you mean ‘remove’?
```

## Kernel-Side Cause Hypothesis

Platform-driver/timer APIs changed: platform_driver .remove_new is no longer a member in v7.0 and timer helper usage requires updated declarations/includes.

This hypothesis is derived from the old-on-new compile failure and kernel-side API/type names in the error log. Target new-driver diff is not used as agent input.

## Existing kselftest/KUnit/QEMU Availability

- existing test/runtime hook: modprobe dummy_hcd + gadget/UDC sysfs smoke
- qemu scope: qemu_partial

## Reference Diff Summary: Evaluation Only

- changed files: `drivers/usb/gadget/udc/Kconfig;drivers/usb/gadget/udc/Makefile;drivers/usb/gadget/udc/dummy_hcd.c`
- modified LOC: `117`
- changed functions, best effort: `USB_FSL_USB2;USB_PXA27X;USB_FSL_QE;o;module_param_named;set_link_state;dummy_enable;dummy_alloc_request;dummy_dequeue;dummy_pullup;dummy_udc_async_callbacks;dummy_udc_resume;dummy_urb_enqueue;top;handle_control_request;DEVICE_ATTR_RO;dummy_start;dummy_hcd_resume;dummy_hcd_init`

Reference diff is used only for evaluation notes and is forbidden as Driver-Agent input.

## Suitable for Paper Showcase

Yes.

Why: Software USB host/device controller; old source fails on platform_driver callback and timer API evolution; QEMU-loadable without real USB hardware.

Caveats: Target lives in a broad UDC directory with many sibling targets; target-specific source scope is only dummy_hcd plus boundary Makefile/Kconfig.
