#!/usr/bin/env python3
"""Standing regression gate for the ctxlab research tooling.

Builds the deterministic fixture repo, mines it, and asserts the outcome
against the expectations declared by ctxlab/fixtures.py: expected cases in,
junk commits filtered, gold labels exact, schema valid, loader round-trip,
harness contract intact. Exits nonzero on any failure. Run after every change
to the miner / data layer:

    python3 tools/run_ctxlab_checks.py [--keep] [--work-dir runs/ctxlab_checks]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from ctxlab.dataset import case_dirs, catalog_stats, load_cases, validate_case_dir  # noqa: E402
from ctxlab.fixtures import build_fixture_repo  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work-dir", default=str(TOOLS.parent / "runs" / "ctxlab_checks"))
    p.add_argument("--keep", action="store_true", help="keep the work dir for inspection")
    args = p.parse_args(argv)
    work = Path(args.work_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    expected = build_fixture_repo(work / "repo")
    repo, old_ref, new_ref = expected["repo"], expected["old_ref"], expected["new_ref"]
    mined = work / "mined"

    # 1. miner census
    census = subprocess.run(
        [sys.executable, str(TOOLS / "mine_migration_bench.py"), "--repo", repo,
         "--old-ref", old_ref, "--new-ref", new_ref, "--list-only"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    n_cand = next((int(ln.rsplit(":", 1)[1]) for ln in census.stdout.splitlines()
                   if "candidate commits" in ln), -1)
    check("miner --list-only census", census.returncode == 0
          and n_cand == expected["expected_candidates"],
          f"candidates={n_cand}, want {expected['expected_candidates']}")

    # 2. full mine
    mine = subprocess.run(
        [sys.executable, str(TOOLS / "mine_migration_bench.py"), "--repo", repo,
         "--old-ref", old_ref, "--new-ref", new_ref, "--out-dir", str(mined)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    catalog = json.loads((mined / "catalog.json").read_text()) if mine.returncode == 0 else {}
    check("miner full run", mine.returncode == 0
          and catalog.get("cases") == expected["expected_total_cases"],
          f"cases={catalog.get('cases')}, want {expected['expected_total_cases']}")

    packs = {}
    for d in case_dirs(mined):
        meta = json.loads((d / "case.json").read_text())
        packs.setdefault(meta["module_file"], []).append(meta)

    # 3. expected cases with exact gold labels (api_rename + struct_field)
    for key, want_gold in expected["expected_cases"].items():
        module_file, scenario = key.split("#")
        metas = packs.get(module_file, [])
        hit = [m for m in metas if sorted(m["kernel_symbols"]) == sorted(want_gold)]
        check(f"gold exact: {module_file} ({scenario})", bool(hit),
              f"got {[sorted(m['kernel_symbols']) for m in metas]}, want {sorted(want_gold)}")

    # 4. treewide sampling + scale
    tw = expected["treewide"]
    tw_metas = [m for metas in packs.values() for m in metas
                if sorted(m["kernel_symbols"]) == sorted(tw["gold"])]
    check("treewide sampled under per-commit cap",
          len(tw_metas) == tw["sampled"], f"got {len(tw_metas)}, want {tw['sampled']}")
    check("treewide_scale recorded",
          all(m["treewide_scale"] == tw["scale"] for m in tw_metas),
          f"scales={sorted({m['treewide_scale'] for m in tw_metas})}, want {tw['scale']}")

    # 5. junk commits filtered
    all_subjects = {m["subject"] for metas in packs.values() for m in metas}
    leaked = all_subjects & set(expected["expected_filtered_subjects"])
    check("junk commits filtered", not leaked, f"leaked: {sorted(leaked)}")

    # 6. sanity gauges
    check("zero-gold rate is 0", catalog.get("zero_gold_rate") == 0.0,
          str(catalog.get("zero_gold_rate")))
    ac = catalog.get("apply_check", {})
    check("apply --check clean", ac.get("ok") == ac.get("sampled") and ac.get("sampled", 0) > 0,
          str(ac))

    # 7. schema validation on every pack
    errors = {d.name: validate_case_dir(d) for d in case_dirs(mined)}
    bad = {k: v for k, v in errors.items() if v}
    check("validate_case_dir clean on all packs", not bad, json.dumps(bad) if bad else "")

    # 8. loader round-trip + stats
    cases = load_cases(mined, verbose=False)
    check("loader round-trip", len(cases) == expected["expected_total_cases"]
          and all(c["gold_text"].strip() and c["old_source"].strip() for c in cases),
          f"loaded={len(cases)}")
    stats = catalog_stats(mined)
    check("catalog_stats agrees", stats["cases"] == catalog.get("cases")
          and stats["zero_gold_cases"] == 0, json.dumps(stats))

    # 9. harness contract (expressibility-only end to end)
    gate = subprocess.run(
        [sys.executable, str(TOOLS / "run_ppl_signal_experiment.py"),
         "--dataset-dir", str(mined), "--out-root", str(work / "gate"),
         "--scorer", "expressibility-only"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    summary = (work / "gate" / "experiment_summary.md")
    gold_row = none_row = ""
    if summary.exists():
        for ln in summary.read_text().splitlines():
            if ln.startswith("| gold |"):
                gold_row = ln
            if ln.startswith("| none |"):
                none_row = ln
    check("harness gate profile", gate.returncode == 0
          and "| 100% | 1.000 |" in gold_row and "| 0% |" in none_row,
          f"gold={gold_row!r} none={none_row!r}")

    # 10. determinism of the whole pipeline (rebuild + re-mine, compare ids)
    expected2 = build_fixture_repo(work / "repo2")
    mined2 = work / "mined2"
    subprocess.run(
        [sys.executable, str(TOOLS / "mine_migration_bench.py"), "--repo", expected2["repo"],
         "--old-ref", old_ref, "--new-ref", new_ref, "--out-dir", str(mined2)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    ids1 = sorted(d.name for d in case_dirs(mined))
    ids2 = sorted(d.name for d in case_dirs(mined2))
    check("end-to-end determinism (stable SHAs + sampling)", ids1 == ids2,
          f"{len(ids1)} vs {len(ids2)} ids")

    if not args.keep:
        shutil.rmtree(work)
    print(f"\n{len(FAILURES)} failure(s)" + (f": {FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
