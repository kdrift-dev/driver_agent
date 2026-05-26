#!/usr/bin/env bash
set -euo pipefail

cd /mnt/disk7/jw_ai4os/driver-agent/workflow

DATASET=/mnt/disk7/jw_ai4os/driver-agent/workflow/datasets/intree_single_module_hardbreak_v0/manifest.csv
OUT=${1:-/mnt/disk7/jw_ai4os/driver-agent/workflow/runs/single_module_hardbreak_v0_four_layer_$(date +%Y%m%d_%H%M%S)}
REPO=/mnt/disk7/jw_ai4os/linux_source_code/linux.git
MAX_PATCH_ITERATIONS=${MAX_PATCH_ITERATIONS:-1}
LOG="$OUT/run.log"
mkdir -p "$OUT"

echo "[dataset] $DATASET" | tee -a "$LOG"
echo "[out] $OUT" | tee -a "$LOG"
echo "[max_patch_iterations] $MAX_PATCH_ITERATIONS" | tee -a "$LOG"

python3 - "$DATASET" <<'PY' | while IFS=$'\t' read -r case_id old_ref new_ref module_path patch_source_mode; do
import csv
import sys
with open(sys.argv[1], newline='', encoding='utf-8') as f:
    for row in csv.DictReader(f):
        print('\t'.join([
            row['case_id'],
            row['old_ref'],
            row['new_ref'],
            row['module_path'],
            row['recommended_patch_source_mode'],
        ]))
PY
  echo "[RUN] $case_id module=$module_path source_mode=$patch_source_mode" | tee -a "$LOG"
  sudo -E python /mnt/disk7/jw_ai4os/driver-agent/workflow/tools/run_four_layer_experiment.py \
    --case-kind intree \
    --repo "$REPO" \
    --old-ref "$old_ref" \
    --new-ref "$new_ref" \
    --module-path "$module_path" \
    --out-dir "$OUT" \
    --jobs 8 \
    --kernel-config-target allmodconfig \
    --enable-llm \
    --llm-provider openrouter \
    --llm-model anthropic/claude-sonnet-4.6 \
    --config-ini /mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini \
    --patch-candidates 1 \
    --max-patch-iterations "$MAX_PATCH_ITERATIONS" \
    --patch-source-mode "$patch_source_mode" \
    --patch-prompt-strength strict \
    --methods B0,B1,D \
    --runtime-policy d-only \
    --qemu-timeout-sec 1800 \
    --layer-timeout-sec 10800 \
    --cleanup-worktrees 2>&1 | tee -a "$LOG"
done

echo "[DONE] $OUT" | tee -a "$LOG"
