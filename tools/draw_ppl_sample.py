#!/usr/bin/env python3
"""Draw the pre-registered PPL sample: N cases with >=1 gold symbol, stratified
by treewide_scale, deterministically (seed + dataset digest -> frozen manifest).

Evaluation-only selection over mined case packs. Writes a manifest the frozen
pre-registration (docs/ppl_signal_preregistration.md) pins by digest, and that
tools/run_ppl_signal_experiment.py can restrict to. No scoring, no ML deps.

Stratification: cases are bucketed by treewide_scale; buckets are filled in a
deterministic round-robin (largest-bucket-first, seeded shuffle within a
bucket) until N are drawn or the eligible pool is exhausted. Fully reproducible
for a given (dataset, N, seed).

  python3 tools/draw_ppl_sample.py --dataset-dir runs/mined --n 200 --seed 0 \
      --out runs/mined/ppl_sample_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.dataset import case_dirs  # noqa: E402
from ctxlab.provenance import CONTENT_DIGEST_ALGORITHM, case_fingerprint  # noqa: E402


def eligible_cases(dataset_dir: Path) -> list[dict[str, Any]]:
    """case_id / treewide_scale / gold_count for every pack with >=1 gold."""
    out: list[dict[str, Any]] = []
    for d in case_dirs(dataset_dir):
        try:
            meta = json.loads((d / "case.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        gold = meta.get("kernel_symbols") or []
        if not gold:
            continue
        out.append({
            "case_id": meta.get("case_id", d.name),
            "treewide_scale": int(meta.get("treewide_scale", 0)),
            "gold_count": len(gold),
            "content_sha256": case_fingerprint(d)["sha256"],
        })
    out.sort(key=lambda c: c["case_id"])  # deterministic base order
    if len({c["case_id"] for c in out}) != len(out):
        raise ValueError("duplicate eligible case_ids")
    return out


def legacy_dataset_digest(cases: list[dict[str, Any]]) -> str:
    """Historical v1 ID-only digest; retained for frozen manifest replay."""
    h = hashlib.sha256()
    for c in cases:  # already sorted by case_id
        h.update(c["case_id"].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def dataset_digest(cases: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    h.update(CONTENT_DIGEST_ALGORITHM.encode() + b"\x00")
    for case in sorted(cases, key=lambda c: c["case_id"]):
        h.update(case["case_id"].encode() + b"\x00")
        h.update(case["content_sha256"].encode() + b"\x00")
    return h.hexdigest()


def stratified_sample(cases: list[dict[str, Any]], n: int, seed: int) -> list[str]:
    """Round-robin over treewide_scale buckets (largest bucket first), seeded
    shuffle within each bucket. Deterministic for (cases, n, seed)."""
    buckets: dict[int, list[str]] = {}
    for c in cases:
        buckets.setdefault(c["treewide_scale"], []).append(c["case_id"])
    rng = random.Random(seed)
    for scale in buckets:
        rng.shuffle(buckets[scale])
    # largest bucket first, ties broken by scale value for stability
    order = sorted(buckets, key=lambda s: (-len(buckets[s]), s))
    selected: list[str] = []
    exhausted = False
    while len(selected) < n and not exhausted:
        exhausted = True
        for scale in order:
            if buckets[scale]:
                selected.append(buckets[scale].pop())
                exhausted = False
                if len(selected) >= n:
                    break
    return selected


def build_manifest(dataset_dir: Path, n: int, seed: int) -> dict[str, Any]:
    if n <= 0:
        raise ValueError("sample size must be positive")
    cases = eligible_cases(dataset_dir)
    if not cases:
        raise SystemExit(f"[sample] no eligible (>=1 gold) case packs under {dataset_dir}")
    selected = stratified_sample(cases, n, seed)
    selected_set = set(selected)
    by_scale = {c["case_id"]: c["treewide_scale"] for c in cases}
    strata: dict[str, int] = {}
    for cid in selected:
        strata[str(by_scale[cid])] = strata.get(str(by_scale[cid]), 0) + 1
    return {
        "tool": "draw_ppl_sample_v2",
        "schema_version": 2,
        "digest_algorithm": CONTENT_DIGEST_ALGORITHM,
        "dataset_dir": str(dataset_dir),
        "dataset_digest": dataset_digest(cases),
        "legacy_id_digest": legacy_dataset_digest(cases),
        "case_content_sha256": {c["case_id"]: c["content_sha256"]
                                for c in cases if c["case_id"] in selected_set},
        "eligible_cases": len(cases),
        "n_requested": n,
        "n_selected": len(selected),
        "seed": seed,
        "strata_by_treewide_scale": dict(sorted(strata.items(), key=lambda kv: int(kv[0]))),
        "case_ids": sorted(selected),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset-dir", required=True)
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=None, help="manifest path (default: stdout only)")
    args = p.parse_args(argv)

    manifest = build_manifest(Path(args.dataset_dir), args.n, args.seed)
    text = json.dumps(manifest, indent=1)
    if args.out:
        with Path(args.out).open("x") as fh:
            fh.write(text + "\n")
        print(f"[sample] wrote {args.out}: {manifest['n_selected']}/{manifest['eligible_cases']} "
              f"eligible, strata={manifest['strata_by_treewide_scale']}, "
              f"digest={manifest['dataset_digest'][:12]}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
