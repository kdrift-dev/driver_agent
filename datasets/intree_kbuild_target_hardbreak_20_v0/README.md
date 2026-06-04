# In-tree Kbuild Module-Target Hard-Break 20 v0

20 Kbuild module-target level cases. Target kernel is fixed to `v7.0`; old ref is `v5.0`.

These are intended for the new module-target case representation, not the old directory-only `module_path` representation.

| # | module_path | module_target | target LOC | files | validation | failure |
|---:|---|---|---:|---:|---|---|
| 1 | `drivers/target/loopback` | `tcm_loop.ko` | 1099 | 2 | compile_only_qemu_partial | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch |
| 2 | `drivers/pps/clients` | `pps-ktimer.ko` | 81 | 1 | compile_only_qemu_partial | missing_function_or_symbol |
| 3 | `drivers/pps/clients` | `pps-ldisc.ko` | 130 | 1 | compile_only_qemu_partial | struct_field_or_type_change;signature_or_callback_mismatch |
| 4 | `drivers/pps/clients` | `pps_parport.ko` | 221 | 1 | compile_only_qemu_partial | missing_function_or_symbol;struct_field_or_type_change |
| 5 | `drivers/net/ethernet/fujitsu` | `fmvj18x_cs.ko` | 969 | 1 | compile_only_hardware_limited | missing_function_or_symbol;signature_or_callback_mismatch |
| 6 | `drivers/media/usb/hackrf` | `hackrf.ko` | 1346 | 1 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change |
| 7 | `drivers/net/ethernet/xircom` | `xirc2ps_cs.ko` | 1581 | 1 | compile_only_hardware_limited | missing_function_or_symbol;signature_or_callback_mismatch |
| 8 | `drivers/net/ethernet/calxeda` | `xgmac.ko` | 1634 | 1 | compile_only_hardware_limited | struct_field_or_type_change;signature_or_callback_mismatch |
| 9 | `drivers/media/pci/dt3155` | `dt3155.ko` | 732 | 2 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change |
| 10 | `drivers/media/usb/airspy` | `airspy.ko` | 928 | 1 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change |
| 11 | `drivers/net/ethernet/ezchip` | `nps_enet.ko` | 755 | 2 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch |
| 12 | `drivers/media/usb/s2255` | `s2255drv.ko` | 2188 | 1 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change |
| 13 | `drivers/media/usb/msi2500` | `msi2500.ko` | 1138 | 1 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch |
| 14 | `drivers/usb/image` | `microtek.ko` | 706 | 2 | compile_only_hardware_limited | missing_function_or_symbol;struct_field_or_type_change;signature_or_callback_mismatch |
| 15 | `drivers/platform/goldfish` | `goldfish_pipe.ko` | 907 | 2 | compile_only_virtual_device_needed | signature_or_callback_mismatch |
| 16 | `drivers/dma/hsu` | `hsu_dma_pci.ko` | 220 | 2 | compile_only_hardware_limited | missing_function_or_symbol |
| 17 | `drivers/media/spi` | `gs1662.ko` | 398 | 1 | compile_only_hardware_limited | struct_field_or_type_change;signature_or_callback_mismatch |
| 18 | `drivers/misc/c2port` | `core.ko` | 790 | 1 | compile_only_hardware_limited | signature_or_callback_mismatch |
| 19 | `drivers/siox` | `siox-core.ko` | 754 | 2 | compile_only_hardware_limited | signature_or_callback_mismatch |
| 20 | `drivers/siox` | `siox-bus-gpio.ko` | 178 | 2 | compile_only_hardware_limited | signature_or_callback_mismatch |

## Why this dataset matters

Several directory-level cases previously marked as not strict single-module become usable after splitting by Kbuild target, e.g. `drivers/pps/clients`, `drivers/misc/c2port`, and `drivers/siox`.

## Caveat

Most cases remain compile-level because the corresponding real hardware or subsystem setup is not available in generic QEMU. This dataset is for target-level source slicing and compile migration, not universal runtime proof.

