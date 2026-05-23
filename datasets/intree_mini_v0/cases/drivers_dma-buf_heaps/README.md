# drivers/dma-buf/heaps

This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.

- category: `allocator_and_memory_semantics_migration`
- driver-side change: kzalloc_obj/kmalloc_objs conversions plus clear_page and GFP accounting adjustment
- kernel-side cause: object-aware allocator helpers are introduced; memory zeroing/accounting semantics are adjusted in heap allocation paths.
- workflow focus: kselftest-friendly multi-edit case; tests can exercise dma-buf heap allocation paths.
- modified_loc: `21`
- code_loc: `883`
- source_file_count: `2`

## Causal Note
The patch combines allocator helper migration with memory-management semantic updates in dma-buf heap allocation code.

## Required Semantic Keys

Required added symbols:
- `kzalloc_obj`
- `kmalloc_objs`
- `clear_page`
- `__GFP_ACCOUNT`

Expected removed old patterns:
- `kzalloc(sizeof`
- `kmalloc_array`
- `memset(vaddr, 0, PAGE_SIZE)`

## Kernel Evidence Snapshot

- `__GFP_ACCOUNT`: unchanged; commits=0
- `clear_page`: unchanged; commits=0
- `kmalloc_array`: unchanged; commits=0
- `kmalloc_objs`: new_or_newly_visible_in_kernel_paths; commits=0
- `kzalloc`: unchanged; commits=0
- `kzalloc_obj`: new_or_newly_visible_in_kernel_paths; commits=0

## Files

- `reference.diff`: upstream target driver diff for evaluation only.
- `old_module_sources/`: old target module source snapshot.
- `kernel_evidence.json`: old/new kernel-side symbol evidence.
- `workflow_artifacts.json`: imported workflow outputs if available.
