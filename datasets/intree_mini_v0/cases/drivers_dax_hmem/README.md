# drivers/dax/hmem

This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.

- category: `soft_reserved_resource_helper_migration`
- driver-side change: walk_iomem_res_desc -> walk_soft_reserve_res; region_intersects -> region_intersects_soft_reserve
- kernel-side cause: v7.0-rc1 adds soft-reserve resource helpers used by hmem.
- workflow focus: Non-allocation API replacement; affected-function discovery should include both hmem_init and hmem_register_device.
- modified_loc: `8`
- code_loc: `267`
- source_file_count: `2`

## Causal Note
The reference patch replaces generic iomem/resource queries parameterized by IORES_DESC_SOFT_RESERVED with new kernel helpers that encode the Soft Reserved contract directly.

## Required Semantic Keys

Required added symbols:
- `walk_soft_reserve_res`
- `region_intersects_soft_reserve`

Expected removed old patterns:
- `walk_iomem_res_desc`
- `region_intersects(`

## Kernel Evidence Snapshot

- `IORES_DESC_SOFT_RESERVED`: unchanged; commits=0
- `region_intersects`: unchanged; commits=0
- `region_intersects_soft_reserve`: new_or_newly_visible_in_kernel_paths; commits=0
- `walk_iomem_res_desc`: unchanged; commits=0
- `walk_soft_reserve_res`: new_or_newly_visible_in_kernel_paths; commits=0

## Files

- `reference.diff`: upstream target driver diff for evaluation only.
- `old_module_sources/`: old target module source snapshot.
- `kernel_evidence.json`: old/new kernel-side symbol evidence.
- `workflow_artifacts.json`: imported workflow outputs if available.
