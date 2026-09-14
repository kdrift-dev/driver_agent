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

Completed on retro, 2026-09-14 16:25:47–16:37:22 UTC, at execution commit
`9e1f7bd4b99ed1f835c1ed1aa87139e3e40af28c` (initial runner `4c71c45`).
Source identity is
`b43c360904dac994d9cbcf3130985f0c32be3cc442de5787749678aea32bd8fc`.

```bash
DRIVER_AGENT_ROOT=/home/arina/work/driver_agent_xilinx_pilot \
  /home/arina/bin/driver-agent-shell python3 tools/run_xilinx_module_controls.py \
  --repo /home/arina/repos/linux \
  --old-source /home/arina/work/driver_agent_ctx/runs/mined/cases/b2c15db74a37__drivers_crypto_xilinx_xilinx_trng_c/old_module_sources/drivers/crypto/xilinx/xilinx-trng.c \
  --out-dir /home/arina/work/driver_agent_xilinx_pilot/runs/xilinx_module_controls_v1_20260914 \
  --jobs 8 --timeout-sec 3600
```

| Phase | Result | Module build seconds |
|---|---|---:|
| Old on old | Fresh `.ko`, strict modpost and final LD pass | 2.671 |
| New on new | Fresh `.ko`, strict modpost and final LD pass | 2.722 |
| Old on new | Compiler API type error; no `.ko` | 1.819 |
| Restored new on new | Fresh `.ko`, strict modpost and final LD pass | 2.671 |

The old-on-new diagnostic is at `xilinx-trng.c:201`: argument 1 to
`crypto_drbg_ctr_df` is `struct crypto_aes_ctx *`, while the new public header
requires `struct aes_enckey *`. It fails before modpost; this is a controlled
**module compilation break**, not an observed link-time failure.

Both fresh kernel builds pass (323.911 and 324.818 seconds). Their resolved
configurations are byte-identical, SHA-256
`9d5c6c0402272fdad72fcf37e6bcd5c6fdcff63c49563e825ce968701f94ead2`.
The old/new `vmlinux.o` hashes differ and stay fixed through their module
phases. Each kernel exports 12,767 symbols. The symbol-table hashes match,
which is expected with unchanged export names and CRC versioning disabled;
the actual modpost input is the independently built, revision-specific object.

Each positive `.ko` is 23,176 bytes and imports 26 symbols, including
`crypto_drbg_ctr_df`. All imports resolve to the corresponding kernel exports.
The new and restored-new modules are byte-identical. Both kernel worktrees
finish with clean tracked source, the kernel store remains at `248951dd`, and
the original evolve diff and ctx checkout are preserved.

Two synthetic module-oracle contract groups pass locally and on retro. An
independent retrospective audit passes all 231 checks, covering 49 artifact
snapshots, 38 command records, all 100 execution-source files, compiler/modpost/
LD commands, imports, stable inputs and restoration. It does not call the
runner's classification function. Its hashes anchor current log bytes; this
is an artifact audit, not an independent replay of historical unlink actions.

The [result catalog](../datasets/xilinx_module_controls_v1.json) pins all phase
hashes and commands. Summary SHA-256:
`2265585ca909a2513b52a1b04aa7134e207a511a42d5fa5059d772244aee5eb8`.
Audit `runs/xilinx_module_controls_audit_20260914.json` SHA-256:
`c432c9a4fb8973533da45bdafff927637d039cc2df78aff8226083ebca922469`.
The 139-file non-worktree artifact index is
`runs/xilinx_module_controls_artifact_index_20260914.json`, SHA-256
`863aae14e1a72513436d7ca1c0e531d99fb42a77e537a9db04d876937354a389`.
The local copy contains all logs, configs, symbol tables and modules; the four
large kernel binary snapshots and both full build worktrees remain on retro.

This qualifies the one-file Xilinx case for controlled build validation of a
future generated patch. The [generation preparation](15_xilinx_generation_protocol.md)
found separate prompt-budget and request-audit gaps; generation conditions
remain **NOT_FROZEN**. No model requests, PPL scoring or runtime tests were run.
