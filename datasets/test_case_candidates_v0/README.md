# Test Case Candidates v0

This candidate pool is for Driver-Agent workflow testing, not a final benchmark.

Selection rule:
- target kernel is `v7.0` where applicable;
- P0 must show old driver source fails on the new kernel before patching;
- failure should be caused by kernel API/interface evolution, not by missing hardware;
- runtime feasibility is tracked separately as `qemu_sufficient`, `qemu_partial`, or compile-only.

Important caveat: OOT cases are dominated by `class_create(owner, name) -> class_create(name)` breaks. They are useful for OOT validation plumbing and QEMU smoke tests, but not enough by themselves for final evaluation diversity.

## QEMU Check Scripts

The OOT smoke scripts intentionally validate load/unload plus visible kernel interfaces (devnode or sysfs). They do not require read/write semantics unless the driver exposes a stable readback behavior. This avoids rejecting suitable API-migration cases because of an over-strong test oracle.
