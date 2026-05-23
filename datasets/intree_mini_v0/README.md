# In-Tree Mini Dataset v0

Release pair: `v6.19 -> v7.0-rc1`.

This dataset is for evaluation and workflow refinement. It contains upstream target-driver reference diffs, so case packs must not be used as Driver-Agent input.

Allowed Driver-Agent inputs remain: old target driver source plus old/new kernel source/definitions.

## Contents

- `manifest.csv` / `manifest.json`: case metadata and semantic keys.
- `cases/<case>/reference.diff`: evaluation-only upstream driver diff.
- `cases/<case>/old_module_sources/`: old target module source snapshot.
- `cases/<case>/kernel_evidence.json`: old/new kernel-side symbol evidence; kernel commit info is disabled by default.
- `kselftest_v0.csv` / `kselftest_v0.json`: small list of kselftest-friendly candidates.

## Rebuild

```bash
python tools/build_intree_mini_dataset.py \
  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \
  --old-ref v6.19 \
  --new-ref v7.0-rc1 \
  --cases-csv /mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv \
  --out-dir datasets/intree_mini_v0 \
  --force
```
