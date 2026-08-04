#!/usr/bin/env python3
"""PPL-signal experiment for context engineering (pre-registered Step 1).

For every case pack in a dataset, scores the reference edit under several
size-matched context conditions and reports whether context quality is
reflected in teacher-forced PPL (and, independently, in the deterministic
expressibility gate). Evaluation-only: reads reference diffs, which the agent
pipeline must never see. Pre-registered success/kill criterion lives in
docs/ppl_signal_preregistration.md — do not tune thresholds here.

Conditions per case (all context conditions truncated to --context-budget):
    gold              gold kernel-side evidence for this case
    gold_distractors  gold + same-case unused evidence
    distractors_only  unused evidence only (negative control)
    random            gold evidence of the *next* case (size-matched)
    none              empty context

Case-pack input (both shapes accepted):
  - mined_migration_bench packs: kernel_evidence.json {"gold": [...], "distractors": [...]}
  - intree_* packs: kernel_evidence.json {"items": [...]} split into gold/distractors
    via expected_semantic_keys.json required_added_symbols.

Environment overrides:
    DATASET_DIR         case-pack dataset root (default datasets/intree_mini_v0)
    OUT_ROOT            output dir             (default runs/ppl_signal_experiment)
    PPL_SCORER          hf|openrouter|expressibility-only (default expressibility-only)
    PPL_MODEL           HF model name          (default Qwen/Qwen2.5-Coder-0.5B)
    PPL_CONTEXT_BUDGET  context chars          (default 8000)
    MAX_CASES           limit cases            (default 0 = all)

Example (local smoke, no ML deps):
    python3 tools/run_ppl_signal_experiment.py --max-cases 2
Example (full, on retro):
    PPL_SCORER=hf PPL_PYTHON=.venv-ppl/bin/python DATASET_DIR=datasets/mined_migration_bench \
    .venv-ppl/bin/python tools/run_ppl_signal_experiment.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ppl_context_scorer import (  # noqa: E402
    ProviderFailure,
    ScoreCache,
    cached_score,
    expressibility_check,
    make_scorer,
)

CONDITIONS = ["gold", "gold_distractors", "distractors_only", "random", "none"]

FIELDS = [
    "case_id", "condition", "scorer", "model",
    "context_chars", "context_truncated",
    "gate_coverage", "gate_pass", "gate_missing_count",
    "nll_sum", "n_target_tokens", "nll_per_token", "ppl",
    "cache_hit", "status",
]

PROMPT_TEMPLATE = (
    "You are migrating a Linux kernel driver from an old kernel version to a "
    "new one. Using the kernel-side evidence below (if any) and the old driver "
    "source, produce the unified diff that adapts the driver to the new kernel.\n\n"
    "=== KERNEL-SIDE EVIDENCE ===\n{context}\n\n"
    "=== OLD DRIVER SOURCE ===\n{old_source}\n\n"
    "=== MIGRATION DIFF ===\n"
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_config(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset-dir", default=_env("DATASET_DIR", str(REPO_ROOT / "datasets" / "intree_mini_v0")))
    p.add_argument("--out-root", default=_env("OUT_ROOT", str(REPO_ROOT / "runs" / "ppl_signal_experiment")))
    p.add_argument("--scorer", default=_env("PPL_SCORER", "expressibility-only"),
                   choices=["expressibility-only", "hf", "openrouter"])
    p.add_argument("--model", default=os.environ.get("PPL_MODEL"))
    p.add_argument("--context-budget", type=int, default=int(_env("PPL_CONTEXT_BUDGET", "8000")))
    p.add_argument("--old-source-budget", type=int, default=int(_env("PPL_OLD_SOURCE_BUDGET", "12000")))
    p.add_argument("--max-cases", type=int, default=int(_env("MAX_CASES", "0")))
    p.add_argument("--conditions", default=",".join(CONDITIONS),
                   help="comma-separated subset of: " + ",".join(CONDITIONS))
    p.add_argument("--reparse", action="store_true",
                   help="rebuild summary from existing experiment_results.csv (no scoring)")
    args = p.parse_args(argv)
    args.condition_list = [c for c in args.conditions.split(",") if c]
    unknown = set(args.condition_list) - set(CONDITIONS)
    if unknown:
        p.error(f"unknown conditions: {sorted(unknown)}")
    return args


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------

def _evidence_text(item: dict[str, Any]) -> str:
    """Render one kernel-evidence item as context text (both pack shapes)."""
    parts = [f"symbol: {item.get('symbol', '?')}"]
    for key in ("definition_diff", "old_definition", "new_definition",
                "old_definition_excerpt", "new_definition_excerpt"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"--- {key} ---\n{val.strip()}")
    for side in ("old_locations", "new_locations"):
        locs = item.get(side) or []
        lines = [f"{loc.get('path')}:{loc.get('line')}: {loc.get('text', '')}"
                 for loc in locs if isinstance(loc, dict)]
        if lines:
            parts.append(f"--- {side} ---\n" + "\n".join(lines))
    return "\n".join(parts)


def load_case(case_dir: Path) -> Optional[dict[str, Any]]:
    case_json = case_dir / "case.json"
    reference = case_dir / "reference.diff"
    evidence_path = case_dir / "kernel_evidence.json"
    if not (case_json.exists() and reference.exists() and evidence_path.exists()):
        return None
    meta = json.loads(case_json.read_text())
    evidence = json.loads(evidence_path.read_text())

    if "gold" in evidence:  # mined_migration_bench shape
        gold_items = evidence.get("gold") or []
        distractor_items = evidence.get("distractors") or []
    else:  # intree_* shape: split items via expected_semantic_keys.json
        items = evidence.get("items") or []
        gold_symbols: set[str] = set()
        keys_path = case_dir / "expected_semantic_keys.json"
        if keys_path.exists():
            keys = json.loads(keys_path.read_text())
            for sym in keys.get("required_added_symbols") or []:
                gold_symbols.add(sym)
            for pat in keys.get("expected_removed_patterns") or []:
                gold_symbols.add(pat.rstrip("(").split("(")[0])
        gold_items = [it for it in items
                      if any(g and g in (it.get("symbol") or "") or (it.get("symbol") or "") in g
                             for g in gold_symbols)]
        distractor_items = [it for it in items if it not in gold_items]

    sources = []
    src_root = case_dir / "old_module_sources"
    if src_root.exists():
        for path in sorted(src_root.rglob("*")):
            if path.is_file() and path.suffix in (".c", ".h"):
                sources.append(path.read_text(errors="replace"))
    if not sources:
        return None
    return {
        "case_id": meta.get("case_id", case_dir.name),
        "reference_diff": reference.read_text(errors="replace"),
        "gold_text": "\n\n".join(_evidence_text(it) for it in gold_items),
        "distractor_text": "\n\n".join(_evidence_text(it) for it in distractor_items),
        "old_source": "\n\n".join(sources),
    }


def load_cases(dataset_dir: Path, max_cases: int) -> list[dict[str, Any]]:
    roots = [dataset_dir / "cases", dataset_dir]
    case_dirs: list[Path] = []
    for root in roots:
        if root.exists():
            case_dirs = sorted(d for d in root.iterdir() if d.is_dir() and (d / "case.json").exists())
            if case_dirs:
                break
    cases = []
    for d in case_dirs:
        case = load_case(d)
        if case is None:
            print(f"[experiment] skip {d.name}: incomplete case pack")
            continue
        cases.append(case)
        if max_cases and len(cases) >= max_cases:
            break
    return cases


def condition_context(case: dict[str, Any], all_cases: list[dict[str, Any]],
                      idx: int, condition: str, budget: int) -> tuple[str, bool]:
    if condition == "gold":
        text = case["gold_text"]
    elif condition == "gold_distractors":
        text = case["gold_text"] + "\n\n" + case["distractor_text"]
    elif condition == "distractors_only":
        text = case["distractor_text"]
    elif condition == "random":
        text = all_cases[(idx + 1) % len(all_cases)]["gold_text"] if len(all_cases) > 1 else ""
    elif condition == "none":
        return "", False
    else:
        raise ValueError(condition)
    truncated = len(text) > budget
    return text[:budget], truncated


# ---------------------------------------------------------------------------
# Scoring loop
# ---------------------------------------------------------------------------

def run_experiment(args: argparse.Namespace) -> list[dict[str, Any]]:
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    cases = load_cases(Path(args.dataset_dir), args.max_cases)
    if not cases:
        raise SystemExit(f"[experiment] no usable case packs under {args.dataset_dir}")
    print(f"[experiment] {len(cases)} cases, conditions={args.condition_list}, scorer={args.scorer}")

    scorer = None
    cache = ScoreCache(out_root / "ppl_cache")
    if args.scorer != "expressibility-only":
        scorer = make_scorer(args.scorer, args.model)

    rows: list[dict[str, Any]] = []
    for idx, case in enumerate(cases):
        # PPL prompt is budgeted; the gate always sees the FULL old source
        # (availability must not depend on the prompt budget).
        old_source = case["old_source"][: args.old_source_budget]
        for condition in args.condition_list:
            context, truncated = condition_context(case, cases, idx, condition, args.context_budget)
            gate = expressibility_check(context, case["old_source"], case["reference_diff"])
            row: dict[str, Any] = {
                "case_id": case["case_id"],
                "condition": condition,
                "scorer": args.scorer,
                "model": getattr(scorer, "model_name", ""),
                "context_chars": len(context),
                "context_truncated": truncated,
                "gate_coverage": gate["coverage"],
                "gate_pass": gate["gate_pass"],
                "gate_missing_count": len(gate["missing_identifiers"]),
                "status": "ok",
            }
            if scorer is not None:
                prompt = PROMPT_TEMPLATE.format(context=context or "(none)", old_source=old_source)
                try:
                    score = cached_score(scorer, cache, prompt, case["reference_diff"])
                    row.update({k: score.get(k) for k in
                                ("nll_sum", "n_target_tokens", "nll_per_token", "ppl", "cache_hit")})
                except ProviderFailure as exc:
                    row["status"] = f"provider_failure: {exc}"
            rows.append(row)
        print(f"[experiment] {idx + 1}/{len(cases)} {case['case_id']} done")
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sign_test_p(wins: int, n: int) -> float:
    """Two-sided binomial sign test against p=0.5."""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n
    return min(1.0, 2 * min(tail, 1 - tail + math.comb(n, wins) / 2 ** n))


def summarize(rows: list[dict[str, Any]]) -> str:
    scored = [r for r in rows if r.get("status") == "ok"]
    excluded = len(rows) - len(scored)
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for r in scored:
        by_case.setdefault(r["case_id"], {})[r["condition"]] = r

    lines = ["# PPL signal experiment summary", ""]
    lines.append(f"cases: {len(by_case)}; rows scored: {len(scored)}; provider/infra excluded: {excluded}")
    lines.append("")
    lines.append("## Per-condition aggregates")
    lines.append("")
    lines.append("| condition | n | gate pass | mean gate coverage | mean nll/token |")
    lines.append("|---|---|---|---|---|")
    conds = sorted({r["condition"] for r in scored}, key=lambda c: CONDITIONS.index(c) if c in CONDITIONS else 99)
    for cond in conds:
        sub = [r for r in scored if r["condition"] == cond]
        gate_rate = sum(1 for r in sub if str(r.get("gate_pass")) in ("True", "true", "1")) / len(sub)
        cov = sum(float(r.get("gate_coverage") or 0) for r in sub) / len(sub)
        nlls = [float(r["nll_per_token"]) for r in sub if r.get("nll_per_token") not in (None, "")]
        nll = f"{sum(nlls) / len(nlls):.4f}" if nlls else "-"
        lines.append(f"| {cond} | {len(sub)} | {gate_rate:.0%} | {cov:.3f} | {nll} |")

    ppl_pairs = [("gold", "random"), ("gold", "none"), ("gold", "distractors_only")]
    lines.append("")
    lines.append("## Pairwise PPL win rates (pre-registered: see docs/ppl_signal_preregistration.md)")
    lines.append("")
    lines.append("| comparison | wins | n | win rate | sign test p |")
    lines.append("|---|---|---|---|---|")
    for a, b in ppl_pairs:
        wins = n = 0
        for per_cond in by_case.values():
            ra, rb = per_cond.get(a), per_cond.get(b)
            if not ra or not rb:
                continue
            va, vb = ra.get("nll_per_token"), rb.get("nll_per_token")
            if va in (None, "") or vb in (None, ""):
                continue
            n += 1
            wins += float(va) < float(vb)
        if n:
            lines.append(f"| PPL({a}) < PPL({b}) | {wins} | {n} | {wins / n:.0%} | {_sign_test_p(wins, n):.4f} |")
        else:
            lines.append(f"| PPL({a}) < PPL({b}) | - | 0 | - | - |")
    lines.append("")
    lines.append("Gate validation expectation: gold ~100% pass, none/random low; a deviating "
                 "profile means the gate or the gold labels need work before trusting PPL rows.")
    return "\n".join(lines) + "\n"


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def main(argv: Optional[list[str]] = None) -> int:
    args = build_config(argv)
    out_root = Path(args.out_root)
    csv_path = out_root / "experiment_results.csv"
    md_path = out_root / "experiment_summary.md"
    if args.reparse:
        if not csv_path.exists():
            raise SystemExit(f"[experiment] --reparse: {csv_path} not found")
        rows = read_csv(csv_path)
    else:
        rows = run_experiment(args)
        write_csv(rows, csv_path)
    md_path.write_text(summarize(rows))
    print(f"[experiment] wrote {csv_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
