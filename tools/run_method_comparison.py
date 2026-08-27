#!/usr/bin/env python3
"""Run the method-comparison harness over a mined/fixture dataset.

Emits comparison_rows.jsonl (one row per case x method x budget x arm) and
comparison_summary.md (per method/budget/arm aggregates). Offline by default
(mock backend); pass --scorer hf for the real teacher-forced model.

Examples:
    # offline comparison on the fixture-mined dataset:
    python3 tools/run_method_comparison.py --dataset-dir runs/m3_probe/mined
    # subset of methods, two budgets, all ablation arms:
    python3 tools/run_method_comparison.py --dataset-dir runs/m3_probe/mined \
        --methods trunc_baseline,ours --budgets 30,80 --arms full,skeleton,drop,guidance
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.backends import ScoreCache, make_backend  # noqa: E402
from ctxlab.compare import ARMS, aggregate, run_comparison, summary_markdown  # noqa: E402
from ctxlab.methods import method_names  # noqa: E402
from run_ppl_signal_experiment import select_cases  # noqa: E402  (frozen-sample restrict)


def _env(name, default):
    return os.environ.get(name, default)


def build_config(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset-dir", default=_env("DATASET_DIR", str(REPO_ROOT / "runs" / "m3_probe" / "mined")))
    p.add_argument("--out-root", default=_env("OUT_ROOT", str(REPO_ROOT / "runs" / "method_comparison")))
    p.add_argument("--methods", default=",".join(method_names()))
    p.add_argument("--budgets", default="30,80")
    p.add_argument("--arms", default="full")
    p.add_argument("--scorer", default=_env("PPL_SCORER", "mock"),
                   choices=["mock", "hf", "openrouter"])
    p.add_argument("--model", default=os.environ.get("PPL_MODEL"))
    p.add_argument("--max-cases", type=int, default=int(_env("MAX_CASES", "0")))
    p.add_argument("--manifest", default=os.environ.get("PPL_MANIFEST"),
                   help="restrict to the case_ids of a draw_ppl_sample manifest "
                        "(digest-guarded); combine with --max-cases for a subset")
    args = p.parse_args(argv)
    args.method_list = [m for m in args.methods.split(",") if m]
    args.budget_list = [int(b) for b in args.budgets.split(",") if b]
    args.arm_list = [a for a in args.arms.split(",") if a]
    unknown_m = set(args.method_list) - set(method_names())
    unknown_a = set(args.arm_list) - set(ARMS)
    if unknown_m:
        p.error(f"unknown methods: {sorted(unknown_m)}")
    if unknown_a:
        p.error(f"unknown arms: {sorted(unknown_a)} (known: {ARMS})")
    return args


def main(argv=None):
    args = build_config(argv)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    cases = select_cases(args)
    if not cases:
        raise SystemExit(f"[compare] no usable case packs under {args.dataset_dir}")
    backend = make_backend(args.scorer, args.model)
    cache = ScoreCache(out_root / "ppl_cache")
    print(f"[compare] {len(cases)} cases x {args.method_list} x budgets {args.budget_list} "
          f"x arms {args.arm_list} (scorer={args.scorer})")

    rows = run_comparison(cases, args.method_list, args.budget_list, backend, cache, args.arm_list)
    jsonl = out_root / "comparison_rows.jsonl"
    with jsonl.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    agg = aggregate(rows)
    (out_root / "comparison_summary.md").write_text(summary_markdown(agg))
    (out_root / "comparison_aggregate.json").write_text(json.dumps(agg, indent=1))
    print(f"[compare] wrote {jsonl} ({len(rows)} rows) and comparison_summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
