#!/usr/bin/env python3
"""Run the four-layer Driver-Agent experiment for one case.

Layers:
  P0: problem reproduction, old target source staged into new kernel.
  B0: source-only LLM baseline.
  B1: old source + compact kernel-side evidence baseline.
  D: full Driver-Agent pipeline.

This script is an experiment harness. It does not feed reference/new target
driver diffs into agent inputs. Reference diffs are written only under
evaluation/.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PREGEN = ROOT / "pregen_driver_agent.py"


SOURCE_SUFFIXES = {
    ".c",
    ".h",
    ".rs",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".hh",
    ".S",
    ".s",
}


def slugify(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif ch in {"/", "-", ".", "_"}:
            out.append("_")
        else:
            out.append("_")
    return "_".join("".join(out).strip("_").split("_")) or "case"


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
        return {
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_sec": round(time.time() - start, 3),
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "returncode": -1,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\n[timeout] after {timeout} sec",
            "duration_sec": round(time.time() - start, 3),
            "ok": False,
        }


def truncate_run(res: dict[str, Any], max_chars: int = 20000) -> dict[str, Any]:
    out = dict(res)
    for key in ("stdout", "stderr"):
        text = str(out.get(key, "") or "")
        if len(text) > max_chars:
            out[key] = text[-max_chars:] + "\n...[truncated]"
    return out


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def prepare_worktree(repo: Path, ref: str, dest: Path) -> dict[str, Any]:
    if dest.exists():
        run_cmd(["git", "-C", str(repo), "worktree", "remove", "-f", str(dest)], timeout=300)
        if dest.exists():
            shutil.rmtree(dest)
    return run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), ref], timeout=900)


def remove_worktree(repo: Path, dest: Path) -> None:
    if dest.exists():
        run_cmd(["git", "-C", str(repo), "worktree", "remove", "-f", str(dest)], timeout=300)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)


def git_lines(repo: Path, args: list[str]) -> list[str]:
    out = subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL)
    return [x.strip() for x in out.splitlines() if x.strip()]


def old_source_files(repo: Path, old_ref: str, module_path: str) -> list[str]:
    files = git_lines(repo, ["ls-tree", "-r", "--name-only", old_ref, "--", module_path])
    return [p for p in files if Path(p).suffix in SOURCE_SUFFIXES]


def find_makefile_dirs(tree: Path, module_path: str, files: list[str]) -> list[str]:
    dirs: list[str] = []
    candidates = sorted({Path(p).parent.as_posix() for p in files} | {module_path.rstrip("/")})
    for rel in candidates:
        cur = tree / rel
        while cur != tree and tree in cur.parents:
            if (cur / "Makefile").exists() or (cur / "Kbuild").exists():
                r = cur.relative_to(tree).as_posix()
                if r not in dirs:
                    dirs.append(r)
                break
            cur = cur.parent
    return dirs or [module_path.rstrip("/")]


def run_intree_p0(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    p0_dir = run_dir / "P0_problem_reproduction"
    old_tree = p0_dir / "worktrees" / "old"
    new_tree = p0_dir / "worktrees" / "new"
    p0_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "layer": "P0",
        "case_kind": "intree",
        "module_path": args.module_path,
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "steps": [],
    }

    wt_old = prepare_worktree(repo, args.old_ref, old_tree)
    wt_new = prepare_worktree(repo, args.new_ref, new_tree)
    result["worktrees"] = {"old": truncate_run(wt_old), "new": truncate_run(wt_new)}
    if not wt_old.get("ok") or not wt_new.get("ok"):
        result.update({"status": "failed", "hard_break_reproduced": False, "reason": "worktree_prepare_failed"})
        write_json(p0_dir / "result.json", result)
        return result

    files = old_source_files(repo, args.old_ref, args.module_path)
    copied: list[str] = []
    for rel in files:
        src = old_tree / rel
        dst = new_tree / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    result["old_source_file_count"] = len(files)
    result["copied_files"] = copied

    steps = [
        run_cmd(["make", args.kernel_config_target], cwd=new_tree, timeout=1800),
        run_cmd(["make", "olddefconfig"], cwd=new_tree, timeout=1800),
        run_cmd(["make", "modules_prepare"], cwd=new_tree, timeout=2400),
    ]
    prep_ok = all(s.get("ok") for s in steps)
    compile_runs: list[dict[str, Any]] = []
    compile_ok = False
    if prep_ok:
        dirs = find_makefile_dirs(new_tree, args.module_path, copied)
        compile_ok = True
        for d in dirs:
            r = run_cmd(["make", f"-j{max(1, args.jobs)}", f"M={d}", "modules"], cwd=new_tree, timeout=3600)
            compile_runs.append(r)
            if not r.get("ok"):
                compile_ok = False
    else:
        dirs = []
    result["steps"] = [truncate_run(s) for s in steps]
    result["compile"] = {
        "prepare_success": prep_ok,
        "compiled_dirs": dirs,
        "runs": [truncate_run(r) for r in compile_runs],
        "success": compile_ok,
    }
    result["hard_break_reproduced"] = not compile_ok
    result["status"] = "old_source_fails_on_new_kernel" if not compile_ok else "old_source_builds_on_new_kernel"
    result["interpretation"] = (
        "hard API/build break reproduced"
        if not compile_ok
        else "no build-level hard break reproduced; this may be source-compatible semantic migration or a case requiring stronger runtime/semantic oracle"
    )

    if args.cleanup_worktrees:
        remove_worktree(repo, old_tree)
        remove_worktree(repo, new_tree)
    write_json(p0_dir / "result.json", result)
    return result


def run_oot_p0(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    # Use the existing integrated OOT pre-patch diagnostics stage so we do not
    # duplicate OOT repository export/build logic here.
    p0_out = run_dir / "P0_problem_reproduction"
    cmd = pregen_base_cmd(args, p0_out, patch_mode="agent", dry_run=True, layer="P0")
    cmd.extend(["--disable-qemu", "--skip-system-test-generation"])
    res = run_cmd(cmd, cwd=ROOT, timeout=args.layer_timeout_sec)
    latest = find_latest_run(p0_out)
    diag = read_json(latest / "extracted" / "oot_pre_patch_build_diagnostics.json", {}) if latest else {}
    summary = {
        "layer": "P0",
        "case_kind": "oot",
        "module_path": args.module_path,
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "command": cmd,
        "run_result": truncate_run(res),
        "run_dir": latest.as_posix() if latest else "",
        "diagnostics": diag,
        "hard_break_reproduced": bool((diag.get("summary") or {}).get("old_source_fails_on_new_kernel")),
        "status": "old_source_fails_on_new_kernel"
        if bool((diag.get("summary") or {}).get("old_source_fails_on_new_kernel"))
        else "old_source_did_not_fail_on_new_kernel_or_unknown",
    }
    write_json(p0_out / "result.json", summary)
    return summary


def pregen_base_cmd(
    args: argparse.Namespace,
    out_dir: Path,
    *,
    patch_mode: str,
    dry_run: bool = False,
    layer: str = "",
) -> list[str]:
    cmd = [
        sys.executable,
        str(PREGEN),
        "--repo",
        args.repo,
        "--old-ref",
        args.old_ref,
        "--new-ref",
        args.new_ref,
        "--module-path",
        args.module_path,
        "--out-dir",
        str(out_dir),
        "--jobs",
        str(args.jobs),
        "--patch-candidates",
        str(args.patch_candidates),
        "--patch-generation-mode",
        patch_mode,
        "--patch-prompt-strength",
        args.patch_prompt_strength,
        "--patch-source-mode",
        args.patch_source_mode,
        "--qemu-timeout-sec",
        str(args.qemu_timeout_sec),
        "--kernel-config-target",
        args.kernel_config_target,
        "--llm-timeout-sec",
        str(args.llm_timeout_sec),
        "--llm-max-retries",
        str(args.llm_max_retries),
        "--llm-retry-backoff-sec",
        str(args.llm_retry_backoff_sec),
    ]
    if dry_run:
        cmd.append("--dry-run")
    elif args.enable_llm:
        cmd.extend(
            [
                "--enable-llm",
                "--llm-provider",
                args.llm_provider,
                "--llm-model",
                args.llm_model,
            ]
        )
        if args.config_ini:
            cmd.extend(["--config-ini", args.config_ini])
    runtime_policy = str(args.runtime_policy or "all")
    disable_runtime = bool(
        args.disable_qemu
        or runtime_policy == "none"
        or (runtime_policy == "d-only" and layer and layer != "D")
    )
    if disable_runtime:
        cmd.append("--disable-qemu")
    else:
        cmd.append("--enable-qemu")
    if args.skip_system_test_generation:
        cmd.append("--skip-system-test-generation")
    if args.cleanup_worktrees:
        cmd.append("--cleanup-worktrees")
    if args.case_kind == "oot":
        cmd.extend(
            [
                "--oot-pipeline-mode",
                "integrated",
                "--oot-repo-url",
                args.oot_repo_url,
                "--oot-repo-ref",
                args.oot_repo_ref,
                "--oot-source-subdir",
                args.oot_source_subdir,
                "--oot-module-ko",
                args.oot_module_ko,
                "--oot-module-name",
                args.oot_module_name,
                "--oot-device-node",
                args.oot_device_node,
                "--oot-qemu-write-data",
                args.oot_qemu_write_data,
                "--oot-qemu-expect-data",
                args.oot_qemu_expect_data,
                "--oot-qemu-test-mode",
                args.oot_qemu_test_mode,
                "--oot-qemu-test-level",
                args.oot_qemu_test_level,
            ]
        )
        for extra in args.oot_make_extra_arg or []:
            cmd.extend(["--oot-make-extra-arg", extra])
    return cmd


def find_latest_run(out_dir: Path) -> Path | None:
    metas = sorted(out_dir.rglob("metadata.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return metas[0].parent if metas else None


def summarize_pipeline_run(layer: str, mode: str, out_dir: Path, cmd: list[str], res: dict[str, Any]) -> dict[str, Any]:
    latest = find_latest_run(out_dir)
    validation = read_json(latest / "validation.json", {}) if latest else {}
    summary_v2 = read_json(latest / "validation_summary_v2.json", {}) if latest else {}
    patch_generation = read_json(latest / "patch_generation.json", {}) if latest else {}
    candidate = {}
    cands = patch_generation.get("patch_candidates") or []
    selected_id = patch_generation.get("selected_candidate_id") or (cands[0].get("candidate_id") if cands else "")
    for cand in cands:
        if cand.get("candidate_id") == selected_id:
            candidate = cand
            break
    if not candidate and cands:
        candidate = cands[0]
    qemu = validation.get("qemu") or {}
    compile_payload = validation.get("compile") or {}
    return {
        "layer": layer,
        "patch_generation_mode": mode,
        "command": cmd,
        "process_ok": bool(res.get("ok")),
        "process_returncode": res.get("returncode"),
        "process_duration_sec": res.get("duration_sec"),
        "run_dir": latest.as_posix() if latest else "",
        "metadata_status": (read_json(latest / "metadata.json", {}) or {}).get("status") if latest else "",
        "validation_status": validation.get("status", ""),
        "overall_evidence_status": summary_v2.get("overall_evidence_status", ""),
        "compile_success": bool(compile_payload.get("success")),
        "qemu_enabled": bool(qemu.get("enabled")),
        "qemu_success": bool(qemu.get("success")),
        "qemu_runs": len(qemu.get("runs") or []),
        "post_verdict": ((validation.get("post_reasoning") or {}).get("overall_verdict") or ""),
        "post_parse_status": ((validation.get("post_reasoning") or {}).get("parse_status") or ""),
        "candidate_id": candidate.get("candidate_id", ""),
        "no_op_candidate": bool(candidate.get("no_op_candidate")),
        "touched_files": candidate.get("touched_files", []),
        "open_questions": validation.get("open_questions", []),
        "stdout_tail": str(res.get("stdout", ""))[-4000:],
        "stderr_tail": str(res.get("stderr", ""))[-4000:],
    }


def run_method_layer(args: argparse.Namespace, run_dir: Path, layer: str, mode: str) -> dict[str, Any]:
    out_dir = run_dir / layer
    cmd = pregen_base_cmd(args, out_dir, patch_mode=mode, dry_run=False, layer=layer)
    res = run_cmd(cmd, cwd=ROOT, timeout=args.layer_timeout_sec)
    summary = summarize_pipeline_run(layer, mode, out_dir, cmd, res)
    write_json(out_dir / "layer_summary.json", summary)
    return summary


def write_reference_diff(args: argparse.Namespace, run_dir: Path) -> None:
    if args.case_kind != "intree":
        return
    repo = Path(args.repo).resolve()
    out = run_cmd(
        ["git", "-C", str(repo), "diff", f"{args.old_ref}..{args.new_ref}", "--", args.module_path],
        timeout=600,
    )
    write_text(run_dir / "evaluation" / "reference.diff", out.get("stdout", ""))
    write_json(run_dir / "evaluation" / "reference_diff_result.json", truncate_run(out))


def write_summary(run_dir: Path, args: argparse.Namespace, p0: dict[str, Any], layers: list[dict[str, Any]]) -> None:
    summary = {
        "schema_version": "four_layer_experiment_v1",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "case_kind": args.case_kind,
        "module_path": args.module_path,
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "input_policy": {
            "P0": "old target source staged into new kernel for reproduction only",
            "B0": "old target module source only",
            "B1": "old target module source plus compact kernel-side definition/diff evidence",
            "D": "full Driver-Agent affected/spec/reasoning workflow",
            "evaluation_only": "reference diffs are saved only under evaluation/ and are not agent inputs",
        },
        "P0": p0,
        "layers": layers,
    }
    write_json(run_dir / "four_layer_summary.json", summary)

    with (run_dir / "four_layer_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "layer",
            "patch_generation_mode",
            "run_dir",
            "process_ok",
            "validation_status",
            "overall_evidence_status",
            "compile_success",
            "qemu_success",
            "qemu_runs",
            "post_verdict",
            "post_parse_status",
            "no_op_candidate",
            "touched_files",
            "open_questions",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in layers:
            out = {k: row.get(k, "") for k in fields}
            out["touched_files"] = ";".join(row.get("touched_files") or [])
            out["open_questions"] = ";".join(row.get("open_questions") or [])
            writer.writerow(out)

    lines = [
        f"# Four-Layer Experiment: {args.module_path}",
        "",
        f"- Case kind: `{args.case_kind}`",
        f"- Release pair: `{args.old_ref} -> {args.new_ref}`",
        f"- P0 status: `{p0.get('status')}`",
        f"- P0 hard break reproduced: `{p0.get('hard_break_reproduced')}`",
        "",
        "| Layer | Mode | Process | Validation | Evidence | Compile | QEMU | Post | No-op |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in layers:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.get('layer')}`",
                    f"`{row.get('patch_generation_mode')}`",
                    str(row.get("process_ok")),
                    f"`{row.get('validation_status')}`",
                    f"`{row.get('overall_evidence_status')}`",
                    str(row.get("compile_success")),
                    f"{row.get('qemu_success')} ({row.get('qemu_runs')})",
                    f"`{row.get('post_verdict') or row.get('post_parse_status')}`",
                    str(row.get("no_op_candidate")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `post_verdict` is spec consistency, not a correctness proof.",
            "- For source-compatible semantic migrations, P0 may build successfully; record this as a case property rather than forcing a hard-break claim.",
            "- Reference diff, if present, is evaluation-only.",
        ]
    )
    write_text(run_dir / "four_layer_report.md", "\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run P0/B0/B1/D four-layer Driver-Agent experiment")
    p.add_argument("--case-kind", choices=["intree", "oot"], default="intree")
    p.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    p.add_argument("--old-ref", required=True)
    p.add_argument("--new-ref", required=True)
    p.add_argument("--module-path", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--patch-candidates", type=int, default=1)
    p.add_argument("--patch-prompt-strength", choices=["normal", "strict"], default="strict")
    p.add_argument("--patch-source-mode", choices=["focused", "full_module"], default="focused")
    p.add_argument("--enable-llm", action="store_true")
    p.add_argument("--llm-provider", default="openrouter")
    p.add_argument("--llm-model", default="anthropic/claude-sonnet-4.6")
    p.add_argument("--config-ini", default="/mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini")
    p.add_argument("--llm-timeout-sec", type=int, default=240)
    p.add_argument("--llm-max-retries", type=int, default=1)
    p.add_argument("--llm-retry-backoff-sec", type=int, default=3)
    p.add_argument("--layer-timeout-sec", type=int, default=7200)
    p.add_argument("--qemu-timeout-sec", type=int, default=1200)
    p.add_argument(
        "--kernel-config-target",
        choices=["defconfig", "allmodconfig", "allyesconfig"],
        default="defconfig",
        help="kernel config target for P0 and validation compile; use allmodconfig for hard-break cases",
    )
    p.add_argument("--disable-qemu", action="store_true")
    p.add_argument(
        "--runtime-policy",
        choices=["all", "d-only", "none"],
        default="all",
        help="which method layers may execute QEMU/KUnit runtime hooks",
    )
    p.add_argument("--skip-system-test-generation", action="store_true")
    p.add_argument("--cleanup-worktrees", action="store_true")
    p.add_argument("--methods", default="B0,B1,D", help="comma-separated subset of B0,B1,D")

    oot = p.add_argument_group("OOT")
    oot.add_argument("--oot-repo-url", default="")
    oot.add_argument("--oot-repo-ref", default="")
    oot.add_argument("--oot-source-subdir", default="")
    oot.add_argument("--oot-module-ko", default="lddbus.ko")
    oot.add_argument("--oot-module-name", default="lddbus")
    oot.add_argument("--oot-device-node", default="/dev/none")
    oot.add_argument("--oot-qemu-write-data", default="qemu-ok")
    oot.add_argument("--oot-qemu-expect-data", default="qemu-ok")
    oot.add_argument("--oot-qemu-test-mode", choices=["auto", "custom", "llm", "default"], default="auto")
    oot.add_argument("--oot-qemu-test-level", choices=["load_unload", "interface_smoke", "semantic_scenario"], default="interface_smoke")
    oot.add_argument("--oot-make-extra-arg", action="append", default=[])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.case_kind == "oot":
        missing = [k for k in ("oot_repo_url", "oot_repo_ref", "oot_source_subdir") if not getattr(args, k)]
        if missing:
            raise SystemExit(f"OOT case requires: {', '.join('--' + x.replace('_', '-') for x in missing)}")

    base = Path(args.out_dir).resolve()
    run_dir = base / f"{slugify(args.module_path)}__{args.old_ref}__{args.new_ref}" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "experiment_args.json", vars(args))
    write_reference_diff(args, run_dir)

    p0 = run_oot_p0(args, run_dir) if args.case_kind == "oot" else run_intree_p0(args, run_dir)
    mode_map = {
        "B0": "baseline_source_only",
        "B1": "baseline_kernel_evidence",
        "D": "agent",
    }
    layers: list[dict[str, Any]] = []
    for layer in [x.strip() for x in args.methods.split(",") if x.strip()]:
        if layer not in mode_map:
            raise SystemExit(f"unknown method layer {layer}; expected one of B0,B1,D")
        print(f"[four-layer] running {layer} ({mode_map[layer]})")
        layers.append(run_method_layer(args, run_dir, layer, mode_map[layer]))
        write_summary(run_dir, args, p0, layers)

    write_summary(run_dir, args, p0, layers)
    print(json.dumps({"run_dir": run_dir.as_posix(), "layers": [x["layer"] for x in layers]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
