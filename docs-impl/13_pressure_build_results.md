# Reserved pressure cases: controlled compilation, 2026-09-14

Runner implementation: `d02ed15`; corrected environment: `c69f0b3`, on
`codex/pressure-build-eligibility`.
The [protocol](12_pressure_build_protocol.md) and
[Kbuild/Kconfig mapping](../datasets/pressure_build_mapping_v1.json) were
committed before running the census on retro. The immutable input is all
11 reserved cases at budget 4096, from ten migration commits.

## Result

All 11 cases are accounted for. Eight old translation units compile on the
target kernel, two have controlled compiler failures, and one source has been
removed. The ten comparable cases have **40 actual source compilations**:
all 30 pristine/restoration controls pass, as do eight old-on-new phases.
There are no preparation, configuration, or control failures in the completed
run. All required Kconfig symbols remain active.

| Case commit | Packed source | Old on new | Interpretation |
|---|---|---|---|
| `158ebb578cd5` | `drivers/hv/hv_proc.c` | Compiles | Added status handling; no compile repair demonstrated |
| `21bab791346e` | `drivers/base/revocable.c` | Not run | File removed; no comparable target |
| `4bf783d8415c` | `drivers/soc/mediatek/mtk-cmdq-helper.c` | Compiles | Added hardware support; no compile repair demonstrated |
| `7bd27439a578` | `drivers/ntb/msi.c` | Fails | Two removed public prototypes; warning-as-error |
| `96ea3a1e2d31` | `drivers/net/can/dev/skb.c` | Compiles | Added skb extensions; link/runtime untested |
| `96ea3a1e2d31` | `drivers/net/can/vxcan.c` | Compiles | Added skb extensions; link/runtime untested |
| `b2c15db74a37` | `drivers/crypto/xilinx/xilinx-trng.c` | Fails | Kernel API argument type mismatch |
| `c789a7f40288` | `drivers/media/mc/mc-request.c` | Compiles | Added manual completion; link/runtime untested |
| `cda83b099f11` | `drivers/gpu/drm/tests/drm_gem_shmem_test.c` | Compiles | Existing interface remains; KUnit not executed |
| `de9f0c2a1ce3` | `drivers/media/mc/mc-request.c` | Compiles | Added debug accounting; link/runtime untested |
| `e44f47a9bf51` | `drivers/gpu/drm/drm_pagemap_util.c` | Compiles | Added owner utility; link/runtime untested |

The [committed result catalog](../datasets/pressure_build_results_v1.json)
pins the exact per-phase results, source/configuration/object hashes and logs.
Its hash index covers 572 non-worktree run artifacts. Summary SHA-256:
`39f6811d23f0a63fff8815a64adf8edc8366ad6f91b27ac02f37667d97f29d66`.
Execution source digest:
`aa54cc263e6bb5a654cf4c5bc3917a329f530f684be0f8d7a559f05038b0388e`.
Initial/final code, mapping, manifest and full frozen input pool are unchanged.

The pressure selection is therefore not a ready 11-case required-repair
benchmark. Xilinx provides one concrete consumer API mismatch for a scoped
pilot; NTB is a separate warning-policy/API-removal case. These observations
do not measure selector performance or justify a statistical method claim.

The build checkout is `/home/arina/work/driver_agent_build_eligibility`.
Full artifacts, configurations, logs, object files, and retained isolated
kernel worktrees are under its
`runs/pressure_build_eligibility_v1_20260914_fixedenv/` directory. The kernel object
store is `/home/arina/repos/linux`; existing checkouts are preserved.
`/home/arina/bin/driver-agent-shell` now defaults to this checkout, with its
previous version saved as `driver-agent-shell.before-build-eligibility`.
All 20 completed kernel worktrees have restored tracked source content; the
original evolve diff and earlier checkout HEADs remain unchanged.

## Scope and controls

The experiment compiles exact packed old C source against its immediate
target commit. It first verifies old-on-old and new-on-new, then transplants
the packed source and restores the pristine new source for a final control.
All three target-kernel phases share the same configuration. Every successful
phase requires a fresh ELF x86_64 relocatable object and an actual compiler
command on the requested source.

The configuration uses x86_64 allmodconfig plus recorded Kconfig prerequisites
and conflicts, COMPILE_TEST, and disabled debug information. Build tooling is
the existing pinned Nix development environment. The runner records resolved
configuration values, full commands and tool versions. The separate synthetic
contract checks passed locally and on retro before this census.

