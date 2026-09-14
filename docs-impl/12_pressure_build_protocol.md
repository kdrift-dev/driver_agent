# Reserved pressure cases: build eligibility protocol v1

This evaluation-only protocol is fixed before the first build census. It
screens all 11 cases in the immutable `reserved_4096.json` from
`context_pressure_v1`, without changing that selection or its calibration
assignment. The scope is build eligibility, with no generation, PPL scoring,
coefficient fitting, or runtime tests.

## Question and unit

Does transplanting the exact packed old C source into its immediate target
kernel commit introduce a reproducible compilation error? Each case uses the
pack's full parent commit and migration commit, not the unrelated currently
checked-out kernel revision or the release-range endpoints.

The initial unit is the packed translation unit. Its owning Kbuild target,
Kconfig dependencies, composite objects, architecture limitations, and source
scope are recorded separately. The surrounding target tree provides build
dependencies only; it is never model input. A translation-unit failure is not
automatically a module-level migration benchmark, and a translation-unit pass
does not establish link or runtime compatibility.

## Mapping before execution

Read the old/new Makefile or Kbuild chain and Kconfig for every source. Record
the actual object target, owning module or builtin target, controlling symbols,
and required configuration changes. A source removed from the target tree is
`target_removed`, not a required repair. Unsupported architectures or disabled
Kconfig dependencies are explicit exclusions. Directly requesting an object
does not by itself prove that the configured kernel selects it.

The x86_64 configuration recipe starts with `allmodconfig`, enables
`COMPILE_TEST` and the mapped prerequisites, disables mapped conflicts, then
runs `olddefconfig`. `DEBUG_INFO_NONE` is selected and debug-info/BTF settings
are disabled consistently to avoid requiring an unrelated BTF toolchain. Save the resulting configuration, its
hash, and all controlling symbol values. Required symbols must survive Kconfig
resolution as `y` or `m`. Any case-specific configuration comes from build
dependencies, not observed compiler outcomes.

## Four controlled conditions

Use newly created, isolated worktrees; existing kernel, evolve, and experiment
worktrees remain untouched. Verify every packed old-source byte against its
parent Git blob before copying it. For each comparable case, compile:

1. **Old on old:** pristine parent source against the parent kernel.
2. **New on new:** pristine target source against the target kernel.
3. **Old on new:** replace only the packed source in the target tree with its
   verified old bytes, retaining the target tree's Kbuild and other files.
4. **Restored new on new:** restore pristine target bytes and compile again.

The last three conditions use exactly the same resolved target configuration.
The parent uses the same configuration recipe; any resolved config difference
between kernel revisions is retained in the artifacts. No reference patch is
applied to the old source and no diagnostic-driven source fixups are allowed.

Delete the requested object and its command/dependency record before each
compile. A success requires an actual compiler invocation on the requested
source and a newly produced, nonempty ELF object. Record exact commands,
return codes, durations, full logs, object hashes, command/dependency records,
source hashes, configuration hashes, and compiler/build-tool identities.
Timeouts terminate the process group and are infrastructure outcomes.

## Interpretation

An `observed_file_compile_break` requires all three pristine/control compiles
to succeed, active mapped configuration, unchanged target configuration, and
a genuine compiler diagnostic in the old-on-new condition. Timeouts, missing
tools, missing build rules, preparation errors, and missing fresh artifacts
cannot count as a migration break. Preserve ambiguous failures as unresolved.

If old-on-new compiles, report `old_source_compiles` for this configuration.
Feature additions and source-compatible semantic changes can still matter at
runtime; this test does not classify them as behaviorally safe or unnecessary.
They have no demonstrated compilation repair requirement here.

Every observed break needs a source-backed attribution to the changed
kernel interface. If it instead depends on new sibling-driver code/private
headers, record that boundary. Before admitting a case to a patch-quality
experiment, resolve the complete old target source scope, verify controlled
owner-target compilation/linking where supported, and measure prompt pressure
again for that scope under a separate protocol. File-pack token eligibility
must not silently transfer to an expanded module.

Full `.ko` linking, module symbol/version resolution, KUnit/QEMU, and hardware
behavior remain distinct evidence. In particular, `KBUILD_MODPOST_WARN=1` or
ignored modpost errors cannot establish a successful module link.

## Preservation and provenance

The runner verifies the frozen pressure manifest and full input census, refuses
an existing output directory, saves the mapping and its hash, and records code
identity at start and end. It records every selected case, including exclusions
and control failures. No subset is chosen after seeing build outcomes.

Build worktrees and full logs remain under the new run directory. A committed
summary will pin their identities and point to the full artifacts on retro.
The original pressure manifests, historical pre-registration, source review,
and earlier experiment outputs remain immutable.

## Environment correction before completed measurements

The initial attempt at code `d02ed15` failed during `modules_prepare` in both
pristine Hyper-V controls: GCC plugin host compilation could not find `gmp.h`.
The run was interrupted and retained. It contains no completed four-condition
measurement. The Nix shell already included GMP/MPFR/MPC runtime packages;
their development include directories are now exposed through
`CPLUS_INCLUDE_PATH` for the host C++ compiler. Kernel configuration and driver
source are not changed by this correction. A fresh census directory records
the corrected environment and code identity.
