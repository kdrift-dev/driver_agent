from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .llm_client import LLMClient
from .prompts import build_system_test_generation_prompt
from .reasoner_agent import run_post_reasoner_agent
from .repo_utils import RepositoryAccessor
from .utils import read_text, write_json, write_text


def _run_cmd(cmd: list[str], *, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> dict[str, Any]:
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cmd": " ".join(shlex.quote(x) for x in cmd),
            "duration_sec": round(time.time() - t0, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\n[timeout] after {timeout} sec",
            "cmd": " ".join(shlex.quote(x) for x in cmd),
            "duration_sec": round(time.time() - t0, 3),
        }


def _truncate(text: str, max_chars: int = 24000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def _extract_touched_files_from_patch(patch_text: str) -> list[str]:
    out: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[len("+++ b/") :].strip()
        if rel == "/dev/null":
            continue
        if rel and rel not in out:
            out.append(rel)
    return out


def _prepare_git_worktree(repo: Path, ref: str, target: Path) -> dict[str, Any]:
    if target.exists():
        _run_cmd(["git", "-C", str(repo), "worktree", "remove", "-f", str(target)], timeout=600)
        shutil.rmtree(target, ignore_errors=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    add = _run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), ref], timeout=7200)
    return add


def _find_makefile_dirs(kernel_tree: Path, paths: Iterable[str], fallback_module_path: str) -> list[str]:
    dirs: list[str] = []

    def add_dir(d: str) -> None:
        if d and d not in dirs:
            dirs.append(d)

    for rel in paths:
        cur = Path(rel).parent
        while True:
            candidate = kernel_tree / cur
            if (candidate / "Makefile").exists() or (candidate / "Kbuild").exists():
                add_dir(cur.as_posix() or ".")
                break
            if cur.as_posix() in ("", "."):
                break
            cur = cur.parent

    if not dirs:
        add_dir(fallback_module_path)
    return dirs


def _extract_slice(text: str, start_line: int, end_line: int, pad: int = 20) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, start_line - pad)
    end = min(len(lines), end_line + pad)
    out = []
    for n in range(start, end + 1):
        out.append(f"{n:5d}: {lines[n - 1]}")
    return "\n".join(out)


def _collect_patched_slices(new_tree: Path, impact_graph: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for afn in impact_graph.get("affected_functions", []):
        rel = afn.get("file")
        if not rel:
            continue
        p = new_tree / rel
        if not p.exists():
            continue
        txt = read_text(p)
        start = int(afn.get("start_line", 1) or 1)
        end = int(afn.get("end_line", start) or start)
        out.append(
            {
                "function": afn.get("function"),
                "file": rel,
                "slice": _extract_slice(txt, start, end),
            }
        )
    return out


def _generate_system_tests(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    case_id: str,
    module_path: str,
    context_packet: dict[str, Any],
    impact_graph: dict[str, Any],
    compile_result: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "05_system_test_generation"
    stage_dir.mkdir(parents=True, exist_ok=True)

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "context_packet": {
            "kernel_change_summary": context_packet.get("kernel_change_summary", ""),
            "compatibility_obligations": context_packet.get("compatibility_obligations", []),
            "patch_generation_guidance": context_packet.get("patch_generation_guidance", []),
        },
        "impact_graph": impact_graph,
        "compile_result": {
            "success": compile_result.get("success", False),
            "compiled_dirs": compile_result.get("compiled_dirs", []),
            "error": compile_result.get("error", ""),
        },
        "instruction": "Generate multiple system entrypoint-level test cases and runnable suite commands.",
    }

    prompt = build_system_test_generation_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "system_test_generation",
        "test_cases": [],
        "suite_commands": [],
        "notes": ["dry-run or no-provider placeholder"],
    }

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )


def _write_test_bundle(out_dir: Path, test_gen: dict[str, Any]) -> dict[str, Any]:
    gen_dir = out_dir / "generated_tests"
    gen_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "test_cases": test_gen.get("test_cases", []),
        "suite_commands": test_gen.get("suite_commands", []),
        "notes": test_gen.get("notes", []),
    }
    write_json(gen_dir / "test_manifest.json", manifest)

    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for case in manifest["test_cases"]:
        cid = case.get("id", "UNKNOWN")
        name = case.get("name", "unnamed")
        lines.append(f"echo '[TEST] {cid} {name}'")
        timeout_sec = int(case.get("timeout_sec", 120) or 120)
        for step in case.get("steps", []):
            cmd = step.get("cmd", "").strip()
            if not cmd:
                continue
            esc = cmd.replace("'", "'\"'\"'")
            lines.append(f"timeout {timeout_sec} bash -lc '{esc}'")
        lines.append("")

    for cmd in manifest["suite_commands"]:
        cmd = (cmd or "").strip()
        if not cmd:
            continue
        esc = cmd.replace("'", "'\"'\"'")
        lines.append(f"bash -lc '{esc}'")

    script = "\n".join(lines) + "\n"
    script_path = gen_dir / "run_generated_tests.sh"
    write_text(script_path, script)
    script_path.chmod(0o755)

    return {
        "manifest_path": (gen_dir / "test_manifest.json").as_posix(),
        "runner_script_path": script_path.as_posix(),
        "test_case_count": len(manifest["test_cases"]),
        "suite_command_count": len(manifest["suite_commands"]),
    }


def _safe_command_for_exec(cmd: str) -> bool:
    deny_substrings = (
        "rm -rf /",
        "mkfs.",
        "shutdown -h",
        "poweroff",
        "reboot",
    )
    stripped = cmd.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    for bad in deny_substrings:
        if bad in lower:
            return False
    return True


def _looks_like_script_lines(suite_commands: list[str]) -> bool:
    if not suite_commands:
        return False
    non_empty = [line for line in suite_commands if (line or "").strip()]
    if not non_empty:
        return False
    first = non_empty[0].strip()
    if first.startswith("#!"):
        return True
    if any("set -euo pipefail" in line for line in non_empty[:5]):
        return True
    # Heuristic: many line-based commands with comments/assignments usually means script body.
    if len(non_empty) >= 20 and any(line.strip().startswith("#") for line in non_empty[:10]):
        return True
    return False


def _run_suite_commands(new_tree: Path, suite_commands: list[str], timeout_sec: int = 3600) -> dict[str, Any]:
    runs = []
    any_fail = False

    if _looks_like_script_lines(suite_commands):
        script_path = new_tree / ".driver_agent_suite.sh"
        script_text = "\n".join(suite_commands) + "\n"
        script_path.write_text(script_text, encoding="utf-8")
        script_path.chmod(0o755)
        res = _run_cmd(["bash", str(script_path)], cwd=new_tree, timeout=timeout_sec)
        res["stdout"] = _truncate(res.get("stdout", ""))
        res["stderr"] = _truncate(res.get("stderr", ""))
        res["script_mode"] = True
        runs.append(res)
        any_fail = not res.get("ok")
        return {"success": not any_fail, "runs": runs}

    for raw in suite_commands:
        cmd = (raw or "").strip()
        if not cmd:
            continue
        if cmd.startswith("#"):
            continue
        if not _safe_command_for_exec(cmd):
            runs.append({"cmd": cmd, "ok": False, "skipped": True, "reason": "command_not_whitelisted"})
            any_fail = True
            continue
        res = _run_cmd(["bash", "-lc", cmd], cwd=new_tree, timeout=timeout_sec)
        res["stdout"] = _truncate(res.get("stdout", ""))
        res["stderr"] = _truncate(res.get("stderr", ""))
        runs.append(res)
        if not res.get("ok"):
            any_fail = True
    return {"success": not any_fail, "runs": runs}


