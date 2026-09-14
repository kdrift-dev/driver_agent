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
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
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

    # 9d. Methods share the exact rendered prompt budget and preserve source.
    # These checks assert contracts, never that a research method wins.
    from ctxlab.evaluation import evaluate_selection
    from ctxlab.methods import make_method, method_names
    from ctxlab.metrics import build_prompt, token_count
    budget = 512
    zoo_ok = True
    for case in cases:
        for name in method_names():
            sel = make_method(name, backend=backend).select(case, budget)
            ev = evaluate_selection(sel, case, backend)
            if (not sel.locus or not ev["within_budget"] or ev["status"] != "ok"
                    or ev["old_source_text"] != case["old_source"]
                    or ev["prompt_text"] != build_prompt(ev["context_text"], ev["old_source_text"])
                    or ev["tokens_kept"] != backend.count_tokens(ev["prompt_text"])):
                zoo_ok = False
    check("method zoo: exact prompt budget, source and scoring contract", zoo_ok)
    check("offline token proxy counts repetitions and punctuation",
          token_count("same same same();") == 6 and token_count("same same") > token_count("same"))

    class SpyBackend(MockBackend):
        def __init__(self):
            super().__init__("explicit-spy")
            self.calls = []

        def count_tokens(self, text):
            # Different counter proves that the harness uses the supplied one.
            return 2 * super().count_tokens(text)

        def score(self, prompt, target):
            self.calls.append((prompt, target))
            return super().score(prompt, target)

    for name in ("longcodezip", "keepdrop_dd", "shapley_probe"):
        rejected = False
        try:
            make_method(name)
        except ValueError:
            rejected = True
        spy = SpyBackend()
        from ctxlab.compare import run_comparison
        row = run_comparison([c0], [name], [4096], spy)[0]
        check(f"explicit selection backend: {name}", rejected and len(spy.calls) >= 2
              and row["selection_model"] == "explicit-spy"
              and row["tokens_kept"] == spy.count_tokens(row["prompt_text"])
              and spy.calls[-1][0] == row["prompt_text"]
              and all(build_prompt("", row["old_source_text"]).split("=== OLD DRIVER SOURCE ===", 1)[1]
                      == call[0].split("=== OLD DRIVER SOURCE ===", 1)[1] for call in spy.calls))

    tiny_spy = SpyBackend()
    tiny = run_comparison([c0], ["ours"], [1], tiny_spy)[0]
    check("insufficient budget preserves source and never scores", tiny["status"] == "insufficient_budget"
          and not tiny["within_budget"] and tiny["old_source_text"] == c0["old_source"]
          and tiny["edit_nll"] is None and not tiny_spy.calls)

    source_case = {"old_source": "visible hidden", "reference_diff": "+hidden\n"}
    legacy = score_context("", source_case, old_source_budget=7)
    exact = score_context("", source_case, old_source_text="visible")
    check("v2 gate sees the exact rendered source; legacy remains reproducible",
          legacy["gate"]["gate_pass"] and not exact["gate"]["gate_pass"]
          and exact["old_source_text"] == "visible")

    class TruncatingBackend(MockBackend):
        def score(self, prompt, target):
            value = super().score(prompt, target)
            value["prompt_truncated"] = True
            return value

    truncated = score_context("", c0, TruncatingBackend(), old_source_text=c0["old_source"], strict_prompt=True)
    check("silent backend truncation is excluded", truncated["ppl"] is None
          and truncated["status"].startswith("context_window_exceeded"))
    window_spy = SpyBackend()
    window_spy.max_input_tokens = 2
    overflow = run_comparison([c0], ["longcodezip"], [4096], window_spy)[0]
    check("model-window overflow never reaches backend", overflow["edit_nll"] is None
          and "context_window_exceeded" in overflow["status"] and not window_spy.calls)

    # Changing only evaluator labels or reference text cannot affect deployable selectors.
    import copy
    relabeled = copy.deepcopy(c0)
    relabeled["gold_items"], relabeled["distractor_items"] = list(reversed(c0["distractor_items"])), list(reversed(c0["gold_items"]))
    relabeled["meta"]["kernel_symbols"] = ["POISON_GOLD_LABEL"]
    relabeled["reference_diff"] = "+POISON_REFERENCE\n"
    for name in ("trunc_baseline", "random_budget", "errprune", "static_epam", "ours"):
        first = make_method(name, backend=backend).select(c0, 512, arm="guidance")
        other = make_method(name, backend=backend).select(relabeled, 512, arm="guidance")
        check(f"labels/reference hidden: {name}", first.prompt_text() == other.prompt_text()
              and [c.id for c in first.evidence] == [c.id for c in other.evidence]
              and all(not c.is_gold and c.id.startswith("evidence:") for c in first.evidence))

    sample_path = work / "seed_case.json"
    sample_path.write_text(json.dumps(c0))
    seed_code = ("import json,sys;sys.path[:0]=[sys.argv[1],sys.argv[2]];"
                 "from ctxlab.methods import make_method;"
                 "c=json.load(open(sys.argv[3]));"
                 "s=make_method('random_budget',seed=31).select(c,512);"
                 "print(json.dumps([x.id for x in s.evidence]))")
    picks = [subprocess.run([sys.executable, "-c", seed_code, str(TOOLS), str(TOOLS.parent), str(sample_path)],
                            env={**os.environ, "PYTHONHASHSEED": salt}, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
             for salt in ("1", "12345")]
    check("random selection is stable across Python hash seeds",
          all(p.returncode == 0 for p in picks) and picks[0].stdout == picks[1].stdout)

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

    # 9f. Every ablation is budgeted after rendering (including guidance).
    from ctxlab.compare import aggregate, run_comparison
    comp_methods = ["trunc_baseline", "ours"]
    comp_budgets = [128, 512]
    comp_arms = ["full", "skeleton", "drop", "guidance"]
    comp_rows = run_comparison(cases, comp_methods, comp_budgets, backend, arms=comp_arms)
    expect_rows = len(cases) * len(comp_methods) * len(comp_budgets) * len(comp_arms)
    check("comparison harness row count", len(comp_rows) == expect_rows,
          f"{len(comp_rows)} vs {expect_rows}")
    check("comparison: all arms count exact rendered prompt",
          all(r["prompt_tokens"] == backend.count_tokens(r["prompt_text"])
              and (r["within_budget"] or r["status"] == "insufficient_budget")
              and r["old_source_text"] == next(c["old_source"] for c in cases if c["case_id"] == r["case_id"])
              for r in comp_rows))
    check("drop arm has no evidence", all(r["context_text"] == "" and r["n_evidence"] == 0
                                         for r in comp_rows if r["arm"] == "drop"))
    excluded = aggregate([tiny])[0]
    check("invalid rows excluded from quality denominators", excluded["n_valid"] == 0
          and excluded["n_excluded"] == 1 and excluded["edit_nll"] is None
          and excluded["gate_pass_rate"] is None)

    # Rendered evidence coverage must not see discarded chunk bodies.
    rendered_case = {"case_id": "rendered-coverage", "old_source": "int source_only;",
                     "meta": {"kernel_symbols": ["new_api"]},
                     "reference_diff": "+new_api();\n", "distractor_items": [],
                     "gold_items": [{"symbol": "wrapper", "new_definition": "void new_api(void);"}]}
    rendered_rows = {r["arm"]: r for r in run_comparison(
        [rendered_case], ["trunc_baseline"], [512], backend, arms=["full", "skeleton", "drop"])}
    check("obligation recall uses rendered evidence, not hidden body symbols",
          rendered_rows["full"]["obligation_recall"] == 1.0
          and rendered_rows["skeleton"]["gold_recall"] == 1.0
          and rendered_rows["skeleton"]["obligation_recall"] == 0.0
          and rendered_rows["drop"]["obligation_recall"] == 0.0)

    # Hidden body padding must not change skeleton gains or value density.
    skeleton_case = {"case_id": "skeleton-gain", "old_source": "int old_api(void);",
                     "meta": {}, "reference_diff": "+old_api();\n", "distractor_items": [],
                     "gold_items": [{"symbol": "old_api", "new_definition": "int old_api(void);"}]}
    padded_skeleton = copy.deepcopy(skeleton_case)
    padded_skeleton["gold_items"][0]["new_definition"] += "\n// padding" * 3000
    skeleton_picks = [make_method("ours", backend=backend).select(c, 512, arm="skeleton")
                      for c in (skeleton_case, padded_skeleton)]
    check("skeleton OACD gain ignores unrendered body padding",
          all(len(s.evidence) == 1 for s in skeleton_picks)
          and skeleton_picks[0].context_text() == skeleton_picks[1].context_text()
          and all(s.evidence[0].tokens == token_count(s.evidence[0].text, backend) for s in skeleton_picks))
    density_case = {"case_id": "skeleton-density", "old_source": "int existing;", "meta": {},
                    "reference_diff": "+new_a(new_a(new_a(new_b)));\n", "distractor_items": [],
                    "gold_items": [{"symbol": name, "new_definition": f"void {name}(void);"}
                                   for name in ("new_a", "new_b")]}
    padded_density = copy.deepcopy(density_case)
    padded_density["gold_items"][0]["new_definition"] += "\n// padding" * 3000
    density_budget = token_count(build_prompt("symbol: new_a", density_case["old_source"]), backend)
    density_picks = [make_method("longcodezip", backend=backend).select(c, density_budget, arm="skeleton")
                     for c in (density_case, padded_density)]
    check("skeleton oracle density charges rendered tokens",
          all(s.context_text() == "symbol: new_a" for s in density_picks))

    # Two source files share line numbers: oracle localization must select b.
    from ctxlab.dataset import load_case
    multi_dir = work / "multifile_case"
    multi_src = multi_dir / "old_module_sources" / "drivers"
    multi_src.mkdir(parents=True)
    for name in ("a", "b"):
        (multi_src / f"{name}.c").write_text(
            f"static int {name}(void)\n{{\n return old_{name}();\n}}\n")
    (multi_dir / "case.json").write_text(json.dumps({"case_id": "multi", "module_file": "drivers/b.c"}))
    (multi_dir / "kernel_evidence.json").write_text('{"gold": [], "distractors": []}')
    (multi_dir / "reference.diff").write_text(
        "--- a/drivers/b.c\n+++ b/drivers/b.c\n@@ -3 +3 @@\n- return old_b();\n+ return new_b();\n")
    multi = load_case(multi_dir)
    oracle_sel = make_method("trunc_baseline", backend=backend, locus_source="oracle_reference").select(multi, 512)
    check("oracle locus respects source file boundaries",
          sorted(multi["old_sources_by_path"]) == ["drivers/a.c", "drivers/b.c"]
          and len(oracle_sel.locus) == 1 and oracle_sel.locus[0].id == "locus:drivers/b.c:b"
          and "old_b" in oracle_sel.old_source_text() and "old_a" not in oracle_sel.old_source_text())
    unmapped = dict(multi)
    del unmapped["old_sources_by_path"]
    fallback = make_method("trunc_baseline", backend=backend, locus_source="oracle_reference").select(unmapped, 512)
    check("unmapped oracle source uses explicit whole-source fallback",
          fallback.meta["locus_whole_source_fallback"]
          and fallback.old_source_text() == multi["old_source"])

    # 9g. frozen-sample manifest restriction scores exactly the sampled ids
    from draw_ppl_sample import build_manifest
    man = build_manifest(mined, n=2, seed=0)
    man_path = work / "sample_manifest.json"
    man_path.write_text(json.dumps(man))
    want_ids = set(man["case_ids"])
    mrun = subprocess.run(
        [sys.executable, str(TOOLS / "run_ppl_signal_experiment.py"),
         "--dataset-dir", str(mined), "--out-root", str(work / "manifest_run"),
         "--scorer", "expressibility-only", "--manifest", str(man_path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    mcsv = work / "manifest_run" / "experiment_results.csv"
    got_ids: set[str] = set()
    if mcsv.exists():
        with mcsv.open() as fh:
            got_ids = {r["case_id"] for r in csv.DictReader(fh)}
    check("manifest restrict: only sampled case_ids scored",
          mrun.returncode == 0 and len(want_ids) == 2 and got_ids == want_ids,
          f"got={sorted(got_ids)} want={sorted(want_ids)}")

    # 9h. digest guard rejects a manifest drawn from a different mine
    tampered = dict(man, dataset_digest="0" * 64)
    tam_path = work / "tampered_manifest.json"
    tam_path.write_text(json.dumps(tampered))
    trun = subprocess.run(
        [sys.executable, str(TOOLS / "run_ppl_signal_experiment.py"),
         "--dataset-dir", str(mined), "--out-root", str(work / "tampered_run"),
         "--scorer", "expressibility-only", "--manifest", str(tam_path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check("manifest digest guard: mismatch is fatal",
          trun.returncode != 0 and "digest" in trun.stdout.lower(),
          f"rc={trun.returncode}")

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
