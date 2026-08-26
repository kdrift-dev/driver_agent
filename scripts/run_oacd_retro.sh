#!/usr/bin/env bash
#
# OACD Phase-3 runbook — meant to run ON the build host (retro), where a full
# linux.git clone and the .venv-ppl (torch/transformers) live. Resumable and
# stage-based: each stage is idempotent and guarded by its output, so a dropped
# Tailscale session (see docs / memory: the Mac<->retro link is flaky) never
# loses progress. Run stages under nohup and reconnect later, e.g.:
#
#   nohup scripts/run_oacd_retro.sh mine  > runs/oacd/mine.log  2>&1 &
#   nohup scripts/run_oacd_retro.sh ppl   > runs/oacd/ppl.log   2>&1 &
#
# Stages (run in order):
#   pilot    small mine (PILOT_CASES) + gold expressibility-gate check (>=GATE_MIN)
#   mine     full v6.19..v7.0-rc1 mine into $MINE_DIR
#   draw     draw the frozen N-case sample manifest (deterministic)
#   -- STOP: freeze docs/ppl_signal_preregistration.md by a COMMIT before scoring --
#   ppl      pre-registered PPL signal experiment on the frozen manifest (HF backend)
#   compare  method comparison on the real backend
#   all      pilot -> mine -> draw, then stop and print the freeze instructions
#
# Env overrides (defaults chosen for retro):
#   REPO=~/repos/linux OLD_REF=v6.19 NEW_REF=v7.0-rc1
#   MINE_DIR=runs/mined  PILOT_DIR=runs/mined_pilot  OUT=runs/oacd
#   N=200 SEED=0 PILOT_CASES=30 GATE_MIN=80 JOBS=14
#   PPL_PY=.venv-ppl/bin/python  PPL_MODEL=Qwen/Qwen2.5-Coder-0.5B
#
# Artifact transfer back to the Mac is done from the Mac side (scp is unreliable
# over this link; use base64-over-ssh with a retry wrapper) — not by this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

REPO="${REPO:-${LINUX_REPO:-$HOME/repos/linux}}"
OLD_REF="${OLD_REF:-v6.19}"
NEW_REF="${NEW_REF:-v7.0-rc1}"
MINE_DIR="${MINE_DIR:-$ROOT/runs/mined}"
PILOT_DIR="${PILOT_DIR:-$ROOT/runs/mined_pilot}"
OUT="${OUT:-$ROOT/runs/oacd}"
N="${N:-200}"
SEED="${SEED:-0}"
PILOT_CASES="${PILOT_CASES:-30}"
GATE_MIN="${GATE_MIN:-80}"
JOBS="${JOBS:-14}"
PPL_PY="${PPL_PY:-$ROOT/.venv-ppl/bin/python}"
PPL_MODEL="${PPL_MODEL:-Qwen/Qwen2.5-Coder-0.5B}"
MANIFEST="${MANIFEST:-$MINE_DIR/ppl_sample_manifest.json}"

log() { echo "[oacd $(date '+%H:%M:%S')] $*"; }
die() { echo "[oacd ERROR] $*" >&2; exit 1; }

require_repo() {
  [ -d "$REPO/.git" ] || die "no linux.git at REPO=$REPO"
  git -C "$REPO" rev-parse "$OLD_REF" >/dev/null 2>&1 || die "missing ref $OLD_REF in $REPO"
  git -C "$REPO" rev-parse "$NEW_REF" >/dev/null 2>&1 || die "missing ref $NEW_REF in $REPO"
}

# Parse the gold row's gate-pass percentage from an expressibility-only summary.
gold_gate_pct() {
  local summary="$1"
  awk -F'|' '/^\| gold \|/ { gsub(/[ %]/,"",$4); print $4; exit }' "$summary"
}

