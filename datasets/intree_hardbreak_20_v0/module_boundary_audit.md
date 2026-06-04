# Module Boundary Audit

This audit checks whether each unique `module_path` in `intree_hardbreak_20_v0` is a reasonable standalone case boundary.

| module_path | cases | own Makefile/Kconfig | parent inclusion | files | suitability | note |
|---|---:|---|---|---:|---|---|
| `drivers/base/test` | 3 | True/True | obj-y			+= test/; Say Y here if you want the Driver core to test driver remove functions / by calling probe, remove, probe. This tests the remove path without / test this functionality. / source "drivers/base/test/Kconfig" / select N here unless you have already successfully tested with the | 7 | YES_test_unit_NOT_driver_module | self-contained Kbuild-owned KUnit/test directory; useful for validation, but should be labeled as test-module case rather than production driver case |
| `drivers/block/zram` | 8 | True/True | obj-$(CONFIG_ZRAM) += zram/; source "drivers/block/zram/Kconfig" | 20 | YES_driver_source_unit | self-contained Kbuild-owned driver/synthetic-driver directory; reasonable as a single module/source-unit case |
| `drivers/iio/dummy` | 6 | True/True | obj-y += dummy/; source "drivers/iio/dummy/Kconfig" | 8 | YES_driver_source_unit | self-contained Kbuild-owned driver/synthetic-driver directory; reasonable as a single module/source-unit case |
| `drivers/iio/test` | 3 | True/True | obj-y += test/; source "drivers/iio/test/Kconfig" | 6 | YES_test_unit_NOT_driver_module | self-contained Kbuild-owned KUnit/test directory; useful for validation, but should be labeled as test-module case rather than production driver case |
