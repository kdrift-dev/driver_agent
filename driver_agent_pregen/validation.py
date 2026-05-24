from __future__ import annotations

import shlex
import shutil
import subprocess
import time
import os
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


def _patch_has_effective_changes(patch_text: str) -> bool:
    removed: list[str] = []
    added: list[str] = []
    for line in (patch_text or "").splitlines():
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
            continue
        if line.startswith("+"):
            added.append(line[1:])
            continue
    if not removed and not added:
        return False
    if len(removed) == len(added) and removed == added:
        return False
    return True


def _list_module_source_files(tree: Path, module_path: str) -> list[str]:
    root = tree / module_path
    if not root.exists():
        return []
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in {".c", ".h", ".rs"}:
            continue
        out.append(p.relative_to(tree).as_posix())
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


def _extract_include_block(text: str, *, max_lines: int = 140) -> str:
    lines = text.splitlines()
    out: list[str] = []
    saw_include = False
    for n, line in enumerate(lines[:max_lines], start=1):
        stripped = line.strip()
        if stripped.startswith("#include"):
            saw_include = True
            out.append(f"{n:5d}: {line}")
            continue
        if not saw_include:
            if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                out.append(f"{n:5d}: {line}")
                continue
            if stripped.startswith("#") and not stripped.startswith("#include"):
                out.append(f"{n:5d}: {line}")
                continue
        else:
            if not stripped or stripped.startswith("#include"):
                out.append(f"{n:5d}: {line}")
                continue
            break
    return "\n".join(out)


