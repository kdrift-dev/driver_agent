# In-tree Hard-Break 20 v0

Purpose: a compact, testable in-tree dataset for Driver-Agent workflow iteration.

Selection rules:
- Target kernel is fixed to `v7.0`.
- Old target module source must fail to C-compile in the new kernel tree.
- Modpost-only `Module.symvers` unresolved-symbol artifacts are not counted as hard breaks.
- Each row has a deterministic Driver-Agent validation hook: QEMU smoke, isolated kselftest, or KUnit.
- Input size is bounded: selected rows are <= 18 source files and <= 3.8K nonblank source LOC.

Caveat: this dataset prioritizes runnable validation over subsystem diversity. It has 20 versioned cases across 4 unique modules. For 20 unique modules, we need to add more deterministic runtime hooks or accept weaker/hardware-dependent validation.
