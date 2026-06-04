#!/usr/bin/env python3
"""Run showcase in-tree/OOT Driver-Agent cases from manifest CSV files.

This runner is intentionally small and auditable:
- it preserves exact argv per case;
- it supports Kbuild target scoped in-tree cases;
- it limits case-level concurrency so kernel builds/QEMU do not overload the host;
- it records stage summaries without treating spec consistency as correctness.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PREGEN = ROOT / "pregen_driver_agent.py"
DEFAULT_REPO = "/mnt/disk7/jw_ai4os/linux_source_code/linux.git"
PYTHON = "/usr/bin/python3"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_manifest(path: Path, *, kind: str | None = None, primary_only: bool = True) -> tuple[str, list[dict[str, str]]]:
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    if not rows:
        return kind or "unknown", []
    if not kind:
        cols = set(rows[0])
        if {"module_path", "module_target", "old_ref", "new_ref"} <= cols:
            kind = "intree"
        elif {"repo_url", "repo_ref", "source_subdir", "module_ko", "old_kernel_ref", "new_kernel_ref"} <= cols:
            kind = "oot"
        else:
            raise SystemExit(f"cannot infer manifest kind from columns: {sorted(cols)}")
    if primary_only and "role" in rows[0]:
        rows = [r for r in rows if (r.get("role") or "").strip() == "primary"]
    return kind, rows


def completed_run_dir(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        marker = "[driver-agent-pregen] completed: "
        if line.startswith(marker):
            return line[len(marker):].strip()
    m = re.search(r"\[driver-agent-pregen\] completed: (.+)", stdout)
    return m.group(1).strip() if m else ""


def summarize_validation(run_dir: str) -> dict[str, Any]:
    if not run_dir:
        return {}
    rd = Path(run_dir)
    validation = read_json(rd / "validation.json")
    summary_v2 = read_json(rd / "validation_summary_v2.json")
    metadata = read_json(rd / "metadata.json")

    stages = validation.get("validation_stage_results") or validation.get("stage_results") or validation.get("stages") or {}
    if isinstance(stages, list):
        stages = {str(s.get("stage") or s.get("name")): s for s in stages if isinstance(s, dict)}
    if not isinstance(stages, dict):
        stages = {}

    out: dict[str, Any] = {
        "run_dir": run_dir,
        "overall_validation_result": (
            summary_v2.get("overall_validation_result")
            or summary_v2.get("overall_evidence_status")
            or validation.get("overall_status")
            or validation.get("status")
            or ""
        ),
        "not_a_correctness_proof": summary_v2.get("not_a_correctness_proof", ""),
        "patch_iterations": len(metadata.get("patch_iterations", []) or []),
    }
    for key in [
        "patch_apply",
        "compile_modules",
        "compile_autofix",
        "test_generation",
        "suite_execution",
        "qemu_execution",
        "migration_obligation_check",
        "spec_consistency",
        "post_reasoning",
    ]:
        stage = stages.get(key)
        if isinstance(stage, dict):
            out[f"{key}_status"] = stage.get("status") or ("passed" if stage.get("success") else "failed")
            out[f"{key}_message"] = stage.get("message") or stage.get("reason") or stage.get("summary") or ""
    return out


def infer_v4l2loopback_dependency_args(row: dict[str, str]) -> list[str]:
    """Add known dependency modules for the v4l2loopback QEMU smoke case."""
    if "v4l2loopback" not in (row.get("case_id", "") + " " + row.get("module_name", "")):
        return []
    return [
        "--oot-kernel-config",
        "MEDIA_SUPPORT=m",
        "--oot-kernel-config",
        "VIDEO_DEV=m",
        "--oot-kernel-config",
        "MEDIA_CONTROLLER=y",
        "--oot-kernel-config",
        "MEDIA_CAMERA_SUPPORT=y",
        "--oot-kernel-module-dir",
        "drivers/media/mc",
        "--oot-kernel-module-dir",
        "drivers/media/v4l2-core",
        "--oot-qemu-check-script",
        str(ROOT / "datasets/oot_qemu_cases/v4l2loopback_v0_15_1_v614_to_v70/qemu_check.sh"),
    ]


def build_cmd(kind: str, row: dict[str, str], args: argparse.Namespace, out_dir: Path) -> list[str]:
    if kind == "intree":
        old_ref = row["old_ref"]
        new_ref = row["new_ref"]
        cmd = [
            "sudo",
            "-n",
            PYTHON,
            str(PREGEN),
            "--repo",
            args.repo,
            "--old-ref",
            old_ref,
            "--new-ref",
            new_ref,
            "--module-path",
            row["module_path"],
            "--kbuild-target",
            row["module_target"],
            "--out-dir",
            str(out_dir / "pregen"),
        ]
    elif kind == "oot":
        old_ref = row["old_kernel_ref"]
        new_ref = row["new_kernel_ref"]
        cmd = [
            "sudo",
            "-n",
            PYTHON,
            str(PREGEN),
            "--repo",
            args.repo,
            "--old-ref",
            old_ref,
            "--new-ref",
            new_ref,
            "--out-dir",
            str(out_dir / "pregen"),
            "--oot-pipeline-mode",
            args.oot_pipeline_mode,
            "--oot-repo-url",
            row["repo_url"],
            "--oot-repo-ref",
            row["repo_ref"],
            "--oot-source-subdir",
            row["source_subdir"],
            "--oot-module-ko",
            row["module_ko"],
            "--oot-module-name",
            row.get("module_name") or row["module_ko"].removesuffix(".ko"),
        ]
        if row.get("device_node"):
            cmd += ["--oot-device-node", row["device_node"]]
        if row.get("qemu_test_level"):
            cmd += ["--oot-qemu-test-level", row["qemu_test_level"]]
        cmd += infer_v4l2loopback_dependency_args(row)
    else:
        raise ValueError(kind)

    kernel_config_target = (
        args.oot_kernel_config_target
        if kind == "oot" and args.oot_kernel_config_target
        else args.kernel_config_target
    )
    cmd += [
        "--jobs",
        str(args.jobs),
        "--kernel-config-target",
        kernel_config_target,
        "--patch-candidates",
        "1",
        "--max-patch-iterations",
        str(args.max_patch_iterations),
        "--oot-max-patch-iterations",
        str(args.max_patch_iterations),
        "--patch-source-mode",
        args.patch_source_mode,
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
    return cmd


def run_one(kind: str, row: dict[str, str], args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    case_id = row["case_id"]
    cmd = build_cmd(kind, row, args, out_dir)
    log_dir = out_dir / "logs"
    cmd_dir = out_dir / "commands"
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd_dir.mkdir(parents=True, exist_ok=True)
    write_json(cmd_dir / f"{case_id}.json", cmd)

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=args.case_timeout_sec,
        )
        stdout = proc.stdout or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") + f"\n[TIMEOUT] after {args.case_timeout_sec}s\n"
        returncode = -1

    (log_dir / f"{case_id}.log").write_text(stdout, encoding="utf-8", errors="replace")
    run_dir = completed_run_dir(stdout)
    summary = summarize_validation(run_dir)
    rec: dict[str, Any] = {
        "case_id": case_id,
        "kind": kind,
        "returncode": returncode,
        "command_ok": returncode == 0,
        "duration_sec": round(time.time() - t0, 3),
        "old_ref": row.get("old_ref") or row.get("old_kernel_ref") or "",
        "new_ref": row.get("new_ref") or row.get("new_kernel_ref") or "",
        "module_path": row.get("module_path", ""),
        "module_target": row.get("module_target", ""),
        "repo_url": row.get("repo_url", ""),
        "repo_ref": row.get("repo_ref", ""),
        "source_subdir": row.get("source_subdir", ""),
        "module_ko": row.get("module_ko", ""),
        **summary,
    }
    rec["paper_showcase_status"] = classify_for_showcase(rec)
    return rec


def classify_for_showcase(rec: dict[str, Any]) -> str:
    compile_status = str(rec.get("compile_modules_status", "")).lower()
    qemu_status = str(rec.get("qemu_execution_status", "")).lower()
    overall = str(rec.get("overall_validation_result", ""))
    if compile_status == "passed" and qemu_status == "passed":
        return "strong_runtime_candidate"
    if compile_status == "passed":
        return "compile_candidate_runtime_missing"
    if rec.get("command_ok") and rec.get("run_dir"):
        return "pipeline_completed_patch_failed"
    if overall:
        return "pipeline_completed_validation_failed"
    return "pipeline_or_timeout_failure"


def write_results(out_dir: Path, results: list[dict[str, Any]]) -> None:
    write_json(out_dir / "results.json", results)
    fieldnames = sorted({k for row in results for k in row})
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    counts: dict[str, int] = {}
    for row in results:
        key = row.get("paper_showcase_status") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    write_json(
        out_dir / "summary.json",
        {
            "out_dir": str(out_dir),
            "total": len(results),
            "counts": counts,
            "results_csv": str(out_dir / "results.csv"),
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", action="append", required=True, help="manifest CSV; may be repeated")
    ap.add_argument("--manifest-kind", choices=["intree", "oot"], help="force kind for a single manifest")
    ap.add_argument("--out-dir", default=str(ROOT / "runs" / ("showcase_dataset_run_" + time.strftime("%Y%m%d_%H%M%S"))))
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--primary-only", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--case-id", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-workers", type=int, default=2)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--kernel-config-target", choices=["defconfig", "allmodconfig", "allyesconfig"], default="allmodconfig")
    ap.add_argument(
        "--oot-kernel-config-target",
        choices=["defconfig", "allmodconfig", "allyesconfig"],
        default="defconfig",
        help="kernel config target for OOT cases; defaults to defconfig because OOT manifests add required dependency CONFIGs explicitly",
    )
    ap.add_argument("--enable-llm", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--llm-provider", default="deepseek")
    ap.add_argument("--llm-model", default="deepseek-v4-pro")
    ap.add_argument("--config-ini", default=str(ROOT / "config.ini"))
    ap.add_argument("--llm-timeout-sec", type=int, default=300)
    ap.add_argument("--llm-max-retries", type=int, default=2)
    ap.add_argument("--max-patch-iterations", type=int, default=3)
    ap.add_argument("--patch-source-mode", choices=["focused", "full_module"], default="focused")
    ap.add_argument("--patch-prompt-strength", choices=["normal", "strict"], default="strict")
    ap.add_argument("--qemu-timeout-sec", type=int, default=1800)
    ap.add_argument("--case-timeout-sec", type=int, default=10800)
    ap.add_argument("--disable-qemu", action="store_true")
    ap.add_argument("--skip-system-test-generation", action="store_true")
    ap.add_argument("--oot-pipeline-mode", choices=["integrated", "parallel"], default="integrated")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cases: list[tuple[str, dict[str, str]]] = []
    for manifest in args.manifest:
        kind, rows = read_manifest(Path(manifest), kind=args.manifest_kind, primary_only=args.primary_only)
        cases.extend((kind, row) for row in rows)
    if args.case_id:
        wanted = set(args.case_id)
        cases = [(kind, row) for kind, row in cases if row.get("case_id") in wanted]
    if args.limit > 0:
        cases = cases[: args.limit]

    write_json(
        out_dir / "run_config.json",
        {
            "manifests": args.manifest,
            "case_count": len(cases),
            "max_workers": args.max_workers,
            "jobs": args.jobs,
            "llm_provider": args.llm_provider,
            "llm_model": args.llm_model,
            "qemu_enabled": not args.disable_qemu,
        },
    )
    print(f"[showcase-runner] out={out_dir}")
    print(f"[showcase-runner] cases={len(cases)} max_workers={args.max_workers} jobs={args.jobs}", flush=True)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        future_map = {
            pool.submit(run_one, kind, row, args, out_dir): row.get("case_id", "unknown")
            for kind, row in cases
        }
        for future in as_completed(future_map):
            case_id = future_map[future]
            try:
                rec = future.result()
            except Exception as exc:
                rec = {"case_id": case_id, "paper_showcase_status": "runner_exception", "error": str(exc)}
            results.append(rec)
            write_results(out_dir, results)
            print(
                "[showcase-runner] result "
                f"{rec.get('case_id')} status={rec.get('paper_showcase_status')} "
                f"compile={rec.get('compile_modules_status')} qemu={rec.get('qemu_execution_status')} "
                f"run={rec.get('run_dir')}",
                flush=True,
            )

    write_results(out_dir, results)
    print(json.dumps(read_json(out_dir / "summary.json"), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
