# In-tree Unique Hard-Break 20 v0

20 unique Kbuild-owned source-unit cases. Target kernel is fixed to `v7.0`.

Reference/new target-driver diff is used only for dataset construction/evaluation, not as Driver-Agent input.

| # | module | old->new | type | validation | LOC | files | failure |
|---:|---|---|---|---|---:|---:|---|
| 1 | `drivers/iio/dummy` | `v6.12 -> v7.0` | synthetic_driver | runtime_ready | 1313 | 6 | missing_function_or_symbol |
| 2 | `drivers/block/zram` | `v6.19 -> v7.0` | production_driver | runtime_ready | 3762 | 18 | signature_or_callback_mismatch |
| 3 | `drivers/base/test` | `v6.0 -> v7.0` | kunit_test_module | runtime_ready_test_unit | 644 | 2 | missing_function_or_symbol |
| 4 | `drivers/iio/test` | `v6.12 -> v7.0` | kunit_test_module | runtime_ready_test_unit | 1331 | 3 | unknown_compile_error |
| 5 | `drivers/gpu/drm/scheduler` | `v6.12 -> v7.0` | subsystem_core | kunit_partial | 2098 | 4 | missing_function_or_symbol |
| 6 | `drivers/gpu/drm/vkms` | `v6.12 -> v7.0` | synthetic_driver | kunit_stress | 1602 | 9 | missing_function_or_symbol |
| 7 | `drivers/target/loopback` | `v5.0 -> v7.0` | production_driver | compile_only_qemu_partial | 1284 | 2 | missing_function_or_symbol |
| 8 | `drivers/pps/clients` | `v5.0 -> v7.0` | production_driver | compile_only_qemu_partial | 741 | 4 | missing_function_or_symbol |
| 9 | `drivers/dca` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 581 | 2 | signature_or_callback_mismatch |
| 10 | `drivers/platform/goldfish` | `v5.0 -> v7.0` | virtual_driver | compile_only_virtual_device_needed | 1066 | 2 | signature_or_callback_mismatch |
| 11 | `drivers/media/usb/hackrf` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1563 | 1 | struct_field_or_type_change |
| 12 | `drivers/media/usb/airspy` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1108 | 1 | struct_field_or_type_change |
| 13 | `drivers/net/ethernet/fujitsu` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1171 | 1 | missing_function_or_symbol |
| 14 | `drivers/media/pci/dm1105` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1244 | 1 | missing_function_or_symbol |
| 15 | `drivers/media/pci/pluto2` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 797 | 1 | missing_function_or_symbol |
| 16 | `drivers/media/pci/dt3155` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 818 | 2 | missing_function_or_symbol |
| 17 | `drivers/net/ethernet/xircom` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1797 | 1 | missing_function_or_symbol |
| 18 | `drivers/net/ethernet/calxeda` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1938 | 1 | struct_field_or_type_change |
| 19 | `drivers/misc/c2port` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1162 | 2 | signature_or_callback_mismatch |
| 20 | `drivers/siox` | `v5.0 -> v7.0` | production_driver | compile_only_hardware_limited | 1156 | 3 | signature_or_callback_mismatch |

## Notes

- `runtime_ready` cases have existing deterministic QEMU/kselftest/KUnit hooks in Driver-Agent.

- `compile_only_hardware_limited` cases are valid hard-break source-unit cases but need hardware/subsystem-specific validation beyond compile.

- `kunit_test_module` rows are useful for validation workflow but should not be counted as production driver migrations.