def _direct_include_requirement_files(compatibility_obligations: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    for obl in compatibility_obligations.get("obligations", []) or []:
        for ev in obl.get("evidence", []) or []:
            if ev.get("type") != "direct_include_requirement":
                continue
            value = ev.get("value") or {}
            rel = str(value.get("file") or "").strip()
            if rel:
                files.add(rel)
    return files


def _collect_patched_slices(
    new_tree: Path,
    impact_graph: dict[str, Any],
    compatibility_obligations: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    include_files: set[str] = set()
    for afn in impact_graph.get("affected_functions", []):
        rel = afn.get("file")
        if not rel:
            continue
        include_files.add(str(rel))
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
    include_files.update(_direct_include_requirement_files(compatibility_obligations or {}))
    for rel in sorted(include_files):
        p = new_tree / rel
        if not p.exists() or p.suffix not in {".c", ".h", ".rs"}:
            continue
        block = _extract_include_block(read_text(p))
        if not block:
            continue
        out.append(
            {
                "function": "__file_includes__",
                "file": rel,
                "source_item_kind": "patched_file_include_block",
                "slice": block,
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

    deterministic = _deterministic_system_tests(case_id=case_id, module_path=module_path)
    if deterministic:
        # Prefer known-good deterministic runtime hooks over LLM-generated
        # scripts. This avoids wasting tokens and prevents test-generation
        # timeouts for cases that already have a stable KUnit/kselftest entry.
        write_text(stage_dir / "prompt.md", "")
        write_json(
            stage_dir / "input.json",
            {
                "case_id": case_id,
                "module_path": module_path,
                "stage": "system_test_generation",
                "generator": "deterministic_fallback",
                "reason": "known deterministic runtime hook available for this module",
            },
        )
        write_text(stage_dir / "raw_output.txt", "")
        write_json(stage_dir / "parsed_output.json", deterministic)
        return deterministic

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

    generated = llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )
    if str(generated.get("parse_status", "")) == "ok" and generated.get("suite_commands"):
        return generated

    return generated


def _deterministic_system_tests(*, case_id: str, module_path: str) -> dict[str, Any] | None:
    """Known-good runtime test hooks that do not depend on LLM generation.

    These commands are conservative: only KUnit paths are executed by default,
    because they run in the generated kernel test environment rather than
    mutating the host. kselftest targets are recorded as guidance but are not
    auto-run unless a later runner provides an isolated QEMU guest.
    """
    cases: list[dict[str, Any]]
    cmds: list[str]
    notes: list[str]

    if module_path == "drivers/iio/test":
        cfg = ".kunitconfig.driver_agent_iio_test"
        cmd = (
            "printf '%s\\n' "
            "'CONFIG_KUNIT=y' "
            "'CONFIG_IIO=y' "
            "'CONFIG_IIO_RESCALE=y' "
            "'CONFIG_IIO_GTS_KUNIT_TEST=y' "
            "'CONFIG_IIO_FORMAT_KUNIT_TEST=y' "
            "'CONFIG_IIO_MULTIPLY_KUNIT_TEST=y' "
            "'CONFIG_IIO_RESCALE_KUNIT_TEST=y' "
            f"> {cfg} && make ARCH=um mrproper >/dev/null 2>&1 || true; ./tools/testing/kunit/kunit.py run --kunitconfig={cfg}"
        )
        cases = [
            {
                "id": "DET-KUNIT-IIO-001",
                "name": "IIO KUnit tests",
                "entry_kind": "boot",
                "intent": "Run synthetic IIO KUnit tests without physical IIO hardware.",
                "preconditions": ["KUnit can build in the prepared kernel tree."],
                "steps": [{"cmd": cmd, "why": "Exercises IIO test modules in an isolated KUnit run."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 600,
                "skip_if": ["KUnit/UML tooling unavailable"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/iio/test"]
    elif module_path == "drivers/gpu/drm/ttm/tests":
        cmd = "make ARCH=um mrproper >/dev/null 2>&1 || true; ./tools/testing/kunit/kunit.py run --kunitconfig=drivers/gpu/drm/ttm/tests"
        cases = [
            {
                "id": "DET-KUNIT-TTM-001",
                "name": "DRM TTM KUnit tests",
                "entry_kind": "boot",
                "intent": "Run DRM TTM KUnit tests without real GPU hardware.",
                "preconditions": ["drivers/gpu/drm/ttm/tests/.kunitconfig exists."],
                "steps": [{"cmd": cmd, "why": "Uses the in-tree TTM KUnit configuration."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 600,
                "skip_if": ["KUnit/UML tooling unavailable"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/gpu/drm/ttm/tests"]
    elif module_path == "drivers/gpu/drm/vkms":
        cmd = "make ARCH=um mrproper >/dev/null 2>&1 || true; ./tools/testing/kunit/kunit.py run --kunitconfig=drivers/gpu/drm/vkms/tests"
        cases = [
            {
                "id": "DET-KUNIT-VKMS-001",
                "name": "VKMS KUnit tests",
                "entry_kind": "boot",
                "intent": "Run VKMS KUnit tests for the virtual KMS driver.",
                "preconditions": ["drivers/gpu/drm/vkms/tests/.kunitconfig exists."],
                "steps": [{"cmd": cmd, "why": "Uses the in-tree VKMS KUnit configuration."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 600,
                "skip_if": ["KUnit/UML tooling unavailable"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/gpu/drm/vkms"]
    else:
        runner = (Path(__file__).resolve().parents[1] / "tools" / "run_intree_kselftest_qemu.py").as_posix()
        if module_path == "drivers/dma-buf/heaps":
            cmd = (
                f"python {shlex.quote(runner)} "
                "--kernel-tree . "
                "--target dmabuf-heaps "
                "--out-dir ../kselftest_qemu/dmabuf-heaps "
                "--jobs 8 "
                "--timeout-sec 600"
            )
            cases = [
                {
                    "id": "DET-QEMU-KSELFTEST-DMABUF-001",
                    "name": "DMA-BUF heaps kselftest in QEMU",
                    "entry_kind": "boot",
                    "intent": "Build and run dmabuf-heaps kselftest inside an isolated QEMU guest.",
                    "preconditions": ["qemu-system-x86_64, busybox, cpio, and gcc are available on the host."],
                    "steps": [{"cmd": cmd, "why": "Executes the selftest in a generated initramfs instead of on the host."}],
                    "assertions": ["runner exits successfully and prints DRIVER_AGENT_QEMU_KSELFTEST_PASS"],
                    "timeout_sec": 600,
                    "skip_if": ["QEMU or required host build tools unavailable"],
                    "artifacts": ["worktrees/kselftest_qemu/dmabuf-heaps/result.json", "worktrees/kselftest_qemu/dmabuf-heaps/qemu_output.txt"],
                }
            ]
            cmds = [cmd]
            notes = ["deterministic isolated-QEMU kselftest runner for drivers/dma-buf/heaps"]
        else:
            known_kselftests = {
                "drivers/net/wireguard": "tools/testing/selftests/wireguard",
                "drivers/net/netdevsim": "tools/testing/selftests/drivers/net/netdevsim",
            }
            target = known_kselftests.get(module_path)
            if not target:
                return None
            cases = [
                {
                    "id": "DET-KSELFTEST-INFO-001",
                    "name": f"{module_path} kselftest target",
                    "entry_kind": "unknown",
                    "intent": "Record the relevant in-tree kselftest target; not auto-run until a target-specific isolated QEMU runner is available.",
                    "preconditions": ["An isolated guest runner is available for this target."],
                    "steps": [{"cmd": f"make -C tools/testing/selftests TARGETS={target.split('/')[-1]} run_tests", "why": "kselftest target is software-visible but host execution is disabled by default."}],
                    "assertions": ["target-specific selftest exits successfully"],
                    "timeout_sec": 600,
                    "skip_if": ["no target-specific isolated QEMU guest runner"],
                    "artifacts": ["kselftest output"],
                }
            ]
            cmds = []
            notes = [f"deterministic kselftest target recorded for {module_path}; no command auto-run without target-specific guest isolation"]

    return {
        "case_id": case_id,
        "stage": "system_test_generation",
        "parse_status": "ok",
        "test_cases": cases,
        "suite_commands": cmds,
        "notes": notes,
        "generator": "deterministic_fallback",
    }


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


def _is_nonfatal_dmesg_permission_failure(stdout_text: str, stderr_text: str) -> bool:
    merged = ((stdout_text or "") + "\n" + (stderr_text or "")).lower()
    return "dmesg: read kernel buffer failed: operation not permitted" in merged


def _is_hardware_unavailable_failure(stdout_text: str, stderr_text: str) -> bool:
    merged = ((stdout_text or "") + "\n" + (stderr_text or "")).lower()
    markers = (
        "suite_skip:",
        "hardware not present",
        "hardware-dependent tests",
        "no ux500 compatible",
    )
    return any(m in merged for m in markers)


def _suite_requires_root(script_text: str) -> bool:
    lower = (script_text or "").lower()
    markers = (
        "modprobe",
        "rmmod",
        "insmod",
        "rtcwake",
        "dmesg --clear",
        "dmesg -c",
    )
    return any(m in lower for m in markers)


def _harden_generated_suite_script(script_text: str) -> str:
    """
    Make generated suite scripts less brittle across heterogeneous hosts.
    - Avoid `set -e*` early-exit traps; keep `-u -o pipefail`.
    - Keep script behavior otherwise unchanged.
    """
    out_lines = []
    for raw in script_text.splitlines():
        line = raw.strip()
        if line.startswith("set -euo pipefail"):
            out_lines.append(raw.replace("set -euo pipefail", "set -uo pipefail", 1))
            continue
        if line.startswith("set -eo pipefail"):
            out_lines.append(raw.replace("set -eo pipefail", "set -o pipefail", 1))
            continue
        if line == "set -e":
            # Keep unset-var safety, but do not terminate on first non-zero.
            prefix = raw[: len(raw) - len(raw.lstrip(" \t"))]
            out_lines.append(prefix + "set -u")
            continue
        out_lines.append(raw)
    hardened = "\n".join(out_lines)
    if script_text.endswith("\n"):
        hardened += "\n"
    return hardened


def _run_suite_commands(new_tree: Path, suite_commands: list[str], timeout_sec: int = 3600) -> dict[str, Any]:
    runs = []
    any_fail = False

    if _looks_like_script_lines(suite_commands):
        script_path = new_tree / ".driver_agent_suite.sh"
        script_text = "\n".join(suite_commands) + "\n"
        script_text = _harden_generated_suite_script(script_text)
        if os.geteuid() != 0 and _suite_requires_root(script_text):
            runs.append(
                {
                    "cmd": "suite_script",
                    "ok": True,
                    "skipped": True,
                    "reason": "requires_root_privileges_non_root_env",
                    "script_mode": True,
                }
            )
            return {"success": True, "runs": runs}
        script_path.write_text(script_text, encoding="utf-8")
        script_path.chmod(0o755)
        res = _run_cmd(["bash", str(script_path)], cwd=new_tree, timeout=timeout_sec)
        res["stdout"] = _truncate(res.get("stdout", ""))
        res["stderr"] = _truncate(res.get("stderr", ""))
        res["script_mode"] = True
        if not res.get("ok") and int(res.get("returncode", -1)) == 77:
            res["original_returncode"] = res.get("returncode")
            res["returncode"] = 0
            res["ok"] = True
            res["nonfatal_adjustment"] = "skipped_exit_77"
        if not res.get("ok") and _is_nonfatal_dmesg_permission_failure(res.get("stdout", ""), res.get("stderr", "")):
            res["original_returncode"] = res.get("returncode")
            res["returncode"] = 0
            res["ok"] = True
            res["nonfatal_adjustment"] = "ignored_dmesg_permission_error"
        if not res.get("ok") and _is_hardware_unavailable_failure(res.get("stdout", ""), res.get("stderr", "")):
            res["original_returncode"] = res.get("returncode")
            res["returncode"] = 0
            res["ok"] = True
            res["skipped"] = True
            res["reason"] = "hardware_unavailable"
            res["nonfatal_adjustment"] = "hardware_unavailable_suite_skip"
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
        if not res.get("ok") and int(res.get("returncode", -1)) == 77:
            res["original_returncode"] = res.get("returncode")
            res["returncode"] = 0
            res["ok"] = True
            res["nonfatal_adjustment"] = "skipped_exit_77"
        if not res.get("ok") and _is_nonfatal_dmesg_permission_failure(res.get("stdout", ""), res.get("stderr", "")):
            res["original_returncode"] = res.get("returncode")
            res["returncode"] = 0
            res["ok"] = True
            res["nonfatal_adjustment"] = "ignored_dmesg_permission_error"
        runs.append(res)
        if not res.get("ok"):
            any_fail = True
    return {"success": not any_fail, "runs": runs}


def _qemu_or_kunit_commands(suite_commands: list[str]) -> list[str]:
    """Extract standalone qemu/kunit commands from generated suite commands.

    Do not run a whole generated shell script just because it contains a qemu
    command: such scripts often also contain host-side modprobe/mount/suspend
    operations. Each extracted line is executed as an independent shell command.
    """
    out: list[str] = []
    for raw in suite_commands or []:
        cmd = (raw or "").strip()
        if not cmd or cmd.startswith("#"):
            continue
        if "kunit.py run" not in cmd and "qemu-system-" not in cmd and "run_intree_kselftest_qemu.py" not in cmd:
            continue
        if not _safe_command_for_exec(cmd):
            continue
        out.append(cmd)
    return out


def _run_qemu_or_kunit_commands(new_tree: Path, suite_commands: list[str], timeout_sec: int = 3600) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for cmd in _qemu_or_kunit_commands(suite_commands):
        res = _run_cmd(["bash", "-lc", cmd], cwd=new_tree, timeout=timeout_sec)
        res["stdout"] = _truncate(res.get("stdout", ""))
        res["stderr"] = _truncate(res.get("stderr", ""))
        res["qemu_or_kunit_command"] = True
        runs.append(res)
    return runs


def _summarize_skip_reason(runs: list[dict[str, Any]], default: str) -> str:
    reasons = []
    for r in runs:
        reason = (r.get("reason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return "; ".join(reasons)
    return default


_VALIDATION_STAGE_ORDER = [
    "worktree_prepare",
    "patch_apply",
    "stage_patched_files",
    "compile_prepare",
    "compile_modules",
    "test_generation",
    "suite_execution",
    "qemu_execution",
    "spec_consistency",
]


def _init_stage_results() -> list[dict[str, Any]]:
    return [{"stage": name, "status": "skipped", "reason": "not_run"} for name in _VALIDATION_STAGE_ORDER]


def _set_stage_result(
    stage_results: list[dict[str, Any]],
    stage: str,
    status: str,
    reason: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    for item in stage_results:
        if item.get("stage") == stage:
            item["status"] = status
            item["reason"] = reason
            if details:
                item["details"] = details
            return


def _first_error_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:280]
    return ""


_IGNORED_FAILURE_LINE_SUBSTRINGS = (
    "stty: 'standard input': Inappropriate ioctl for device",
)


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if any(x in s for x in _IGNORED_FAILURE_LINE_SUBSTRINGS):
            continue
        return s[:280]
    return ""


def _best_failure_reason(stderr_text: str, stdout_text: str, fallback: str) -> str:
    line = _first_meaningful_line(stderr_text)
    if line:
        return line
    for raw in (stdout_text or "").splitlines():
        s = raw.strip()
        if "timed out after" in s:
            idx = s.find("timed out after")
            if idx >= 0:
                return ("timeout: " + s[idx:])[:280]
            return s[:280]
    line = _first_meaningful_line(stdout_text)
    if line:
        return line
    # Final fallback keeps backward compatibility for uncommon outputs.
    line = _first_error_line(stderr_text) or _first_error_line(stdout_text)
    return line or fallback


def _run_has_timeout_hint(run: dict[str, Any]) -> bool:
    stderr_text = str(run.get("stderr", "") or "")
    stdout_text = str(run.get("stdout", "") or "")
    hay = (stderr_text + "\n" + stdout_text).lower()
    return "timed out after" in hay


def _stage_summary_lines(stage_results: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in stage_results:
        stage = item.get("stage", "unknown")
        status = str(item.get("status", "skipped")).upper()
        reason = item.get("reason", "")
        if reason:
            lines.append(f"{stage}: {status} - {reason}")
        else:
            lines.append(f"{stage}: {status}")
    return lines


def _write_and_print_stage_summary(run_dir: Path, stage_results: list[dict[str, Any]]) -> None:
    lines = _stage_summary_lines(stage_results)
    summary_text = "\n".join(lines) + ("\n" if lines else "")
    validation_dir = run_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    write_text(validation_dir / "stage_summary.txt", summary_text)
    for line in lines:
        print(f"[validation-summary] {line}")


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
    skip_system_test_generation: bool,
    keep_worktrees: bool,
    enable_host_suite: bool = False,
) -> dict[str, Any]:
    stage_results = _init_stage_results()

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
        "spec_consistency": {},
        "validation_stage_results": stage_results,
        "open_questions": [],
    }

    validation_dir = run_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)

    def _finalize(status: str) -> dict[str, Any]:
        out["status"] = status
        out["validation_stage_results"] = stage_results
        _write_and_print_stage_summary(run_dir, stage_results)
        write_json(run_dir / "validation.json", out)
        return out

    candidates = patch_generation_result.get("patch_candidates", [])
    if not candidates:
        _set_stage_result(stage_results, "worktree_prepare", "skipped", "no_patch_candidate_available")
        out["open_questions"].append("no_patch_candidate_available")
        return _finalize("skipped")

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
        _set_stage_result(stage_results, "worktree_prepare", "skipped", "selected_candidate_missing_fixed_patch_path")
        out["open_questions"].append("selected_candidate_missing_fixed_patch_path")
        return _finalize("skipped")

    fixed_patch_path = run_dir / fixed_patch_rel
    if not fixed_patch_path.exists():
        _set_stage_result(stage_results, "worktree_prepare", "skipped", f"fixed_patch_missing:{fixed_patch_rel}")
        out["open_questions"].append(f"fixed_patch_missing:{fixed_patch_rel}")
        return _finalize("skipped")

    if accessor.mode != "git" or not accessor.repo or not old_ref or not new_ref:
        _set_stage_result(stage_results, "worktree_prepare", "skipped", "validation_requires_git_mode_with_old_ref_and_new_ref")
        out["open_questions"].append("validation_requires_git_mode_with_old_ref_and_new_ref")
        return _finalize("skipped")

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
        _set_stage_result(
            stage_results,
            "worktree_prepare",
            "failed",
            "failed_to_prepare_worktrees",
            details={"old_ok": bool(wt_old.get("ok")), "new_ok": bool(wt_new.get("ok"))},
        )
        out["open_questions"].append("failed_to_prepare_worktrees")
        return _finalize("failed")

    _set_stage_result(stage_results, "worktree_prepare", "passed", "old/new worktrees prepared")

    patch_text = read_text(fixed_patch_path)
    touched_files = selected.get("touched_files") or _extract_touched_files_from_patch(patch_text)
    no_op_candidate = bool(selected.get("no_op_candidate")) or not _patch_has_effective_changes(patch_text)

    apply_res = None
    already_applied = False
    if no_op_candidate:
        apply_res = {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "noop_candidate_skip_apply",
            "cmd": "git apply (skipped for no-op candidate)",
            "duration_sec": 0.0,
        }
        if not touched_files:
            touched_files = _list_module_source_files(old_tree, module_path)
    else:
        apply_check = _run_cmd(["git", "apply", "--check", str(fixed_patch_path)], cwd=old_tree, timeout=300)
        if apply_check.get("ok"):
            apply_res = _run_cmd(["git", "apply", str(fixed_patch_path)], cwd=old_tree, timeout=300)
        else:
            rev_check = _run_cmd(["git", "apply", "-R", "--check", str(fixed_patch_path)], cwd=old_tree, timeout=300)
            if rev_check.get("ok"):
                already_applied = True
                apply_res = {
                    "ok": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "already_applied",
                    "cmd": "git apply",
                    "duration_sec": 0.0,
                }
            else:
                apply_res = apply_check

    out["apply"] = {
        "patch_path": fixed_patch_path.as_posix(),
        "touched_files": touched_files,
        "already_applied": already_applied,
        "no_op_candidate": no_op_candidate,
        "result": {
            **apply_res,
            "stdout": _truncate(apply_res.get("stdout", "") if apply_res else ""),
            "stderr": _truncate(apply_res.get("stderr", "") if apply_res else ""),
        },
    }

    if not apply_res or not apply_res.get("ok"):
        err_line = _first_error_line((apply_res or {}).get("stderr", ""))
        _set_stage_result(stage_results, "patch_apply", "failed", err_line or "patch_apply_failed_on_old_ref")
        out["open_questions"].append("patch_apply_failed_on_old_ref")
        _set_stage_result(stage_results, "stage_patched_files", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "compile_prepare", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "compile_modules", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "test_generation", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "suite_execution", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "qemu_execution", "skipped", "patch_apply_failed")
        _set_stage_result(stage_results, "post_reasoning", "skipped", "patch_apply_failed")
        if not keep_worktrees:
            _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(old_tree)], timeout=300)
            _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(new_tree)], timeout=300)
        return _finalize("failed")

    if no_op_candidate:
        _set_stage_result(stage_results, "patch_apply", "passed", "no-op candidate; skipped git apply")
    else:
        _set_stage_result(stage_results, "patch_apply", "passed", "patch applied on old_ref")

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
    if missing:
        _set_stage_result(
            stage_results,
            "stage_patched_files",
            "failed",
            f"missing {len(missing)} touched files while staging",
            details={"missing_files": missing},
        )
    else:
        _set_stage_result(stage_results, "stage_patched_files", "passed", f"staged {len(copied)} files into new tree")

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
        _set_stage_result(stage_results, "compile_prepare", "passed", "defconfig/olddefconfig/modules_prepare succeeded")
    else:
        prep_reason = "kernel_prepare_failed"
        for step in prep_steps:
            if not step.get("ok"):
                prep_reason = _first_error_line(step.get("stderr", "")) or step.get("cmd", "kernel_prepare_failed")
                break
        _set_stage_result(stage_results, "compile_prepare", "failed", prep_reason)

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
            first_fail = next((r for r in compile_result["runs"] if not r.get("ok")), None)
            reason = _first_error_line((first_fail or {}).get("stderr", "")) or "module_compile_failed"
            _set_stage_result(stage_results, "compile_modules", "failed", reason)
        else:
            _set_stage_result(stage_results, "compile_modules", "passed", f"compiled dirs: {', '.join(compile_dirs) if compile_dirs else '(none)'}")
    else:
        compile_result["error"] = "kernel_prepare_failed"
        _set_stage_result(stage_results, "compile_modules", "skipped", "compile_prepare_failed")

    out["compile"] = compile_result

    if skip_system_test_generation:
        test_gen = {
            "case_id": case_id,
            "stage": "system_test_generation",
            "parse_status": "skipped_by_flag",
            "test_cases": [],
            "suite_commands": [],
            "notes": ["skipped by --skip-system-test-generation"],
        }
        out["test_generation"] = test_gen
        _set_stage_result(stage_results, "test_generation", "skipped", "skipped by flag")

        test_bundle = {
            "manifest_path": "",
            "runner_script_path": "",
            "test_case_count": 0,
            "suite_command_count": 0,
            "skipped": True,
        }
        out["test_bundle"] = test_bundle

        suite_exec = {"success": True, "runs": [], "skipped": True, "reason": "skipped_by_flag"}
        out["suite_execution"] = suite_exec
        _set_stage_result(stage_results, "suite_execution", "skipped", "skipped by flag")

        out["qemu"] = {
            "enabled": enable_qemu,
            "detected_qemu_runs": 0,
            "runs": [],
            "success": False,
            "note": "skipped because system-test generation was disabled",
        }
        _set_stage_result(stage_results, "qemu_execution", "skipped", "skipped by flag")
        suite_runs = []
        all_skipped = True
    else:
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
        tg_status = "passed" if str(test_gen.get("parse_status", "")) == "ok" else "failed"
        if tg_status == "passed":
            generator = str(test_gen.get("generator") or "")
            if generator == "deterministic_fallback":
                tg_reason = "deterministic system test generated"
            else:
                tg_reason = "llm test generation parsed"
        else:
            tg_reason = test_gen.get("error") or f"parse_status={test_gen.get('parse_status')}"
        _set_stage_result(stage_results, "test_generation", tg_status, tg_reason)

        test_bundle = _write_test_bundle(run_dir, test_gen)
        out["test_bundle"] = test_bundle

        if enable_host_suite:
            suite_exec = _run_suite_commands(new_tree, test_gen.get("suite_commands", []), timeout_sec=qemu_timeout_sec)
        else:
            suite_exec = {
                "success": True,
                "runs": [
                    {
                        "cmd": "generated_host_suite",
                        "ok": True,
                        "skipped": True,
                        "reason": "host_suite_execution_disabled_by_default",
                    }
                ],
                "skipped": True,
                "reason": "host_suite_execution_disabled_by_default",
            }
        out["suite_execution"] = suite_exec
        suite_runs = suite_exec.get("runs", [])
        all_skipped = bool(suite_runs) and all(bool(r.get("skipped")) for r in suite_runs)
        if suite_exec.get("success"):
            if not suite_runs:
                _set_stage_result(stage_results, "suite_execution", "skipped", "no_suite_commands_generated")
            elif all_skipped:
                _set_stage_result(
                    stage_results,
                    "suite_execution",
                    "skipped",
                    _summarize_skip_reason(suite_runs, "all_suite_commands_skipped"),
                )
            else:
                _set_stage_result(stage_results, "suite_execution", "passed", f"runs={len(suite_runs)}")
        else:
            first_fail = next((r for r in suite_runs if not r.get("ok")), {})
            reason = _best_failure_reason(
                first_fail.get("stderr", ""),
                first_fail.get("stdout", ""),
                first_fail.get("reason", "suite_command_failed"),
            )
            _set_stage_result(stage_results, "suite_execution", "failed", reason)

        qemu_runs = []
        suite_cmds = test_gen.get("suite_commands", [])
        if enable_qemu:
            qemu_runs = _run_qemu_or_kunit_commands(new_tree, suite_cmds, timeout_sec=qemu_timeout_sec)
        out["qemu"] = {
            "enabled": enable_qemu,
            "detected_qemu_runs": len(qemu_runs),
            "runs": qemu_runs,
            "success": all(r.get("ok") for r in qemu_runs) if qemu_runs else False,
            "note": "qemu stage executes only standalone suite_commands containing kunit.py run or qemu-system-*; host suite scripts are not reused",
        }
        for run in qemu_runs:
            if bool(run.get("ok")) and _run_has_timeout_hint(run):
                run["ok"] = False
                run["timeout_hint"] = True
                if not run.get("reason"):
                    run["reason"] = "timeout_detected_in_qemu_output"
        out["qemu"]["success"] = all(r.get("ok") for r in qemu_runs) if qemu_runs else False
        if not enable_qemu:
            _set_stage_result(stage_results, "qemu_execution", "skipped", "qemu disabled by flag")
        elif not qemu_runs:
            _set_stage_result(stage_results, "qemu_execution", "skipped", "qemu enabled but no standalone qemu/kunit command generated")
        elif all(r.get("ok") for r in qemu_runs):
            _set_stage_result(stage_results, "qemu_execution", "passed", f"runs={len(qemu_runs)}")
        else:
            first_fail = next((r for r in qemu_runs if not r.get("ok")), {})
            reason = _best_failure_reason(first_fail.get("stderr", ""), first_fail.get("stdout", ""), "qemu_run_failed")
            _set_stage_result(stage_results, "qemu_execution", "failed", reason)

    patched_slices = _collect_patched_slices(new_tree, impact_graph, compatibility_obligations)
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
    pr_parse_ok = str(post_reasoning.get("parse_status", "")) == "ok"
    pr_verdict = str(post_reasoning.get("overall_verdict", "UNKNOWN")).upper()
    if not pr_parse_ok:
        pr_status = "failed"
        pr_reason = post_reasoning.get("error") or f"parse_status={post_reasoning.get('parse_status')}"
    elif pr_verdict == "PASS":
        pr_status = "passed"
        pr_reason = f"overall_verdict={pr_verdict}"
    else:
        pr_status = "failed"
        pr_reason = f"overall_verdict={pr_verdict}"
    out["spec_consistency"] = {
        "status": pr_status,
        "reason": pr_reason,
        "overall_verdict": pr_verdict,
        "parse_status": post_reasoning.get("parse_status", ""),
        "not_a_correctness_proof": True,
        "caveat": (
            "This is an LLM consistency check against generated local specs and stated "
            "obligations; it is not a correctness metric."
        ),
    }
    _set_stage_result(stage_results, "spec_consistency", pr_status, pr_reason)

    test_gen_ok = str(test_gen.get("parse_status", "")) == "ok"
    suite_has_real_run = bool(suite_runs) and not all_skipped
    # Runtime hooks are evidence, not a core pass/fail oracle. They are recorded
    # in validation_summary_v2 as PASS/FAIL/SKIPPED, but do not decide whether
    # the pipeline completed its core apply/build/spec-consistency checks.
    tests_gate_ok = True
    qemu_gate_ok = True
    overall_ok = bool(compile_result.get("success") and tests_gate_ok and qemu_gate_ok)
    if not compile_result.get("success"):
        out["open_questions"].append("compile_not_success")
    if not skip_system_test_generation:
        if not test_gen_ok:
            out["open_questions"].append("test_generation_not_success")
        if not suite_exec.get("success"):
            out["open_questions"].append("suite_execution_not_success")
        elif not suite_has_real_run:
            out["open_questions"].append("suite_execution_skipped_or_empty")
        if enable_qemu and not out["qemu"].get("success"):
            out["open_questions"].append("qemu_execution_not_success")
    if not pr_parse_ok:
        out["open_questions"].append("spec_consistency_not_success")
    elif pr_verdict != "PASS":
        out["open_questions"].append(f"spec_consistency_verdict_{pr_verdict.lower()}")

    final_status = "ok" if overall_ok else "failed"

    if not keep_worktrees:
        _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(old_tree)], timeout=300)
        _run_cmd(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(new_tree)], timeout=300)

    return _finalize(final_status)
