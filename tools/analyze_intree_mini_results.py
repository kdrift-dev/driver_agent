#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = WORKFLOW_ROOT / "tools"
for p in (WORKFLOW_ROOT, TOOLS_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from analyze_spec_gap import (  # type: ignore
    ReferenceRepo,
    _affected_functions,
    _discover_run_cases,
    _format_fn_set,
    _generated_changed_functions,
)
from driver_agent_pregen.utils import ensure_dir, read_json, slugify, write_json, write_text


def load_cases(dataset_dir: Path) -> list[dict[str, Any]]:
    manifest = read_json(dataset_dir / "manifest.json", {})
    cases = manifest.get("cases", []) if isinstance(manifest, dict) else []
    if not isinstance(cases, list):
        return []
    return [c for c in cases if isinstance(c, dict)]


def load_patch_text(case_dir: Path) -> tuple[str, str]:
    pg = read_json(case_dir / "patch_generation.json", {})
    if not isinstance(pg, dict):
        return "", ""
    selected_id = pg.get("selected_candidate_id")
    selected = None
    for cand in pg.get("patch_candidates", []):
        if isinstance(cand, dict) and cand.get("candidate_id") == selected_id:
            selected = cand
            break
    if selected is None:
        cands = [c for c in pg.get("patch_candidates", []) if isinstance(c, dict)]
        selected = cands[0] if cands else None
    if not selected:
        return "", ""
    rel = selected.get("fixed_patch_path") or selected.get("raw_patch_path")
    if not rel:
        return "", str(selected_id or "")
    path = case_dir / str(rel)
    if not path.exists():
        return "", str(selected_id or "")
    return path.read_text(encoding="utf-8", errors="replace"), str(selected.get("candidate_id", selected_id or ""))


def diff_added_removed_lines(patch_text: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def semantic_key_status(patch_text: str, keys: dict[str, Any]) -> dict[str, Any]:
    added_lines, removed_lines = diff_added_removed_lines(patch_text)
    added_text = "\n".join(added_lines)
    removed_text = "\n".join(removed_lines)
    required = [str(x) for x in keys.get("required_added_symbols", [])]
    removed_patterns = [str(x) for x in keys.get("expected_removed_patterns", [])]
    present = [x for x in required if x in added_text]
    missing = [x for x in required if x not in added_text]
    removed_present = [x for x in removed_patterns if x in removed_text]
    removed_missing = [x for x in removed_patterns if x not in removed_text]
    return {
        "required_added_symbols": required,
        "required_added_present": present,
        "required_added_missing": missing,
        "expected_removed_patterns": removed_patterns,
        "expected_removed_present": removed_present,
        "expected_removed_missing": removed_missing,
        "semantic_key_match": not missing and not removed_missing,
    }


def stage_status(validation: dict[str, Any], stage: str) -> tuple[str, str]:
    stages = validation.get("validation_stage_results", validation.get("stage_results", []))
    for item in stages:
        if isinstance(item, dict) and item.get("stage") == stage:
            return str(item.get("status", "")).lower(), str(item.get("reason", ""))
    return "", ""


def verdict_from_row(*, run_dir: Path | None, compile_pass: bool, post_verdict: str, semantic_match: bool, missing_reference: set[tuple[str, str]], patch_text: str) -> tuple[str, str]:
    if run_dir is None:
        return "NOT_RUN", "No workflow run was found for this case."
    if not patch_text.strip():
        if post_verdict in {"PASS", "MATCH"}:
            return "POST_PASS_BUT_NO_EFFECTIVE_PATCH", "Post reasoning passed despite no selected patch text."
        return "NO_EFFECTIVE_PATCH", "No selected patch text was found."
    if not compile_pass:
        return "COMPILE_FAIL", "Patch did not pass compile stage."
    if semantic_match and not missing_reference:
        return "LIKELY_CORRECT", "Patch compiled and matched semantic keys and reference-changed functions."
    if post_verdict in {"PASS", "MATCH"} and not semantic_match:
        return "POST_PASS_BUT_SEMANTIC_MISS", "Post reasoning passed, but required semantic keys were missing."
    if post_verdict in {"PASS", "MATCH"} and missing_reference:
        return "POST_PASS_BUT_REFERENCE_MISS", "Post reasoning passed, but generated patch missed reference-changed functions."
    if not semantic_match:
        return "SEMANTIC_KEY_MISS", "Required semantic keys were missing."
    if missing_reference:
        return "REFERENCE_FUNCTION_MISS", "Generated patch missed reference-changed functions."
    return "UNKNOWN", "Insufficient evidence for a stronger verdict."


def latest_runs_by_module(runs_dir: Path, modules: set[str]) -> dict[str, Path]:
    if not runs_dir.exists():
        return {}
    cases = _discover_run_cases(runs_dir, modules_filter=modules, latest_per_module=True)
    out: dict[str, Path] = {}
    for case_dir in cases:
        meta = read_json(case_dir / "metadata.json", {})
        mod = str(meta.get("module_path", "")) if isinstance(meta, dict) else ""
        if mod:
            out[mod] = case_dir
    return out


def write_case_report(out_dir: Path, case: dict[str, Any], row: dict[str, Any], key_status: dict[str, Any]) -> None:
    lines = [
        f"# {case['module_path']}",
        "",
        f"- manual_verdict: `{row['manual_verdict']}`",
        f"- gap_reason: {row['gap_reason']}",
        f"- run_dir: `{row['run_dir']}`",
        f"- compile_pass: `{row['compile_pass']}`",
        f"- post_verdict: `{row['post_verdict']}`",
        f"- semantic_key_match: `{row['semantic_key_match']}`",
        f"- reference_function_overlap: `{row['reference_function_overlap']}`",
        f"- missing_reference_functions: `{row['missing_reference_functions']}`",
        f"- extra_generated_functions: `{row['extra_generated_functions']}`",
        "",
        "## Semantic Keys",
        "",
        f"- required_added_present: `{';'.join(key_status['required_added_present'])}`",
        f"- required_added_missing: `{';'.join(key_status['required_added_missing'])}`",
        f"- expected_removed_present: `{';'.join(key_status['expected_removed_present'])}`",
        f"- expected_removed_missing: `{';'.join(key_status['expected_removed_missing'])}`",
        "",
        "## Notes",
        "",
        case.get("why_kernel_evolution_driven", ""),
    ]
    write_text(out_dir / "spec_gap_case_reports" / f"{slugify(case['module_path'].replace('/', '_'))}.md", "\n".join(lines) + "\n")


def analyze(args: argparse.Namespace) -> None:
    dataset_dir = Path(args.dataset_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    ensure_dir(out_dir / "spec_gap_case_reports")
    cases = load_cases(dataset_dir)
    modules = {str(c.get("module_path")) for c in cases if c.get("module_path")}
    run_map = latest_runs_by_module(Path(args.runs_dir).resolve(), modules) if args.runs_dir else {}
    ref = ReferenceRepo(Path(args.repo).resolve(), args.old_ref, args.new_ref, ignore_comment_only_function_changes=True)

    rows: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}
    for case in cases:
        module_path = str(case["module_path"])
        case_pack_dir = dataset_dir / "cases" / slugify(module_path.replace("/", "_"))
        keys = read_json(case_pack_dir / "expected_semantic_keys.json", {})
        run_dir = run_map.get(module_path)
        patch_text = ""
        candidate_id = ""
        validation: dict[str, Any] = {}
        post_verdict = "NOT_RUN"
        compile_pass = False
        patch_apply_status = "not_run"
        compile_status = "not_run"
        affected = set()
        generated = set()
        if run_dir:
            patch_text, candidate_id = load_patch_text(run_dir)
            validation = read_json(run_dir / "validation.json", {})
            post = validation.get("post_reasoning", {}) if isinstance(validation, dict) else {}
            post_verdict = str(post.get("overall_verdict", "UNKNOWN")).upper() if isinstance(post, dict) else "UNKNOWN"
            patch_apply_status, _ = stage_status(validation, "patch_apply")
            compile_status, _ = stage_status(validation, "compile_modules")
            compile_pass = compile_status == "passed"
            affected = _affected_functions(run_dir)
            generated = _generated_changed_functions(run_dir, ref)
        reference_changed, _comment_only = ref.changed_functions_for_module(module_path)
        missing_reference = reference_changed - generated if run_dir else reference_changed
        extra_generated = generated - reference_changed if run_dir else set()
        overlap = reference_changed & generated if run_dir else set()
        key_status = semantic_key_status(patch_text, keys)
        manual_verdict, gap_reason = verdict_from_row(
            run_dir=run_dir,
            compile_pass=compile_pass,
            post_verdict=post_verdict,
            semantic_match=bool(key_status["semantic_key_match"]),
            missing_reference=missing_reference,
            patch_text=patch_text,
        )
        row = {
            "case_id": case.get("case_id", ""),
            "module_path": module_path,
            "category": case.get("category", ""),
            "run_dir": str(run_dir) if run_dir else "",
            "selected_candidate_id": candidate_id,
            "patch_apply_status": patch_apply_status,
            "compile_status": compile_status,
            "compile_pass": compile_pass,
            "post_verdict": post_verdict,
            "semantic_key_match": key_status["semantic_key_match"],
            "required_added_missing": ";".join(key_status["required_added_missing"]),
            "expected_removed_missing": ";".join(key_status["expected_removed_missing"]),
            "affected_functions": _format_fn_set(affected),
            "reference_changed_functions": _format_fn_set(reference_changed),
            "generated_changed_functions": _format_fn_set(generated),
            "reference_function_overlap": _format_fn_set(overlap),
            "missing_reference_functions": _format_fn_set(missing_reference),
            "extra_generated_functions": _format_fn_set(extra_generated),
            "manual_verdict": manual_verdict,
            "gap_reason": gap_reason,
        }
        rows.append(row)
        verdict_counts[manual_verdict] = verdict_counts.get(manual_verdict, 0) + 1
        write_case_report(out_dir, case, row, key_status)

    csv_path = out_dir / "mini_spec_gap_table.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    summary = {
        "dataset_dir": str(dataset_dir),
        "runs_dir": str(Path(args.runs_dir).resolve()) if args.runs_dir else "",
        "case_count": len(rows),
        "manual_verdict_counts": verdict_counts,
        "notes": [
            "Reference diff is evaluation-only.",
            "Post reasoning is treated as a consistency signal, not a correctness proof.",
            "Semantic-key match is diagnostic and does not require exact textual patch equality.",
        ],
    }
    write_json(out_dir / "mini_spec_gap_summary.json", summary)
    report = [
        "# In-Tree Mini Dataset Result Analysis",
        "",
        f"- cases: `{len(rows)}`",
        f"- table: `{csv_path}`",
        "",
        "## Verdict Counts",
        "",
    ]
    for label, count in sorted(verdict_counts.items()):
        report.append(f"- `{label}`: {count}")
    report.extend(["", "## Interpretation", "", "`PASS` or `MATCH` from post reasoning is not considered a proof of correctness; this analysis explicitly checks semantic keys and reference-changed function coverage.", ""])
    write_text(out_dir / "mini_spec_gap_report.md", "\n".join(report))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Analyze workflow runs against the in-tree mini dataset")
    p.add_argument("--dataset-dir", default="datasets/intree_mini_v0")
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--out-dir", default="runs/intree_mini_v0_analysis")
    return p


def main(argv: list[str] | None = None) -> int:
    analyze(build_parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
