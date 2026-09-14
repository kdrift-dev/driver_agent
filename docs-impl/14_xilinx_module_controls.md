# Xilinx strict module control protocol v1

Frozen before execution on 2026-09-14. This is a scoped continuation of the
reserved-case file compilation census, not a new dataset or method comparison.
No model calls, source repairs, module loading, or runtime tests are included.

## Case and boundary

- Case: `b2c15db74a37__drivers_crypto_xilinx_xilinx_trng_c`.
- Parent: `9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5`.
- Target: `b2c15db74a379a50d723002799c3db0c6781c75a`.
- Complete old target: `drivers/crypto/xilinx/xilinx-trng.c`, 430 lines, one
  translation unit, no driver-local quoted headers. Kbuild directly assigns
  `xilinx-trng.o` to `CONFIG_CRYPTO_DEV_XILINX_TRNG`.
- Build target: `drivers/crypto/xilinx/xilinx-trng.ko` on x86_64 with
  `COMPILE_TEST`. This establishes build compatibility only; the physical
  Xilinx device and firmware behavior require hardware validation.

Verify the packed old bytes against the exact parent Git blob. Only that C
file is transplanted. The new driver and reference change are evaluation-only
controls and never generation inputs. Existing worktrees and runs are retained.

## Configuration and complete kernel prerequisite

Use two fresh detached worktrees, one per commit. Start from x86_64 `defconfig`;
enable `MODULES`, `COMPILE_TEST`, `CRYPTO`, `CRYPTO_HW`,
`CRYPTO_DRBG_MENU`, `CRYPTO_DRBG_CTR`, `CRYPTO_DF80090A`, `CRYPTO_RNG`,
`HW_RANDOM`, and `DEBUG_INFO_NONE`; set `CRYPTO_DEV_XILINX_TRNG=m`.
The visible DRBG selectors keep the hidden DF/RNG dependencies built in.
Disable debug-info/BTF settings, automatic localversion, unused-symbol trimming,
missing namespace allowances, and section-mismatch warning-only behavior.
Set an empty localversion and run `olddefconfig`. Reject unresolved recipe values.
`MODVERSIONS=n`: these revisions require `!COMPILE_TEST` for CRC module
versioning. Strict exported-symbol resolution is exercised; CRC version
compatibility is outside this x86_64 build configuration.

Use the corrected Nix development shell from `c69f0b3`, GCC and GNU ld, eight
jobs, and a 3600-second per-command timeout. Reject inherited make/error,
external-module, and compiler-flag overrides. Record tool versions and code
identity; never snapshot credentials or the general process environment.

Build `vmlinux modules_prepare` independently at each pristine revision.
Archive configuration, `vmlinux`, `vmlinux.o`, `vmlinux.symvers`, object/command
records, and release identity. At these exact revisions, **in-tree modpost reads
`vmlinux.o` directly**, whereas an external `M=` build would read `Module.symvers`.
The single `.ko` goal does not create a missing `vmlinux.o`; this prerequisite
must therefore exist before testing the target. Verify the DF symbol is exported.

## Four phases and strict outcome

Build old-on-old, new-on-new, old-on-new, then restored-new-on-new. Each phase
removes the target object, module, generated module C/object, their command
records, and the previous `Module.symvers` plus its command record. Preserve
the pristine kernel artifacts. Require a fresh compiler command, actual modpost
with `vmlinux.o` and without `-w`, `-n`, or `-N`, successful make, and a new
x86_64 ELF relocatable `.ko`. Keep full commands, logs, source/config/artifact
hashes, symbol imports, module metadata, and ELF sections.

Configuration, `vmlinux.o`, and `vmlinux.symvers` must remain byte-identical
through each kernel's module phases. No `KBUILD_MODPOST_WARN=1`, ignored errors,
missing-input fallback, or hand-supplied symbol table is accepted.

`observed_module_compile_break` requires all three pristine/restoration module
controls to pass and old-on-new to fail with a genuine compiler diagnostic.
Other failure stages remain control/infrastructure/unresolved outcomes. This
label does not claim the mismatch arises at link time: a compilation failure
can prevent reaching modpost. A successful old-on-new build is recorded as
`old_source_module_builds`, with no runtime implication.

The runner restores the target C source in `finally`, verifies both tracked
kernel worktrees are clean, checks the original kernel checkout HEAD, and
records start/end source identity. Outputs use a fresh directory and include
failures without replacing the earlier file-census measurements.

## Execution and results

Pending at protocol freeze. Append the canonical command, execution identity,
outcomes and artifact hashes after completion; do not rewrite the above rules
to accommodate an observed build failure.
