#!/usr/bin/env python3
"""Run selected cases from testcase_catalog.csv.

The runner is intentionally simple and sequential. It records the exact command,
stdout/stderr, completed run directory, and validation_summary_v2 status for
each case so failed cases remain auditable.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


FIELDNAMES = [
    "case_id",
    "kind",
    "module_path",
    "returncode",
    "duration_sec",
    "run_dir",
    "overall_validation_result",
    "not_a_correctness_proof",
    "stage_summary",
    "error",
]


def _read_catalog(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _cmd_for_case(row: dict[str, str], args: argparse.Namespace, case_out: Path) -> list[str]:
    old_ref = row.get("kernel_old_ref") or args.old_ref
    new_ref = row.get("kernel_new_ref") or args.new_ref
    patch_source_mode = row.get("patch_source_mode") or args.patch_source_mode
    cmd = [
        sys.executable,
        str(Path(args.workflow_dir) / "pregen_driver_agent.py"),
        "--repo",
        args.repo,
        "--old-ref",
        old_ref,
        "--new-ref",
        new_ref,
        "--module-path",
        row["module_path"],
        "--out-dir",
        str(case_out),
        "--jobs",
        str(args.jobs),
        "--patch-candidates",
        str(args.patch_candidates),
        "--patch-source-mode",
        patch_source_mode,
        "--patch-prompt-strength",
        args.patch_prompt_strength,
        "--patch-source-with-lineno",
        "--qemu-timeout-sec",
        str(args.qemu_timeout_sec),
        "--cleanup-worktrees",
    ]
    if args.enable_llm:
        cmd += [
            "--enable-llm",
            "--llm-provider",
            args.llm_provider,
            "--llm-model",
            args.llm_model,
            "--config-ini",
            args.config_ini,
            "--llm-timeout-sec",
            str(args.llm_timeout_sec),
            "--llm-max-retries",
            str(args.llm_max_retries),
        ]
    if args.disable_qemu:
        cmd.append("--disable-qemu")
    else:
        cmd.append("--enable-qemu")
    if args.skip_system_test_generation:
        cmd.append("--skip-system-test-generation")

    if row.get("kind") == "oot":
        cmd += [
            "--oot-pipeline-mode",
            "integrated",
            "--oot-repo-url",
            row["repo_url"],
            "--oot-repo-ref",
            row["repo_ref"],
            "--oot-source-subdir",
            row["source_subdir"],
            "--oot-module-ko",
            row["module_ko"],
            "--oot-module-name",
            row["module_name"],
            "--oot-qemu-test-mode",
            "auto",
            "--oot-qemu-test-level",
            "interface_smoke" if row.get("qemu_scope") == "qemu_sufficient" else "load_unload",
            "--oot-max-patch-iterations",
            str(args.oot_max_patch_iterations),
        ]
    return cmd


def _summarize_run_dir(run_dir: str) -> tuple[str, str, str]:
    if not run_dir:
        return "", "", ""
    rd = Path(run_dir)
    summary = _read_json(rd / "validation_summary_v2.json")
    status = str(summary.get("overall_validation_result") or summary.get("overall_evidence_status", ""))
    proof = str(summary.get("not_a_correctness_proof", ""))
    stage_summary = ""
    p = rd / "validation" / "stage_summary.txt"
    if p.exists():
        stage_summary = " | ".join(line.strip() for line in p.read_text().splitlines() if line.strip())
    return status, proof, stage_summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog-csv", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workflow-dir", default="/mnt/disk7/jw_ai4os/driver-agent/workflow")
    ap.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    ap.add_argument("--old-ref", default="v6.19")
    ap.add_argument("--new-ref", default="v7.0-rc1")
    ap.add_argument("--case-ids-file")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--enable-llm", action="store_true")
    ap.add_argument("--llm-provider", default="openrouter")
    ap.add_argument("--llm-model", default="anthropic/claude-sonnet-4.6")
    ap.add_argument("--config-ini", default="/mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini")
    ap.add_argument("--llm-timeout-sec", type=int, default=240)
    ap.add_argument("--llm-max-retries", type=int, default=2)
    ap.add_argument("--patch-candidates", type=int, default=1)
    ap.add_argument("--patch-source-mode", default="focused", choices=["focused", "full_module"])
    ap.add_argument("--patch-prompt-strength", default="strict", choices=["normal", "strict"])
    ap.add_argument("--qemu-timeout-sec", type=int, default=1800)
    ap.add_argument("--disable-qemu", action="store_true")
    ap.add_argument("--skip-system-test-generation", action="store_true")
    ap.add_argument("--case-timeout-sec", type=int, default=3600)
    ap.add_argument("--oot-max-patch-iterations", type=int, default=2)
    args = ap.parse_args()

    rows = _read_catalog(Path(args.catalog_csv))
    if args.case_ids_file:
        wanted = [x.strip() for x in Path(args.case_ids_file).read_text().splitlines() if x.strip()]
        by_id = {r["case_id"]: r for r in rows}
        selected = [by_id[x] for x in wanted if x in by_id]
    else:
        selected = [r for r in rows if r.get("recommended_run") == "yes"]
    selected = selected[: args.limit]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "selected_cases.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
        writer.writeheader()
        for row in selected:
            writer.writerow(row)

    results: list[dict[str, Any]] = []
    log_path = out_dir / "run.log"
    with log_path.open("a", encoding="utf-8") as log:
        for idx, row in enumerate(selected, start=1):
            case_out = out_dir / "pregen"
            cmd = _cmd_for_case(row, args, case_out)
            log.write(f"\n[CASE {idx}/{len(selected)}] {row['case_id']} {row['module_path']}\n")
            log.write("[CMD] " + " ".join(cmd) + "\n")
            log.flush()
            t0 = time.time()
            proc = subprocess.run(
                cmd,
                cwd=args.workflow_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.case_timeout_sec,
            )
            duration = round(time.time() - t0, 3)
            log.write(proc.stdout)
            if proc.stdout and not proc.stdout.endswith("\n"):
                log.write("\n")
            m = re.search(r"\[driver-agent-pregen\] completed: (.+)", proc.stdout)
            run_dir = m.group(1).strip() if m else ""
            status, proof, stage_summary = _summarize_run_dir(run_dir)
            result = {
                "case_id": row["case_id"],
                "kind": row["kind"],
                "module_path": row["module_path"],
                "returncode": proc.returncode,
                "duration_sec": duration,
                "run_dir": run_dir,
                "overall_validation_result": status,
                "not_a_correctness_proof": proof,
                "stage_summary": stage_summary,
                "error": "" if proc.returncode == 0 else "command_failed",
            }
            results.append(result)
            log.write("[RESULT] " + json.dumps(result, ensure_ascii=False) + "\n")
            log.flush()

    with (out_dir / "case_results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    (out_dir / "case_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = {
        "total": len(results),
        "returncode_ok": sum(int(r["returncode"]) == 0 for r in results),
        "overall_validation_result_counts": {},
        "results_csv": str(out_dir / "case_results.csv"),
    }
    for row in results:
        key = row.get("overall_validation_result") or "missing"
        summary["overall_validation_result_counts"][key] = summary["overall_validation_result_counts"].get(key, 0) + 1
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
