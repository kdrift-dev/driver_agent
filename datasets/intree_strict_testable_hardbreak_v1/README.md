# In-tree Strict Testable Hard-Break Dataset v1

Selection policy:
- New kernel is fixed to `v7.0`.
- Agent input must remain old target module source plus kernel-side evidence only.
- Cases are included only if the old module source fails to C-compile against `v7.0` after ignoring modpost-only `Module.symvers` artifacts.
- Cases must have an existing deterministic runtime hook in Driver-Agent: isolated QEMU smoke/kselftest or KUnit.
- Large parent directories and no-op-buildable cases are excluded.

Primary cases are intended for first full Driver-Agent runs. Stretch cases are useful stress tests but should not be used as the first demo.

Generated from: `runs/strict_testable_case_search_20260601_225325`.
