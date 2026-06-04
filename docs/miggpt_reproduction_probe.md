# MigGPT Reproduction Probe

Date: 2026-06-04

## Goal

Check whether a MigGPT benchmark case can be directly reproduced with Driver-Agent using less information than MigGPT, namely without an existing old patch and without new/reference patched code as agent input.

## MigGPT Public Input Format

The cloned repository is:

```text
/mnt/disk7/jw_ai4os/driver-agent/source_code/MigGPT
```

The public benchmark file is:

```text
source_code/MigGPT/data/data.csv
```

Its documented columns are:

```text
type
s_old
s_old_p
file_new
s_new_gt1
s_new_gt2
s_new_p_gt1
s_new_p_gt2
```

Interpretation:

- `s_old`: old kernel snippet.
- `s_old_p`: old patched snippet.
- `file_new`: new kernel file content.
- `s_new_gt*`: ground-truth retrieval target in the new file.
- `s_new_p_gt*`: ground-truth migrated patched snippet.

This means MigGPT assumes an existing old patch (`s_old -> s_old_p`) and migrates it to a new kernel snippet. Driver-Agent's default task does not assume an old patch.

## Dataset Audit

Local audit result:

```json
{
  "rows": 135,
  "s_old_p_diff": 135,
  "s_new_p_diff": 129,
  "new_p_none": 0,
  "type1": 80,
  "type2": 55
}
```

All rows provide a non-identical `s_old_p`, so every MigGPT sample uses old-patch information that is outside Driver-Agent's default input boundary.

## Path/Buildability Probe

A lightweight grep-based mapping probe found 97 candidate function patterns that can be matched into Linux `v7.0`. The first matches are mostly internal kernel files, for example:

```text
idx 0: arch/arm64/include/asm/daifflags.h::local_daif_restore
idx 1: arch/arm64/include/asm/daifflags.h::local_daif_inherit
idx 3: arch/arm64/include/asm/mmu_context.h::cpu_set_reserved_ttbr0_nosync
idx 4: arch/arm64/include/asm/mmu_context.h::__cpu_set_tcr_t0sz
idx 5: arch/arm64/include/asm/mmu_context.h::cpu_replace_ttbr1
```

These are snippet-level kernel code migrations, not standalone Linux driver module or OOT module cases with a clear `module_path + module_target` and build/QEMU validation path.

## Conclusion

The public MigGPT benchmark is not directly runnable as a Driver-Agent case without changing the task definition.

Reasons:

- It lacks repo/ref/path metadata needed for an auditable Driver-Agent run.
- It supplies old patched code and new patched ground truth, which are forbidden as Driver-Agent agent input.
- The first mappable examples are kernel-internal snippets, not target driver module migrations.
- There is no direct module build target or runtime validation oracle for these snippet rows.

Therefore, MigGPT should be discussed as related/complementary work rather than used as the main direct baseline.

## If We Add a Sanity Comparison Later

A fair comparison would need a separate experiment definition:

1. Select a MigGPT row.
2. Treat `s_old -> s_old_p` as the old patch and run a patch-migration task.
3. Compare against MigGPT in that patch-migration setting.
4. Keep this separate from the default Driver-Agent task, which starts from old driver source plus old/new kernel evidence.

Do not feed `s_old_p`, `s_new_gt*`, or `s_new_p_gt*` into paper-facing Driver-Agent runs.