One selected case removes `drivers/base/revocable.c` and its public header.
The parent Makefile did not wire that file into a target. The runtime checks
the target Git tree and records its absence; it does not recreate an invented
target and count its failure as a migration requirement.

This is translation-unit compilation evidence. Kbuild ownership is mapped,
but other source files and private headers remain at the target revision in
the old-on-new phase. Module linking, symbol/version resolution and runtime
behavior are separate questions. An old source that compiles may still need a
semantic update; a compiler failure still needs attribution and a complete
old-target source boundary before patch evaluation.

No model calls, PPL runs, generated patches, QEMU boots or hardware tests are
part of this package. The gate calibration and frozen pressure selections
are unchanged.

## Retained environment diagnostic

The first attempt in `runs/pressure_build_eligibility_v1_20260914/` was
interrupted after GCC plugins failed to compile during `modules_prepare`
because the host C++ include path lacked `gmp.h`. Both Hyper-V controls and
the MediaTek preparation failed; the deleted-file case was recorded without
a build. This incomplete attempt supplies no four-condition measurement.

The correction exposes GMP/MPFR/MPC development headers through
`CPLUS_INCLUDE_PATH` in the pinned Nix shell. A header-only host C++ compile
and the Nix formatting check pass. Driver sources, the configuration recipe,
the selected cases, and the kernel commits stay the same for the fresh run.

## Interpreting the NTB diagnostic

The NTB case removes the declarations of `ntbm_msi_free_irq` and
`ntb_msi_peer_addr` from `include/linux/ntb.h`, alongside removal of their
implementations upstream. The packed old implementation then reports
`-Werror=missing-prototypes` for these two definitions. Its old/pristine/restored
controls compile successfully. This is a reproducible compilation failure
under the fixed warning policy, with a specific API-removal mechanism.

It must be reported separately from an incompatible argument type or missing
API used by a consumer. The experiment does not prove a runtime compatibility
failure, and it does not change warning flags after observing the result.
The file is also only one constituent of `ntb.o`; full old-target source and
link validation would be needed before a module-level patch experiment.

## Xilinx TRNG interface mismatch

The old `xilinx-trng.c` passes `struct crypto_aes_ctx *` to
`crypto_drbg_ctr_df()`. The target `include/crypto/df_sp80090a.h` changes that
parameter to `struct aes_enckey *`; GCC reports an incompatible pointer type
at old-source line 201. Old-on-old, new-on-new and restored-new controls pass.
The observed error is therefore tied to a changed kernel interface rather
than a preparation failure or unrelated build target.

This target has one C translation unit and no quoted private-header includes
in the packed old source. It is a useful pilot candidate, subject to separate
module link and runtime limitations. Its frozen comparison prompt costs 3692
tokens without evidence and 4201 with all three candidate blocks, at a 4096
budget. These are the earlier comparison renderer's counts, not measured
generation-prompt costs or evidence that a particular selector will help.

## Artifact audit and binary variation

Independent retrospective inspection checks exact source Git bytes, all
compiler invocations, configuration hashes, ELF headers, saved object/command
hashes and preceding-artifact deletion records. This is an artifact audit,
not a separate build replay or live observation of unlink operations.
The machine-readable audit is `runs/pressure_build_artifact_audit_20260914.json`,
pinned by the result catalog. It reports no discrepancies, verifies all 40
compiler invocations and 38 ELF objects, checks all 98 execution-source hashes
against commit `c69f0b3`, and verifies the full 685-pack input pool.

VXCAN and Xilinx each have different initial/restored-new object hashes despite
identical source, configuration and compiler commands. In each object the
difference is exactly eight bytes in `.init.text`, at a random immediate next
to a `latent_entropy` relocation. The configuration enables
`GCC_PLUGIN_LATENT_ENTROPY`; the recorded command loads the plugin without a
fixed seed, and its source reads `/dev/urandom`. This is recorded compiler
nondeterminism. Successful restoration controls do not imply bit-identical
binaries, and no flags were changed to force a hash match.

## Next experiment boundary

Start with a scoped Xilinx pilot: establish the complete old-target source
boundary and strict module link controls, then freeze the actual generation
prompt, budget, model, conditions and validation before generating patches.
The current configuration builds its object as a builtin; it does not certify
the `.ko` configuration or hardware operation. Keep NTB's prototype-removal
diagnostic separate. More build-qualified migration commits are needed before
comparing methods statistically; the 11-case pressure manifest stays fixed.