def run_validation_agent(
    *,
    accessor: RepositoryAccessor,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    run_dir: Path,
    case_id: str,
    module_path: str,
    old_ref: Optional[str],
    new_ref: Optional[str],
    patch_generation_result: dict[str, Any],
    context_packet: dict[str, Any],
    impact_graph: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    dry_run: bool,
    jobs: int,
    enable_qemu: bool,
    qemu_timeout_sec: int,
    keep_worktrees: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "case_id": case_id,
        "stage": "patch_validation",
        "status": "unknown",
        "apply": {},
        "stage_patched_files": {},
        "compile": {},
        "test_generation": {},
        "test_bundle": {},
        "suite_execution": {},
        "qemu": {},
        "post_reasoning": {},
        "open_questions": [],
    }

    validation_dir = run_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)

    candidates = patch_generation_result.get("patch_candidates", [])
    if not candidates:
        out["status"] = "skipped"
        out["open_questions"].append("no_patch_candidate_available")
        write_json(run_dir / "validation.json", out)
        return out

    selected_id = patch_generation_result.get("selected_candidate_id") or candidates[0].get("candidate_id")
    selected = None
    for c in candidates:
        if c.get("candidate_id") == selected_id:
            selected = c
            break
    if not selected:
        selected = candidates[0]

    fixed_patch_rel = selected.get("fixed_patch_path")
    if not fixed_patch_rel:
        out["status"] = "skipped"
        out["open_questions"].append("selected_candidate_missing_fixed_patch_path")
        write_json(run_dir / "validation.json", out)
        return out

    fixed_patch_path = run_dir / fixed_patch_rel
    if not fixed_patch_path.exists():
        out["status"] = "skipped"
        out["open_questions"].append(f"fixed_patch_missing:{fixed_patch_rel}")
        write_json(run_dir / "validation.json", out)
        return out

    if accessor.mode != "git" or not accessor.repo or not old_ref or not new_ref:
        out["status"] = "skipped"
        out["open_questions"].append("validation_requires_git_mode_with_old_ref_and_new_ref")
        write_json(run_dir / "validation.json", out)
        return out

    worktrees_dir = (run_dir / "worktrees").resolve()
    old_tree = (worktrees_dir / "old").resolve()
    new_tree = (worktrees_dir / "new").resolve()

    wt_old = _prepare_git_worktree(accessor.repo, old_ref, old_tree)
    wt_new = _prepare_git_worktree(accessor.repo, new_ref, new_tree)
    out["worktrees"] = {
        "old": old_tree.as_posix(),
        "new": new_tree.as_posix(),
        "old_prepare": {**wt_old, "stdout": _truncate(wt_old.get("stdout", "")), "stderr": _truncate(wt_old.get("stderr", ""))},
        "new_prepare": {**wt_new, "stdout": _truncate(wt_new.get("stdout", "")), "stderr": _truncate(wt_new.get("stderr", ""))},
    }

    if not wt_old.get("ok") or not wt_new.get("ok"):
        out["status"] = "failed"
        out["open_questions"].append("failed_to_prepare_worktrees")
        write_json(run_dir / "validation.json", out)
        return out

    patch_text = read_text(fixed_patch_path)
    touched_files = selected.get("touched_files") or _extract_touched_files_from_patch(patch_text)

    apply_check = _run_cmd(["git", "apply", "--check", str(fixed_patch_path)], cwd=old_tree, timeout=300)
    apply_res = None
    already_applied = False
    if apply_check.get("ok"):
        apply_res = _run_cmd(["git", "apply", str(fixed_patch_path)], cwd=old_tree, timeout=300)
    else:
        rev_check = _run_cmd(["git", "apply", "-R", "--check", str(fixed_patch_path)], cwd=old_tree, timeout=300)
        if rev_check.get("ok"):
            already_applied = True
            apply_res = {"ok": True, "returncode": 0, "stdout": "", "stderr": "already_applied", "cmd": "git apply", "duration_sec": 0.0}
        else:
            apply_res = apply_check

    out["apply"] = {
        "patch_path": fixed_patch_path.as_posix(),
        "touched_files": touched_files,
        "already_applied": already_applied,
        "result": {
            **apply_res,
            "stdout": _truncate(apply_res.get("stdout", "") if apply_res else ""),
            "stderr": _truncate(apply_res.get("stderr", "") if apply_res else ""),
        },
    }

    if not apply_res or not apply_res.get("ok"):
        out["status"] = "failed"
        out["open_questions"].append("patch_apply_failed_on_old_ref")
        write_json(run_dir / "validation.json", out)
        if not keep_worktrees:
            _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(old_tree)], timeout=300)
            _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(new_tree)], timeout=300)
        return out

    copied = []
    missing = []
    for rel in touched_files:
        src = old_tree / rel
        dst = new_tree / rel
        if not src.exists():
            missing.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    out["stage_patched_files"] = {
        "copied_files": copied,
        "missing_files": missing,
        "count": len(copied),
    }

    prep_steps: list[dict[str, Any]] = []
    prep_steps.append(_run_cmd(["make", "defconfig"], cwd=new_tree, timeout=1800))
    prep_steps.append(_run_cmd(["make", "olddefconfig"], cwd=new_tree, timeout=1800))
    prep_steps.append(_run_cmd(["make", "modules_prepare"], cwd=new_tree, timeout=2400))

    prep_ok = all(step.get("ok") for step in prep_steps)
    for step in prep_steps:
        step["stdout"] = _truncate(step.get("stdout", ""))
        step["stderr"] = _truncate(step.get("stderr", ""))

    compile_result: dict[str, Any] = {
        "success": False,
        "prepare_steps": prep_steps,
        "compiled_dirs": [],
        "runs": [],
        "error": "",
    }

    if prep_ok:
        compile_dirs = _find_makefile_dirs(new_tree, copied or touched_files, module_path)
        compile_result["compiled_dirs"] = compile_dirs
        all_ok = True
        for d in compile_dirs:
            cmd = ["make", f"-j{max(1, jobs)}", f"M={d}", "modules"]
            run = _run_cmd(cmd, cwd=new_tree, timeout=3600)
            run["stdout"] = _truncate(run.get("stdout", ""))
            run["stderr"] = _truncate(run.get("stderr", ""))
            compile_result["runs"].append(run)
            if not run.get("ok"):
                all_ok = False
        compile_result["success"] = all_ok
        if not all_ok:
            compile_result["error"] = "module_compile_failed"
    else:
        compile_result["error"] = "kernel_prepare_failed"

    out["compile"] = compile_result

    test_gen = _generate_system_tests(
        llm_client=llm_client,
        llm_calls_dir=llm_calls_dir,
        case_id=case_id,
        module_path=module_path,
        context_packet=context_packet,
        impact_graph=impact_graph,
        compile_result=compile_result,
        dry_run=dry_run,
    )
    out["test_generation"] = test_gen

    test_bundle = _write_test_bundle(run_dir, test_gen)
    out["test_bundle"] = test_bundle

    suite_exec = _run_suite_commands(new_tree, test_gen.get("suite_commands", []), timeout_sec=qemu_timeout_sec)
    out["suite_execution"] = suite_exec

    qemu_runs = []
    suite_cmds = test_gen.get("suite_commands", [])
    qemu_hint = any(("kunit.py run" in (line or "") or "qemu-system-" in (line or "")) for line in suite_cmds)
    if enable_qemu:
        for item in suite_exec.get("runs", []):
            cmd = item.get("cmd", "")
            if "kunit.py run" in cmd or "qemu-system-" in cmd:
                qemu_runs.append(item)
            elif item.get("script_mode") and qemu_hint:
                qemu_runs.append(item)
        if not qemu_runs:
            fallback_cfg = run_dir / "generated_tests" / "fallback.kunitconfig"
            fallback_cfg.parent.mkdir(parents=True, exist_ok=True)
            fallback_cfg.write_text(
                "\n".join(
                    [
                        "CONFIG_KUNIT=y",
                        "CONFIG_KUNIT_TEST=y",
                        "CONFIG_DEV_DAX=y",
                        "CONFIG_DEV_DAX_HMEM=y",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            # KUnit requires a clean source tree for out-of-tree builds.
            clean_run = _run_cmd(["make", "ARCH=x86_64", "mrproper"], cwd=new_tree, timeout=2400)
            clean_run["stdout"] = _truncate(clean_run.get("stdout", ""))
            clean_run["stderr"] = _truncate(clean_run.get("stderr", ""))
            timeout = min(max(120, qemu_timeout_sec), 1200)
            fallback_cmd = (
                "python3 tools/testing/kunit/kunit.py run "
                f"--arch x86_64 --kunitconfig {fallback_cfg.as_posix()} "
                "--summary "
                f"--timeout {timeout}"
            )
            fallback_run = _run_cmd(["bash", "-lc", fallback_cmd], cwd=new_tree, timeout=timeout + 900)
            fallback_run["stdout"] = _truncate(fallback_run.get("stdout", ""))
            fallback_run["stderr"] = _truncate(fallback_run.get("stderr", ""))
            fallback_run["fallback_qemu"] = True
            fallback_run["pre_clean"] = clean_run
            qemu_runs.append(fallback_run)
    out["qemu"] = {
        "enabled": enable_qemu,
        "detected_qemu_runs": len(qemu_runs),
        "runs": qemu_runs,
        "success": all(r.get("ok") for r in qemu_runs) if qemu_runs else False,
        "note": "qemu stage uses suite_commands that invoke kunit.py run or qemu-system-*",
    }

    patched_slices = _collect_patched_slices(new_tree, impact_graph)
    post_reasoning = run_post_reasoner_agent(
        llm_client=llm_client,
        llm_calls_dir=llm_calls_dir,
        case_id=case_id,
        module_path=module_path,
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
        patched_function_slices=patched_slices,
        compile_result=compile_result,
        test_generation_result=test_gen,
        dry_run=dry_run,
    )
    out["post_reasoning"] = post_reasoning

    if compile_result.get("success"):
        out["status"] = "ok"
    else:
        out["status"] = "failed"
        out["open_questions"].append("compile_not_success")

    write_json(run_dir / "validation.json", out)

    if not keep_worktrees:
        _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(old_tree)], timeout=300)
        _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(new_tree)], timeout=300)

    return out