stage_pilot() {
  require_repo
  mkdir -p "$OUT"
  log "pilot mine: $PILOT_CASES cases $OLD_REF..$NEW_REF -> $PILOT_DIR"
  REPO="$REPO" OUT_DIR="$PILOT_DIR" JOBS="$JOBS" python3 tools/mine_migration_bench.py \
    --old-ref "$OLD_REF" --new-ref "$NEW_REF" --max-cases "$PILOT_CASES"
  log "pilot gate check (expressibility-only)"
  python3 tools/run_ppl_signal_experiment.py \
    --dataset-dir "$PILOT_DIR" --out-root "$OUT/pilot_gate" --scorer expressibility-only
  local pct; pct="$(gold_gate_pct "$OUT/pilot_gate/experiment_summary.md")"
  log "gold expressibility-gate pass = ${pct:-?}% (min $GATE_MIN%)"
  [ -n "$pct" ] || die "could not parse gold gate rate"
  [ "$pct" -ge "$GATE_MIN" ] || die "gold gate ${pct}% < ${GATE_MIN}% — FIX miner labels \
(mining.py SYMBOL_STOPLIST / gold invariant) before the full mine (pre-reg kill rule)"
  log "pilot OK: labels good enough to proceed to full mine"
}

stage_mine() {
  require_repo
  log "full mine $OLD_REF..$NEW_REF -> $MINE_DIR (this is the long one; nohup it)"
  REPO="$REPO" OUT_DIR="$MINE_DIR" JOBS="$JOBS" python3 tools/mine_migration_bench.py \
    --old-ref "$OLD_REF" --new-ref "$NEW_REF"
  log "mine done: $(ls -1 "$MINE_DIR"/cases 2>/dev/null | wc -l) case dirs; catalog:"
  cat "$MINE_DIR/catalog.json" 2>/dev/null || true
}

stage_draw() {
  [ -d "$MINE_DIR" ] || die "no mine at $MINE_DIR — run 'mine' first"
  log "draw frozen sample: n=$N seed=$SEED -> $MANIFEST"
  python3 tools/draw_ppl_sample.py --dataset-dir "$MINE_DIR" --n "$N" --seed "$SEED" \
    --out "$MANIFEST"
  echo
  log "NEXT (manual, load-bearing): freeze the pre-registration BEFORE scoring —"
  echo "    1) copy dataset_digest + n_selected from $MANIFEST into"
  echo "       docs/ppl_signal_preregistration.md (Status DRAFT -> FROZEN)"
  echo "    2) git commit -m 'docs: freeze PPL pre-registration on frozen sample'"
  echo "    3) then run: scripts/run_oacd_retro.sh ppl"
}

stage_ppl() {
  [ -f "$MANIFEST" ] || die "no manifest at $MANIFEST — run 'draw' first"
  grep -qi 'FROZEN' docs/ppl_signal_preregistration.md \
    || die "pre-registration still DRAFT — freeze it by a commit before scoring"
  [ -x "$PPL_PY" ] || die "no PPL python at $PPL_PY (.venv-ppl not built?)"
  log "pre-registered PPL run (HF $PPL_MODEL) on frozen manifest"
  PPL_SCORER=hf PPL_MODEL="$PPL_MODEL" DATASET_DIR="$MINE_DIR" \
    "$PPL_PY" tools/run_ppl_signal_experiment.py \
      --dataset-dir "$MINE_DIR" --manifest "$MANIFEST" --out-root "$OUT/ppl_signal"
  echo; log "summary:"; cat "$OUT/ppl_signal/experiment_summary.md"
}

stage_compare() {
  [ -d "$MINE_DIR" ] || die "no mine at $MINE_DIR"
  [ -x "$PPL_PY" ] || die "no PPL python at $PPL_PY"
  log "method comparison (HF backend)"
  PPL_SCORER=hf PPL_MODEL="$PPL_MODEL" \
    "$PPL_PY" tools/run_method_comparison.py \
      --dataset-dir "$MINE_DIR" --scorer hf --out-root "$OUT/method_comparison"
  echo; log "done: $OUT/method_comparison"
}

case "${1:-}" in
  pilot)   stage_pilot ;;
  mine)    stage_mine ;;
  draw)    stage_draw ;;
  ppl)     stage_ppl ;;
  compare) stage_compare ;;
  all)     stage_pilot; stage_mine; stage_draw ;;
  *) echo "usage: $0 {pilot|mine|draw|ppl|compare|all}" >&2; exit 2 ;;
esac
