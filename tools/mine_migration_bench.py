#!/usr/bin/env python3
"""Mine caller-migration cases with free gold-context labels from linux.git.

Evaluation-only dataset builder (Step 0 of the context-engineering plan).
Scans kernel git history between two refs for *atomic migration commits*:
commits that change a kernel-side API (include/ etc.) and adapt driver
callers in the same commit. Such commits are self-labeling:

  - kernel-side hunks             -> gold context (what the edit needed)
  - driver-side hunks             -> reference edit (one case per .c file)
  - changed-but-unused symbols    -> distractor labels (free negatives)

Gold invariant: every gold symbol appears in BOTH the kernel-side diff and
the caller edit (hard failure otherwise). Reference diffs are evaluation
artifacts and must never enter the agent pipeline (`evaluation_only: true`).

Output layout (compatible with tools/run_ppl_signal_experiment.py):

  <out-dir>/cases/<shortsha>__<driver-file-slug>/
      case.json  kernel_evidence.json  reference.diff  kernel_side.diff
      old_module_sources/<original/path>.c
  <out-dir>/catalog.json  catalog.md

Environment overrides:
    REPO / LINUX_REPO   linux.git checkout (required)
    OLD_REF NEW_REF     git refs           (default v6.19 / v7.0-rc1)
    OUT_DIR             output dataset dir (default runs/mined_migration_bench)
    JOBS                worker threads     (default 8)

Examples:
    # candidate-commit census only, writes nothing:
    REPO=~/repos/linux python3 tools/mine_migration_bench.py \
        --old-ref 'v7.0-rc1~500' --new-ref v7.0-rc1 --list-only
    # small smoke:
    REPO=~/repos/linux python3 tools/mine_migration_bench.py \
        --old-ref 'v7.0-rc1~500' --new-ref v7.0-rc1 --max-cases 20
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.mining import (  # noqa: E402
    apply_check_sample,
    extract_cases_for_commit,
    scan_candidate_commits,
    write_case_pack,
    write_catalog,
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_config(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=os.environ.get("REPO") or os.environ.get("LINUX_REPO", ""))
    p.add_argument("--old-ref", default=_env("OLD_REF", "v6.19"))
    p.add_argument("--new-ref", default=_env("NEW_REF", "v7.0-rc1"))
    p.add_argument("--out-dir", default=_env("OUT_DIR", str(WORKFLOW_ROOT / "runs" / "mined_migration_bench")))
    p.add_argument("--kernel-prefixes", default="include/",
                   help="comma-separated; add kernel/,mm/,block/,lib/ for core-subsystem changes")
    p.add_argument("--driver-prefixes", default="drivers/,sound/")
    p.add_argument("--min-caller-changed-lines", type=int, default=2)
    p.add_argument("--max-caller-changed-lines", type=int, default=200)
    p.add_argument("--max-cases", type=int, default=0, help="stop after N cases (0 = no limit)")
    p.add_argument("--max-cases-per-commit", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=int(_env("JOBS", "8")))
    p.add_argument("--list-only", action="store_true",
                   help="print candidate-commit census and exit, write nothing")
    p.add_argument("--mode", default="atomic", choices=["atomic", "split-commits"],
                   help="split-commits (pickaxe follow-up conversions) is a stub: "
                        "implement only if atomic volume is insufficient")
    args = p.parse_args(argv)
    if not args.repo:
        p.error("linux repo not set; pass --repo or REPO=/path/to/linux")
    if args.mode == "split-commits":
        p.error("--mode split-commits is not implemented yet (atomic MVP first)")
    args.kernel_prefix_list = [s for s in args.kernel_prefixes.split(",") if s]
    args.driver_prefix_list = [s for s in args.driver_prefixes.split(",") if s]
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = build_config(argv)
    repo = Path(args.repo).expanduser()
    rng = random.Random(args.seed)
    scanned_range = f"{args.old_ref}..{args.new_ref}"

    print(f"[miner] scanning {scanned_range} in {repo}")
    candidates = scan_candidate_commits(repo, args.old_ref, args.new_ref,
                                        args.kernel_prefix_list, args.driver_prefix_list)
    print(f"[miner] candidate commits (touch kernel+driver prefixes): {len(candidates)}")
    if args.list_only:
        for cand in candidates[:40]:
            print(f"  {cand['sha'][:12]} k={len(cand['kernel_files'])} "
                  f"d={len(cand['driver_files'])} {cand['subject']}")
        if len(candidates) > 40:
            print(f"  ... and {len(candidates) - 40} more")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_meta: list[dict[str, Any]] = []
    stop = False

    def process(cand: dict[str, Any]) -> list[dict[str, Any]]:
        # per-commit RNG keeps sampling deterministic under thread scheduling;
        # str.__hash__ is salted per process, so derive the seed from the sha hex
        return extract_cases_for_commit(repo, cand, args,
                                        random.Random(args.seed ^ int(cand["sha"][:16], 16)))

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for cases in pool.map(process, candidates):
            for case in cases:
                cases_meta.append(write_case_pack(case, out_dir, args))
                if args.max_cases and len(cases_meta) >= args.max_cases:
                    stop = True
                    break
            if stop:
                break

    print(f"[miner] wrote {len(cases_meta)} case packs to {out_dir / 'cases'}")
    apply_ok = apply_check_sample(cases_meta, out_dir, rng) if cases_meta else (0, 0)
    write_catalog(cases_meta, len(candidates), scanned_range, out_dir, apply_ok, rng)
    print(f"[miner] catalog: {out_dir / 'catalog.json'}; apply --check {apply_ok[0]}/{apply_ok[1]}")
    zero_gold = sum(1 for m in cases_meta if not m["kernel_symbols"])
    if cases_meta and zero_gold / len(cases_meta) > 0.2:
        print(f"[miner] WARNING: zero-gold rate {zero_gold}/{len(cases_meta)} exceeds 20% — "
              "improve symbol extraction before Step 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
