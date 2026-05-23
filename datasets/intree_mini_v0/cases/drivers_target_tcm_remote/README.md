# drivers/target/tcm_remote

This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.

- category: `allocator_object_helper_migration`
- driver-side change: kzalloc(sizeof(*remote_hba), GFP_KERNEL) -> kzalloc_obj(*remote_hba)
- kernel-side cause: slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.
- workflow focus: Target subsystem; spec may over- or under-constrain string/configfs behavior.
- modified_loc: `2`
- code_loc: `288`
- source_file_count: `2`

## Causal Note
The old code allocates one HBA object and then initializes it; the reference patch adopts the object-aware allocator helper.

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
