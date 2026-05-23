# drivers/video/fbdev/mmp/panel

This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.

- category: `allocator_object_helper_migration`
- driver-side change: kzalloc(sizeof(*plat_data), GFP_KERNEL) -> kzalloc_obj(*plat_data)
- kernel-side cause: slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.
- workflow focus: Probe callback and allocation failure behavior.
- modified_loc: `2`
- code_loc: `172`
- source_file_count: `1`

## Causal Note
The old probe allocates one platform-data object; the new kernel helper captures that object allocation idiom.

## Required Semantic Keys

Required added symbols:
- `kzalloc_obj`

Expected removed old patterns:
- `kzalloc(sizeof`

## Kernel Evidence Snapshot

- `kmalloc_obj`: new_or_newly_visible_in_kernel_paths; commits=0
- `kzalloc`: unchanged; commits=0
- `kzalloc_obj`: new_or_newly_visible_in_kernel_paths; commits=0

## Files

- `reference.diff`: upstream target driver diff for evaluation only.
- `old_module_sources/`: old target module source snapshot.
- `kernel_evidence.json`: old/new kernel-side symbol evidence.
- `workflow_artifacts.json`: imported workflow outputs if available.
