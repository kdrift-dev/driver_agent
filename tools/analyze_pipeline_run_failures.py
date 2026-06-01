#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def stage_map(validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(x.get("stage")): x for x in validation.get("validation_stage_results", []) if isinstance(x, dict)}


def selected_candidate(pg: dict[str, Any]) -> dict[str, Any]:
    cands = pg.get("patch_candidates") or []
    sid = pg.get("selected_candidate_id")
    for c in cands:
        if c.get("candidate_id") == sid:
            return c
    return cands[0] if cands else {}


def classify(row: dict[str, str], run_dir: Path) -> tuple[str, str]:
    validation = read_json(run_dir / "validation.json")
    stages = stage_map(validation)
    pg = read_json(run_dir / "patch_generation.json")
    cand = selected_candidate(pg)

    source_files = int(float(row.get("source_file_count") or 0))
    code_loc = int(float(row.get("code_loc") or 0))
    if source_files > 80 or code_loc > 20000:
        return "CASE_SCOPE_TOO_BROAD", f"source_file_count={source_files}, code_loc={code_loc} exceeds single-module practical budget"

    if not (pg.get("patch_candidates") or []):
        parse = pg.get("parse_status", "")
        return "NO_PATCH_CANDIDATE", f"patch_generation produced no candidate; parse_status={parse}"

    if cand.get("no_op_candidate") and row.get("compile_modules_status") == "passed" and row.get("qemu_execution_status") == "passed":
        return "NOOP_RUNTIME_PASS_NOT_HARDBREAK", "selected no-op candidate compiled and runtime test passed; case is not a clean hard-break under this validation config"

    if row.get("compile_modules_status") == "failed":
        return "PATCH_COMPILE_FAILURE", row.get("compile_modules_reason", "compile failed")

    if row.get("qemu_execution_status") == "failed":
        qemu = validation.get("qemu") or {}
        reason = qemu.get("reason") or row.get("qemu_execution_reason") or "runtime failed"
        summary = read_json(run_dir / "validation_summary_v2.json")
        for level in summary.get("levels", []) or []:
            if level.get("name") != "runtime_hooks":
                continue
            for check in level.get("checks", []) or []:
                if check.get("status") != "FAIL":
                    continue
                ev = check.get("evidence") or {}
                if isinstance(ev, dict) and ev.get("first_failure"):
                    raw_failure = str(ev.get("first_failure"))
                    try:
                        failure_obj = json.loads(raw_failure)
                        reason = "structured_run_status=" + str(failure_obj.get("status") or "failed")
                    except Exception:
                        reason = raw_failure
                    break
        return "RUNTIME_OR_TEST_BUILD_FAILURE", str(reason)[:260]

    if row.get("qemu_execution_status") == "skipped":
        return "RUNTIME_NOT_AVAILABLE", row.get("qemu_execution_reason") or "runtime skipped"

    if row.get("overall_validation_result") == "FAILED_VALIDATION":
        return "OBLIGATION_OR_SCOPE_VALIDATION_FAILURE", "build/runtime may pass, but migration obligation or edit-scope validation failed"

    return "PASS_OR_INCOMPLETE", row.get("overall_validation_result", "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage Driver-Agent batch results into actionable failure buckets")
    ap.add_argument("--run-dir", required=True, help="batch directory containing results.csv")
    ap.add_argument("--out-dir", default="", help="output dir; defaults to run-dir/triage")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else run_dir / "triage"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader((run_dir / "results.csv").open(newline="", encoding="utf-8")))

    records = []
    counts = Counter()
    for row in rows:
        rd = Path(row.get("run_dir", ""))
        label, reason = classify(row, rd)
        rec = dict(row)
        rec["triage_label"] = label
        rec["triage_reason"] = reason
        records.append(rec)
        counts[label] += 1

    fields = sorted({k for r in records for k in r})
    with (out_dir / "pipeline_triage.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(records)
    summary = {"run_dir": str(run_dir), "case_count": len(records), "counts": dict(counts)}
    (out_dir / "pipeline_triage_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = ["# Pipeline Run Triage", "", f"Run: `{run_dir}`", "", "## Counts", ""]
    for label, count in counts.most_common():
        lines.append(f"- {label}: {count}")
    lines.extend(["", "## Cases", ""])
    for rec in records:
        lines.append(f"- `{rec.get('case_id')}`: **{rec['triage_label']}** - {rec['triage_reason']}")
    (out_dir / "pipeline_triage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
