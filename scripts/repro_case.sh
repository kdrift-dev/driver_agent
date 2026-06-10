#!/usr/bin/env bash
#
# Local, env-overridable wrapper around pregen_driver_agent.py so a single
# driver-migration case can be reproduced without editing the machine-specific
# /mnt/disk7 defaults in tools/*.py or datasets/*/run_*.sh.
#
# Usage:
#   MODE=dry   scripts/repro_case.sh      # stages 1-9, no LLM, no compile
#   MODE=llm   scripts/repro_case.sh      # OpenRouter patch-gen + module compile
#   MODE=qemu  scripts/repro_case.sh      # + kselftest/KUnit/QEMU runtime hooks
#
# Override any default via environment, e.g.:
#   MODULE_PATH=drivers/pmdomain/st OLD_REF=v6.19 NEW_REF=v7.0 \
#     MODE=llm scripts/repro_case.sh
set -euo pipefail

# Resolve repo root so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE="${MODE:-dry}"
REPO="${REPO:-${LINUX_REPO:-$HOME/repos/linux}}"
OLD_REF="${OLD_REF:-v6.19}"
NEW_REF="${NEW_REF:-v7.0-rc1}"
MODULE_PATH="${MODULE_PATH:-drivers/dax/hmem}"
OUT_DIR="${OUT_DIR:-$ROOT/runs/pregen}"
CONFIG_INI="${CONFIG_INI:-$ROOT/config.ini}"
JOBS="${JOBS:-8}"
LLM_PROVIDER="${LLM_PROVIDER:-openrouter}"
LLM_MODEL="${LLM_MODEL:-anthropic/claude-sonnet-4.6}"
QEMU_TIMEOUT_SEC="${QEMU_TIMEOUT_SEC:-1800}"
PYTHON="${PYTHON:-python}"

if [ ! -d "$REPO" ]; then
  echo "error: Linux tree not found at REPO=$REPO" >&2
  echo "       set REPO/LINUX_REPO to your linux checkout." >&2
  exit 1
fi

if ! git -C "$REPO" rev-parse --verify --quiet "$OLD_REF^{commit}" >/dev/null; then
  echo "error: ref '$OLD_REF' not found in $REPO" >&2
  echo "       fetch tags first: git -C \"$REPO\" fetch --tags origin" >&2
  exit 1
fi
if ! git -C "$REPO" rev-parse --verify --quiet "$NEW_REF^{commit}" >/dev/null; then
  echo "error: ref '$NEW_REF' not found in $REPO" >&2
  echo "       fetch tags first: git -C \"$REPO\" fetch --tags origin" >&2
  exit 1
fi

args=(
  "$ROOT/pregen_driver_agent.py"
  --repo "$REPO"
  --old-ref "$OLD_REF"
  --new-ref "$NEW_REF"
  --module-path "$MODULE_PATH"
  --out-dir "$OUT_DIR"
  --jobs "$JOBS"
)

case "$MODE" in
  dry)
    args+=(--dry-run)
    ;;
  llm)
    args+=(
      --enable-llm
      --llm-provider "$LLM_PROVIDER"
      --llm-model "$LLM_MODEL"
      --config-ini "$CONFIG_INI"
    )
    ;;
  qemu)
    args+=(
      --enable-llm
      --llm-provider "$LLM_PROVIDER"
      --llm-model "$LLM_MODEL"
      --config-ini "$CONFIG_INI"
      --enable-qemu
      --qemu-timeout-sec "$QEMU_TIMEOUT_SEC"
    )
    ;;
  *)
    echo "error: unknown MODE='$MODE' (expected dry|llm|qemu)" >&2
    exit 1
    ;;
esac

echo "[repro] MODE=$MODE $MODULE_PATH $OLD_REF -> $NEW_REF (repo=$REPO)" >&2
exec "$PYTHON" "${args[@]}" "$@"
