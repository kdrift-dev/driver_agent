#!/usr/bin/env python3
"""Run candidate test cases from datasets/test_case_candidates_v0.

This is an experiment harness, not the benchmark definition. It records which
cases are actually suitable for Driver-Agent evaluation under the current
pipeline.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREGEN = ROOT / "pregen_driver_agent.py"
PYTHON = "/usr/bin/python3"


def run(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    start = time.time()
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return {
            "cmd": cmd,
            "returncode": p.returncode,
            "ok": p.returncode == 0,
            "stdout": p.stdout,
            "duration_sec": round(time.time() - start, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "returncode": -1,
            "ok": False,
            "stdout": (exc.stdout or "") + f"\n[timeout] after {timeout} sec",
            "duration_sec": round(time.time() - start, 3),
        }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def slug_from_output(output: str) -> str | None:
    for line in output.splitlines()[::-1]:
        marker = "[driver-agent-pregen] completed: "
        if line.startswith(marker):
            return line[len(marker):].strip()
    return None


def summarize_validation(run_dir: Path) -> dict[str, Any]:
    validation = read_json(run_dir / "validation.json", {}) or {}
    summary_v2 = read_json(run_dir / "validation_summary_v2.json", {}) or {}
    out: dict[str, Any] = {
        "run_dir": str(run_dir),
        "validation_summary": (
            summary_v2.get("overall_validation_result")
            or summary_v2.get("overall_evidence_status")
            or summary_v2.get("overall")
            or validation.get("overall_status")
            or validation.get("status")
        ),
    }
    stages = validation.get("validation_stage_results") or validation.get("stage_results") or validation.get("stages") or {}
    if isinstance(stages, list):
        stages = {s.get("stage") or s.get("name"): s for s in stages if isinstance(s, dict)}
    for key in ["patch_apply", "compile_modules", "qemu_execution", "spec_consistency", "post_reasoning", "test_generation"]:
        stage = stages.get(key) if isinstance(stages, dict) else None
        if isinstance(stage, dict):
            out[f"{key}_status"] = stage.get("status") or ("passed" if stage.get("success") else "failed")
            out[f"{key}_message"] = stage.get("message") or stage.get("reason") or stage.get("summary")
    return out


def summarize_oot_result(run_dir: Path) -> dict[str, Any]:
    result = read_json(run_dir / "result.json", {}) or {}
    verdict = result.get("verdict") or {}
    qemu = result.get("qemu_smoke") or {}
    return {
        "run_dir": str(run_dir),
        "old_build_passes": verdict.get("old_build_passes"),
        "old_source_fails_on_new_kernel": verdict.get("old_source_fails_on_new_kernel"),
        "patch_applies": verdict.get("patch_applies"),
        "patched_build_passes_new_kernel": verdict.get("patched_build_passes_new_kernel"),
        "qemu_smoke_passes": verdict.get("qemu_smoke_passes"),
        "case_study_success": verdict.get("case_study_success"),
        "qemu_reason": qemu.get("reason"),
        "selected_iteration": result.get("selected_iteration"),
    }


def oot_check_script(case: dict[str, str]) -> str | None:
    q = case.get("qemu_testability", "")
    if case["case_id"] == "oot_lddbus_v61_v70":
        return str(ROOT / "datasets" / "oot_qemu_cases" / "lddbus_sysfs_check.sh")
    if "dummy_char" in case["case_id"]:
        return str(ROOT / "datasets" / "test_case_candidates_v0" / "qemu_checks" / "dev_dummy_char.sh")
    if "lkmc" in case["case_id"]:
        return str(ROOT / "datasets" / "test_case_candidates_v0" / "qemu_checks" / "dev_lkmc_character_device_create.sh")
    if "embetronicx" in case["case_id"]:
        return str(ROOT / "datasets" / "test_case_candidates_v0" / "qemu_checks" / "dev_etx_device.sh")
    return None


def build_command(case: dict[str, str], out_dir: Path, args: argparse.Namespace) -> list[str]:
    base = [
        "sudo", "-n", PYTHON, str(PREGEN),
        "--repo", case["kernel_repo"],
        "--old-ref", case["kernel_old_ref"],
        "--new-ref", case["kernel_new_ref"],
        "--out-dir", str(out_dir),
        "--jobs", str(args.jobs),
        "--enable-llm",
        "--llm-provider", args.llm_provider,
        "--llm-model", args.llm_model,
        "--config-ini", str(ROOT / "config.ini"),
        "--llm-timeout-sec", str(args.llm_timeout_sec),
        "--llm-max-retries", str(args.llm_max_retries),
        "--patch-candidates", "1",
        "--patch-prompt-strength", "strict",
        "--enable-qemu",
        "--qemu-timeout-sec", str(args.qemu_timeout_sec),
        "--cleanup-worktrees",
    ]
    if case["case_kind"] == "intree":
        cmd = base + [
            "--module-path", case["module_path"],
            "--kernel-config-target", "allmodconfig",
            "--max-patch-iterations", str(args.max_patch_iterations),
            "--patch-source-mode", "full_module",
        ]
        if "qemu_sufficient" not in case.get("qemu_testability", ""):
            cmd.append("--skip-system-test-generation")
        return cmd

    cmd = base + [
        "--oot-pipeline-mode", "parallel",
        "--oot-repo-url", case["oot_repo_url"],
        "--oot-repo-ref", case["oot_repo_ref"],
        "--oot-source-subdir", case["oot_source_subdir"],
        "--oot-module-ko", case["module_ko"],
        "--oot-module-name", case["module_name"],
        "--oot-max-patch-iterations", str(args.max_patch_iterations),
    ]
    if case.get("device_node"):
        cmd += ["--oot-device-node", case["device_node"]]
    script = oot_check_script(case)
    if script:
        cmd += ["--oot-qemu-check-script", script]
    if case.get("make_extra_arg"):
        cmd += ["--oot-make-extra-arg", case["make_extra_arg"]]
    return cmd


def classify(case: dict[str, str], res: dict[str, Any], summary: dict[str, Any]) -> str:
    if not res.get("ok"):
        return "pipeline_failed"
    if case["case_kind"] == "oot":
        if summary.get("case_study_success"):
            return "suitable_qemu_confirmed"
        if summary.get("patched_build_passes_new_kernel"):
            return "suitable_compile_only"
        return "not_suitable_or_pipeline_bug"
    cm = str(summary.get("compile_modules_status") or "").lower()
    qm = str(summary.get("qemu_execution_status") or "").lower()
    overall = str(summary.get("validation_summary") or "")
    if overall == "PASS_ALL_VALIDATION":
        return "suitable_qemu_confirmed" if qm == "passed" else "suitable_compile_confirmed"
    if cm == "passed" and qm == "passed":
        return "suitable_qemu_confirmed"
    if cm == "passed":
        return "suitable_compile_confirmed"
    return "not_suitable_or_pipeline_bug"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(ROOT / "datasets/test_case_candidates_v0/manifest.csv"))
    ap.add_argument("--out-dir", default=str(ROOT / "runs" / ("candidate_v0_fullcheck_" + time.strftime("%Y%m%d_%H%M%S"))))
    ap.add_argument("--case-id", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--llm-provider", default="openrouter")
    ap.add_argument("--llm-model", default="anthropic/claude-sonnet-4.6")
    ap.add_argument("--llm-timeout-sec", type=int, default=300)
    ap.add_argument("--llm-max-retries", type=int, default=2)
    ap.add_argument("--qemu-timeout-sec", type=int, default=1800)
    ap.add_argument("--max-patch-iterations", type=int, default=3)
    ap.add_argument("--timeout-sec", type=int, default=10800)
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(args.manifest, newline="", encoding="utf-8")))
    if args.case_id:
        wanted = set(args.case_id)
        rows = [r for r in rows if r["case_id"] in wanted]
    results: list[dict[str, Any]] = []
    print(f"[candidate-runner] out={out_dir}")
    print(f"[candidate-runner] cases={len(rows)}")
    for i, case in enumerate(rows, 1):
        print(f"[candidate-runner] {i}/{len(rows)} {case['case_id']}", flush=True)
        case_log_dir = out_dir / "logs"
        case_log_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_command(case, out_dir, args)
        (case_log_dir / f"{case['case_id']}.cmd.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
        res = run(cmd, timeout=args.timeout_sec)
        (case_log_dir / f"{case['case_id']}.log").write_text(res.get("stdout", ""), encoding="utf-8")
        run_path = slug_from_output(res.get("stdout", "") or "")
        summary: dict[str, Any]
        if run_path:
            run_dir = Path(run_path)
            summary = summarize_oot_result(run_dir) if case["case_kind"] == "oot" else summarize_validation(run_dir)
        else:
            summary = {"run_dir": None}
        rec = {
            **{k: case.get(k, "") for k in ["case_id", "case_kind", "kernel_old_ref", "kernel_new_ref", "module_path", "oot_repo_url", "oot_source_subdir", "kernel_failure_signal", "qemu_testability"]},
            "command_ok": res.get("ok"),
            "returncode": res.get("returncode"),
            "duration_sec": res.get("duration_sec"),
            **summary,
        }
        rec["suitability"] = classify(case, res, summary)
        results.append(rec)
        write_json(out_dir / "results.json", results)
        with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
            fieldnames = sorted({k for r in results for k in r})
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(results)
        print(f"[candidate-runner] result {case['case_id']} {rec['suitability']} run={rec.get('run_dir')}", flush=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["suitability"]] = counts.get(r["suitability"], 0) + 1
    write_json(out_dir / "summary.json", {"out_dir": str(out_dir), "counts": counts, "results": results})
    print(json.dumps({"out_dir": str(out_dir), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
