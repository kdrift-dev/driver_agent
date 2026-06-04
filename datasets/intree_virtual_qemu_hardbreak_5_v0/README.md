# In-tree Virtual QEMU Hard-Break 5 v0

Five primary virtual/software-device Kbuild targets whose old source fails against Linux v7.0 and which should be triggerable in QEMU.

| role | module_path | target | old->new | validation | P0 failure | why |
|---|---|---|---|---|---|---|
| primary | `drivers/block/zram` | `zram.ko` | `v6.19 -> v7.0` | qemu_selftest | signature_or_callback_mismatch | software compressed RAM block device; strong runtime oracle |
| primary | `drivers/net/netdevsim` | `netdevsim.ko` | `v6.19 -> v7.0` | qemu_selftest | missing_function_or_symbol;signature_or_callback_mismatch | network device simulator with dedicated driver selftests |
| primary | `drivers/gpu/drm/vkms` | `vkms.ko` | `v6.19 -> v7.0` | qemu_kunit | missing_function_or_symbol | virtual KMS driver; QEMU-visible synthetic DRM target |
| primary | `drivers/iio/dummy` | `iio_dummy.ko` | `v6.12 -> v7.0` | qemu_interface | missing_function_or_symbol;signature_or_callback_mismatch | dummy IIO driver explicitly has no hardware requirements |
| primary | `drivers/gpu/drm/vgem` | `vgem.ko` | `v6.12 -> v7.0` | qemu_interface | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch | virtual GEM provider; no physical GPU |
| backup | `drivers/usb/gadget/udc` | `dummy_hcd.ko` | `v6.12 -> v7.0` | qemu_interface | missing_function_or_symbol;struct_field_or_type_change | software USB host/device controller; larger target context |

Primary cases are intended for QEMU validation experiments. The backup case is QEMU-triggerable but has a broader target directory and weaker oracle.

Source screen: `runs/virtual_device_target_screen_fixed_20260602_204049`
