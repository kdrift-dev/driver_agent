# drivers/media/mmc/siano

This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.

- category: `allocator_object_helper_migration`
- driver-side change: kzalloc(sizeof(struct smssdio_device), GFP_KERNEL) -> kzalloc_obj(struct smssdio_device)
- kernel-side cause: slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.
- workflow focus: Probe path in media/mmc.
- modified_loc: `2`
- code_loc: `351`
- source_file_count: `1`

## Causal Note
The reference patch converts a one-struct allocation to the new object-aware helper.

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
