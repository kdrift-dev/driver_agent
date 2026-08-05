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

    # 9b. mock backend orders gold below none (offline PPL signal exists)
    from ctxlab.backends import MockBackend
    from ctxlab.metrics import score_context
    backend = MockBackend()
    gold_beats_none = 0
    for case in cases:
        gold_ppl = score_context(case["gold_text"], case, backend)["ppl"]["nll_per_token"]
        none_ppl = score_context("", case, backend)["ppl"]["nll_per_token"]
        gold_beats_none += gold_ppl < none_ppl
    check("mock backend: gold NLL < none NLL on every case",
          gold_beats_none == len(cases), f"{gold_beats_none}/{len(cases)}")

    # 9c. mock backend is deterministic (identical repeat score)
    c0 = cases[0]
    s1 = score_context(c0["gold_text"], c0, MockBackend())["ppl"]["nll_sum"]
    s2 = score_context(c0["gold_text"], c0, MockBackend())["ppl"]["nll_sum"]
    check("mock backend determinism", s1 == s2, f"{s1} vs {s2}")

    # 9d. method zoo: every method fits budget, keeps the edit locus, and the
    # obligation-aware methods recall gold at least as well as blind/random
    from ctxlab.evaluation import evaluate_selection
    from ctxlab.methods import make_method, method_names
    budget = 40
    per_method_recall = {n: [] for n in method_names()}
    zoo_ok = True
    for case in cases:
        for name in method_names():
            sel = make_method(name).select(case, budget)
            ev = evaluate_selection(sel, case, MockBackend())
            if not sel.locus or not ev["within_budget"]:
                zoo_ok = False
            per_method_recall[name].append(ev["gold_recall"])
    check("method zoo: budget respected + locus kept (all methods, all cases)", zoo_ok)
    mean = {n: sum(v) / len(v) for n, v in per_method_recall.items()}
    check("ours gold-recall >= random_budget",
          mean["ours"] >= mean["random_budget"],
          f"ours={mean['ours']:.2f} random={mean['random_budget']:.2f}")
    check("ours gold-recall >= trunc_baseline",
          mean["ours"] >= mean["trunc_baseline"],
          f"ours={mean['ours']:.2f} trunc={mean['trunc_baseline']:.2f}")

    # 9e. pipeline hook is inert with the flag off, active only when set
    from driver_agent_pregen.context_pruner import (obligation_symbols_from_payload,
                                                    prune_old_module_sources)
    srcs = [{"content": "a alpha_setup", "editable": False},
            {"content": "b ALPHA_SETUP_SYNC filler filler", "editable": False},
            {"content": "loc", "editable": True}]
    obl = obligation_symbols_from_payload([{"required_added_symbols": ["ALPHA_SETUP_SYNC"]}])
    pruned = prune_old_module_sources(srcs, obl)
    check("context_pruner: editable stays first, obl block outranks noise",
          pruned[0]["editable"] is True and "ALPHA_SETUP_SYNC" in pruned[1]["content"],
          str([s["content"][:12] for s in pruned]))
    check("context_pruner: non-destructive (no blocks dropped)",
          len(pruned) == len(srcs))

    # 9f. comparison harness: shape, budget adherence, and the two null arms
    from ctxlab.compare import aggregate, run_comparison
    comp_methods = ["trunc_baseline", "ours"]
    comp_budgets = [30, 80]
    comp_arms = ["full", "skeleton", "drop", "guidance"]
    comp_rows = run_comparison(cases, comp_methods, comp_budgets, MockBackend(),
                               arms=comp_arms)
    expect_rows = len(cases) * len(comp_methods) * len(comp_budgets) * len(comp_arms)
    check("comparison harness row count", len(comp_rows) == expect_rows,
          f"{len(comp_rows)} vs {expect_rows}")
    check("comparison: all cells within budget",
          all(r["within_budget"] for r in comp_rows))
    agg = {(r["method"], r["budget"], r["arm"]): r for r in aggregate(comp_rows)}
    # Sam-Bodden transfer: dropping evidence cannot pass the gate more than full
    drop_gate = agg[("ours", 30, "drop")]["gate_pass_rate"]
    full_gate = agg[("ours", 30, "full")]["gate_pass_rate"]
    check("null arm: drop gate <= full gate", drop_gate <= full_gate,
          f"drop={drop_gate} full={full_gate}")
    # ETH null: generated guidance adds no NLL gain over full under the mock
    g_nll = agg[("ours", 80, "guidance")]["edit_nll"]
    f_nll = agg[("ours", 80, "full")]["edit_nll"]
    check("ETH null: guidance NLL not better than full", g_nll >= f_nll,
          f"guidance={g_nll} full={f_nll}")

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
