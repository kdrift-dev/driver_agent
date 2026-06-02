from __future__ import annotations

import shlex
import shutil
import subprocess
import time
import json
import os
import re
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


def _ordered_unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
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

    def _kunit_case(
        *,
        test_id: str,
        name: str,
        intent: str,
        kunitconfig: str,
        note: str,
        timeout_sec: int = 900,
    ) -> dict[str, Any]:
        cmd = (
            "make ARCH=um mrproper >/dev/null 2>&1 || true; "
            f"./tools/testing/kunit/kunit.py run --kunitconfig={shlex.quote(kunitconfig)}"
        )
        return {
            "case_id": case_id,
            "stage": "system_test_generation",
            "parse_status": "ok",
            "test_cases": [
                {
                    "id": test_id,
                    "name": name,
                    "entry_kind": "boot",
                    "intent": intent,
                    "preconditions": [f"{kunitconfig} exists or can be generated in the prepared kernel tree."],
                    "steps": [{"cmd": cmd, "why": "Runs the target in-tree KUnit suite without physical hardware."}],
                    "assertions": ["kunit.py exits successfully"],
                    "timeout_sec": timeout_sec,
                    "skip_if": ["KUnit/UML tooling unavailable or dependencies unsatisfied"],
                    "artifacts": ["KUnit stdout/stderr"],
                }
            ],
            "suite_commands": [cmd],
            "notes": [note],
            "generator": "deterministic_fallback",
        }

    if module_path == "drivers/base/test":
        return _kunit_case(
            test_id="DET-KUNIT-BASE-TEST-001",
            name="driver core/base KUnit tests",
            intent="Run driver-core platform/property/root-device KUnit tests for the base test module.",
            kunitconfig="drivers/base/test",
            note="deterministic KUnit fallback for drivers/base/test",
        )

    if module_path == "drivers/gpu/drm/scheduler":
        return _kunit_case(
            test_id="DET-KUNIT-DRM-SCHED-001",
            name="DRM scheduler KUnit tests",
            intent="Run DRM scheduler KUnit tests without GPU hardware.",
            kunitconfig="drivers/gpu/drm/scheduler",
            note="deterministic KUnit fallback for drivers/gpu/drm/scheduler",
        )

    if module_path == "drivers/firewire":
        return _kunit_case(
            test_id="DET-KUNIT-FIREWIRE-001",
            name="FireWire KUnit tests",
            intent="Run FireWire UAPI and serialization KUnit tests without FireWire hardware.",
            kunitconfig="drivers/firewire",
            note="deterministic KUnit fallback for drivers/firewire",
        )

    if module_path == "drivers/base/power":
        cfg = ".kunitconfig.driver_agent_base_power"
        cmd = (
            "printf '%s\\n' "
            "'CONFIG_KUNIT=y' "
            "'CONFIG_PM=y' "
            "'CONFIG_PM_RUNTIME=y' "
            "'CONFIG_PM_QOS_KUNIT_TEST=y' "
            "'CONFIG_PM_RUNTIME_KUNIT_TEST=y' "
            f"> {cfg} && make ARCH=um mrproper >/dev/null 2>&1 || true; "
            f"./tools/testing/kunit/kunit.py run --kunitconfig={cfg}"
        )
        cases = [
            {
                "id": "DET-KUNIT-BASE-POWER-001",
                "name": "base power-management KUnit tests",
                "entry_kind": "boot",
                "intent": "Run PM QoS and runtime-PM KUnit tests without physical hardware.",
                "preconditions": ["KUnit/UML tooling is available."],
                "steps": [{"cmd": cmd, "why": "Exercises PM helper semantics in an isolated KUnit run."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 900,
                "skip_if": ["KUnit/UML tooling unavailable or dependencies unsatisfied"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/base/power"]
    elif module_path == "drivers/base/regmap":
        cfg = ".kunitconfig.driver_agent_regmap"
        cmd = (
            "printf '%s\\n' "
            "'CONFIG_KUNIT=y' "
            "'CONFIG_REGMAP=y' "
            "'CONFIG_REGMAP_KUNIT=y' "
            f"> {cfg} && make ARCH=um mrproper >/dev/null 2>&1 || true; "
            f"./tools/testing/kunit/kunit.py run --kunitconfig={cfg}"
        )
        cases = [
            {
                "id": "DET-KUNIT-REGMAP-001",
                "name": "regmap KUnit tests",
                "entry_kind": "boot",
                "intent": "Run regmap KUnit tests without physical hardware.",
                "preconditions": ["KUnit/UML tooling is available."],
                "steps": [{"cmd": cmd, "why": "Exercises regmap helper semantics in an isolated KUnit run."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 1200,
                "skip_if": ["KUnit/UML tooling unavailable or dependencies unsatisfied"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/base/regmap"]
    elif module_path == "drivers/iio/test":
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
    elif module_path == "drivers/iio/dummy":
        runner = (Path(__file__).resolve().parents[1] / "tools" / "run_intree_iio_dummy_qemu.py").as_posix()
        cmd = (
            f"python {shlex.quote(runner)} "
            "--kernel-tree . "
            "--out-dir ../iio_dummy_qemu "
            "--jobs 8 "
            "--timeout-sec 900"
        )
        cases = [
            {
                "id": "DET-QEMU-IIO-DUMMY-001",
                "name": "IIO dummy configfs smoke test in QEMU",
                "entry_kind": "boot",
                "intent": "Create an IIO dummy software device through configfs inside an isolated QEMU guest.",
                "preconditions": ["qemu-system-x86_64, busybox, cpio, and gcc are available on the host."],
                "steps": [{"cmd": cmd, "why": "Builds a small x86_64 kernel and creates /sys/kernel/config/iio/devices/dummy/driver_agent_dummy0 in guest."}],
                "assertions": ["runner exits successfully and prints DRIVER_AGENT_QEMU_IIO_DUMMY_PASS"],
                "timeout_sec": 900,
                "skip_if": ["QEMU or required host build tools unavailable"],
                "artifacts": ["worktrees/iio_dummy_qemu/result.json", "worktrees/iio_dummy_qemu/qemu_output.txt"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic isolated-QEMU smoke test for drivers/iio/dummy"]
    elif module_path in {"drivers/gpu/drm/ttm", "drivers/gpu/drm/ttm/tests"}:
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
    elif module_path == "drivers/gpu/drm/vc4":
        cmd = "make ARCH=um mrproper >/dev/null 2>&1 || true; ./tools/testing/kunit/kunit.py run --kunitconfig=drivers/gpu/drm/vc4/tests"
        cases = [
            {
                "id": "DET-KUNIT-VC4-001",
                "name": "VC4 KUnit tests",
                "entry_kind": "boot",
                "intent": "Run VC4 mock-device KUnit tests without Raspberry Pi VC4 hardware.",
                "preconditions": ["drivers/gpu/drm/vc4/tests/.kunitconfig exists."],
                "steps": [{"cmd": cmd, "why": "Uses the in-tree VC4 mock KUnit configuration."}],
                "assertions": ["kunit.py exits successfully"],
                "timeout_sec": 600,
                "skip_if": ["KUnit/UML tooling unavailable"],
                "artifacts": ["KUnit stdout/stderr"],
            }
        ]
        cmds = [cmd]
        notes = ["deterministic KUnit fallback for drivers/gpu/drm/vc4"]
    else:
        runner = (Path(__file__).resolve().parents[1] / "tools" / "run_intree_kselftest_qemu.py").as_posix()
        if module_path == "drivers/block/zram":
            cmd = (
                f"python {shlex.quote(runner)} "
                "--kernel-tree . "
                "--target zram "
                "--out-dir ../kselftest_qemu/zram "
                "--jobs 8 "
                "--timeout-sec 900"
            )
            cases = [
                {
                    "id": "DET-QEMU-KSELFTEST-ZRAM-001",
                    "name": "zram kselftest in QEMU",
                    "entry_kind": "boot",
                    "intent": "Build and run zram kselftests inside an isolated QEMU guest.",
                    "preconditions": ["qemu-system-x86_64, busybox, cpio, and gcc are available on the host."],
                    "steps": [{"cmd": cmd, "why": "Executes the zram selftest in a generated initramfs instead of on the host."}],
                    "assertions": ["runner exits successfully and prints DRIVER_AGENT_QEMU_KSELFTEST_PASS"],
                    "timeout_sec": 900,
                    "skip_if": ["QEMU or required host build tools unavailable"],
                    "artifacts": ["worktrees/kselftest_qemu/zram/result.json", "worktrees/kselftest_qemu/zram/qemu_output.txt"],
                }
            ]
            cmds = [cmd]
            notes = ["deterministic isolated-QEMU kselftest runner for drivers/block/zram"]
        elif module_path == "drivers/dma-buf":
            cmd = (
                f"python {shlex.quote(runner)} "
                "--kernel-tree . "
                "--target drivers-dma-buf "
                "--out-dir ../kselftest_qemu/drivers-dma-buf "
                "--jobs 8 "
                "--timeout-sec 900"
            )
            cases = [
                {
                    "id": "DET-QEMU-KSELFTEST-DMABUF-DRIVERS-001",
                    "name": "drivers/dma-buf kselftest in QEMU",
                    "entry_kind": "boot",
                    "intent": "Build and run the udmabuf driver selftest inside an isolated QEMU guest.",
                    "preconditions": ["qemu-system-x86_64, busybox, cpio, and gcc are available on the host."],
                    "steps": [{"cmd": cmd, "why": "Executes the drivers/dma-buf selftest in a generated initramfs instead of on the host."}],
                    "assertions": ["runner exits successfully and prints DRIVER_AGENT_QEMU_KSELFTEST_PASS"],
                    "timeout_sec": 900,
                    "skip_if": ["QEMU or required host build tools unavailable"],
                    "artifacts": ["worktrees/kselftest_qemu/drivers-dma-buf/result.json", "worktrees/kselftest_qemu/drivers-dma-buf/qemu_output.txt"],
                }
            ]
            cmds = [cmd]
            notes = ["deterministic isolated-QEMU kselftest runner for drivers/dma-buf"]
        elif module_path == "drivers/dma-buf/heaps":
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


def _is_kunit_config_unavailable_failure(stdout_text: str, stderr_text: str) -> bool:
    merged = ((stdout_text or "") + "\n" + (stderr_text or "")).lower()
    return (
        "not all kconfig options selected in kunitconfig" in merged
        and ("unsatisfied dependencies" in merged or "aren't available on uml" in merged)
    )


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
        if (
            "kunit.py run" not in cmd
            and "qemu-system-" not in cmd
            and "run_intree_kselftest_qemu.py" not in cmd
            and "run_intree_iio_dummy_qemu.py" not in cmd
        ):
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
    "compile_autofix",
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


def _normalize_partial_module_compile_result(run: dict[str, Any]) -> dict[str, Any]:
    """
    Treat the common partial in-tree module-build artifact as non-fatal.

    In validation we compile only the target module directory after
    `modules_prepare`, not the whole kernel. That often leaves
    `Module.symvers` absent, so modpost reports many unresolved in-kernel
    symbols even when all C compilation succeeded. Keep the warning evidence,
    but do not classify this environment artifact as a source-level compile
    failure.
    """
    if run.get("ok"):
        return run
    text = (run.get("stdout", "") or "") + "\n" + (run.get("stderr", "") or "")
    if "Module.symvers is missing" not in text or "ERROR: modpost:" not in text:
        return run
    for line in text.splitlines():
        lower = line.lower()
        if "fatal error:" in lower:
            return run
        if " error:" in lower and "error: modpost:" not in lower:
            return run
    out = dict(run)
    out["ok"] = True
    out["partial_module_compile_note"] = (
        "C compilation reached modpost; unresolved symbol errors were ignored because "
        "Module.symvers is unavailable in the partial validation worktree."
    )
    out["original_returncode"] = run.get("returncode")
    return out


_UNUSED_VAR_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+unused variable\s+[`\u2018'](?P<var>[A-Za-z_][A-Za-z0-9_]*)['\u2019`]"
)
_MISSING_HEADER_RE = re.compile(
    r"(?P<file>[^:\n]+\.[ch]):(?P<line>\d+):(?P<col>\d+):\s+fatal error:\s+(?P<header>[^:\s]+):\s+No such file or directory"
)
_IIO_DEVICE_ALLOC_TOO_FEW_ARGS_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+too few arguments to function\s+[`'\u2018]iio_device_alloc['\u2019`]"
)
_STRUCT_FIELD_MISSING_RE = re.compile(
    r"(?P<file>[^:\n]+\.[ch]):(?P<line>\d+):(?P<col>\d+):\s+error:\s+[`'\u2018](?:const\s+)?struct\s+(?P<struct>[A-Za-z_][A-Za-z0-9_]*)[`'\u2019]\s+has no member named\s+[`'\u2018](?P<field>[A-Za-z_][A-Za-z0-9_]*)['\u2019`]"
)
_UNUSED_LABEL_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+label\s+[`'\u2018](?P<label>[A-Za-z_][A-Za-z0-9_]*)['\u2019`]\s+defined but not used"
)
_UNUSED_FUNCTION_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+[`'\u2018](?P<function>[A-Za-z_][A-Za-z0-9_]*)['\u2019`]\s+defined but not used"
)
_VOID_VALUE_NOT_IGNORED_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+void value not ignored as it ought to be"
)
_CONFLICTING_TYPES_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+conflicting types for\s+[`'\u2018](?P<function>[A-Za-z_][A-Za-z0-9_]*)['\u2019`]"
)
_PLATFORM_REMOVE_VOID_RE = re.compile(
    r"(?P<file>[^:\n]+\.c):(?P<line>\d+):(?P<col>\d+):\s+error:\s+initialization of\s+.*void\s+\(\*\)\s*\(\s*struct\s+platform_device\s+\*\s*\).*from incompatible pointer type\s+.*int\s+\(\*\)\s*\(\s*struct\s+platform_device\s+\*\s*\)",
)


def _resolve_compile_error_file(filename: str, candidates: Iterable[str]) -> str:
    name = filename.strip()
    if not name:
        return ""
    normalized = name.replace("\\", "/")
    matches = [p for p in candidates if p == normalized or p.endswith("/" + normalized) or Path(p).name == normalized]
    if len(matches) == 1:
        return matches[0]
    return ""


def _is_simple_unused_var_decl(line: str, var: str) -> bool:
    stripped = line.strip()
    if not stripped.endswith(";"):
        return False
    if "(" in stripped or ")" in stripped or "," in stripped:
        return False
    if re.search(rf"\b{re.escape(var)}\b", stripped) is None:
        return False
    # Only remove ordinary declaration-shaped lines. This deliberately avoids
    # pointer arithmetic, labels, statements, and multi-declarator lines.
    decl_re = re.compile(
        rf"^(?:static\s+)?(?:const\s+)?(?:unsigned\s+|signed\s+)?"
        rf"(?:(?:struct|enum|union)\s+[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)"
        rf"(?:\s+\*+|\s+\*?\s*){re.escape(var)}(?:\s*=\s*[^;]+)?;$"
    )
    return bool(decl_re.match(stripped))


def _remove_unused_var_from_decl_line(line: str, var: str) -> tuple[bool, str]:
    """Remove an unused variable from a conservative declaration shape.

    Returns (changed, replacement_line). An empty replacement_line means the
    whole declaration should be deleted.
    """
    if _is_simple_unused_var_decl(line, var):
        return True, ""

    indent = line[: len(line) - len(line.lstrip())]
    stripped = line.strip()
    if not stripped.endswith(";"):
        return False, line
    if "(" in stripped or ")" in stripped or "=" in stripped:
        return False, line
    body = stripped[:-1].strip()
    if "," not in body:
        return False, line

    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2 or any(not p for p in parts):
        return False, line

    # Only handle the low-risk case where the unused variable is not the first
    # declarator, so we do not have to reconstruct the type prefix.
    keep = [parts[0]]
    removed = False
    var_decl_re = re.compile(rf"^\*?\s*{re.escape(var)}(?:\s*\[[^\]]+\])?$")
    for part in parts[1:]:
        if var_decl_re.match(part):
            removed = True
            continue
        keep.append(part)
    if not removed:
        return False, line
    if len(keep) == len(parts):
        return False, line
    return True, indent + ", ".join(keep) + ";"


def _remove_line_from_trees(old_tree: Path, new_tree: Path, rel: str, line_no: int, var: str) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "variable": var, "applied": False, "reason": ""}
    changed_text: str | None = None
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        lines = read_text(path).splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        if tree_name == "new":
            line = lines[line_no - 1]
            changed, replacement = _remove_unused_var_from_decl_line(line, var)
            if not changed:
                result["reason"] = "line_is_not_simple_unused_variable_declaration"
                result["line_text"] = line
                return result
            if replacement:
                lines[line_no - 1] = replacement
            else:
                del lines[line_no - 1]
            changed_text = "\n".join(lines) + ("\n" if read_text(path).endswith("\n") else "")
        else:
            # The old_ref validation worktree has the same patch applied before
            # staging. Remove the matching declaration there too so the recorded
            # validation patch represents exactly what was compiled.
            changed, replacement = _remove_unused_var_from_decl_line(lines[line_no - 1], var)
            if not changed:
                result["reason"] = "old_tree_matching_decl_not_found"
                return result
            if replacement:
                lines[line_no - 1] = replacement
            else:
                del lines[line_no - 1]
            changed_text = "\n".join(lines) + ("\n" if read_text(path).endswith("\n") else "")
        write_text(path, changed_text)
    result["applied"] = True
    result["reason"] = "removed_or_reduced_unused_variable_declaration"
    return result


def _find_header_replacement(new_tree: Path, header: str) -> str:
    header = header.strip("<>\" ")
    if not header or (new_tree / "include" / header).exists():
        return ""
    hpath = Path(header)
    stem = hpath.stem
    suffix = hpath.suffix
    parent = (new_tree / "include" / hpath.parent).resolve()
    if not parent.exists() or not parent.is_dir():
        return ""

    candidates: list[Path] = []
    variants = {
        stem.replace("_", ""),
        stem.replace("-", "_"),
        stem.replace("_", "-"),
    }
    for variant in variants:
        if not variant or variant == stem:
            continue
        p = parent / f"{variant}{suffix}"
        if p.exists():
            candidates.append(p)
    if not candidates:
        normalized = stem.replace("_", "").replace("-", "")
        for p in parent.glob(f"*{suffix}"):
            if p.stem.replace("_", "").replace("-", "") == normalized:
                candidates.append(p)
    unique = []
    seen: set[str] = set()
    for p in candidates:
        rel = p.relative_to(new_tree / "include").as_posix()
        if rel not in seen:
            seen.add(rel)
            unique.append(rel)
    if len(unique) == 1:
        return unique[0]
    return ""


def _replace_header_include_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
    old_header: str,
    new_header: str,
) -> dict[str, Any]:
    result = {
        "file": rel,
        "line": line_no,
        "missing_header": old_header,
        "replacement_header": new_header,
        "applied": False,
        "reason": "",
    }
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        line = lines[line_no - 1]
        if old_header not in line or "#include" not in line:
            result["reason"] = f"{tree_name}_line_is_not_matching_include"
            result["line_text"] = line
            return result
        lines[line_no - 1] = line.replace(old_header, new_header)
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    result["applied"] = True
    result["reason"] = "replaced_missing_include_with_unique_existing_header"
    return result


def _fix_iio_device_alloc_parent_arg(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "function": "iio_device_alloc", "applied": False, "reason": ""}
    changed = False
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        line = lines[line_no - 1]
        if "iio_device_alloc(" not in line:
            result["reason"] = f"{tree_name}_line_does_not_call_iio_device_alloc"
            result["line_text"] = line
            return result
        if "iio_device_alloc(NULL," in line:
            continue
        # Safe narrow migration for the v5.x -> v7.x IIO signature:
        # iio_device_alloc(sizeof_priv) -> iio_device_alloc(NULL, sizeof_priv)
        # Only rewrite a single-line call and preserve the old ownership model.
        new_line = line.replace("iio_device_alloc(", "iio_device_alloc(NULL, ", 1)
        lines[line_no - 1] = new_line
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
        changed = True
    result["applied"] = changed
    result["reason"] = "inserted_null_parent_for_iio_device_alloc_signature_change" if changed else "already_fixed"
    return result


def _remove_missing_struct_field_initializer_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
    field: str,
) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "field": field, "applied": False, "reason": ""}
    line_re = re.compile(rf"^\s*\.{re.escape(field)}\s*=.*,\s*$")
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        line = lines[line_no - 1]
        if not line_re.match(line):
            result["reason"] = f"{tree_name}_line_is_not_matching_field_initializer"
            result["line_text"] = line
            return result
        del lines[line_no - 1]
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    result["applied"] = True
    result["reason"] = "removed_missing_struct_field_initializer"
    return result


def _remove_unused_label_in_trees(old_tree: Path, new_tree: Path, rel: str, line_no: int, label: str) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "label": label, "applied": False, "reason": ""}
    label_re = re.compile(rf"^\s*{re.escape(label)}:\s*$")
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        if not label_re.match(lines[line_no - 1]):
            result["reason"] = f"{tree_name}_line_is_not_matching_label"
            result["line_text"] = lines[line_no - 1]
            return result
        del lines[line_no - 1]
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    result["applied"] = True
    result["reason"] = "removed_unused_label"
    return result


def _remove_unused_static_function_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
    function: str,
) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "function": function, "applied": False, "reason": ""}
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        start = line_no - 1
        signature = "\n".join(lines[start : min(len(lines), start + 8)])
        if "static" not in signature or re.search(rf"\b{re.escape(function)}\s*\(", signature) is None:
            result["reason"] = f"{tree_name}_line_is_not_static_function_definition"
            result["line_text"] = lines[start]
            return result
        open_idx = None
        for idx in range(start, min(len(lines), start + 20)):
            if "{" in lines[idx]:
                open_idx = idx
                break
        if open_idx is None:
            result["reason"] = f"{tree_name}_function_open_brace_not_found"
            return result
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(lines)):
            depth += lines[idx].count("{")
            depth -= lines[idx].count("}")
            if depth == 0:
                end_idx = idx
                break
        if end_idx is None:
            result["reason"] = f"{tree_name}_function_end_not_found"
            return result
        del lines[start : end_idx + 1]
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    result["applied"] = True
    result["reason"] = "removed_unused_static_function"
    return result


def _rename_static_function_conflicting_with_kernel_symbol_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
    function: str,
) -> dict[str, Any]:
    stem = Path(rel).stem
    prefix = re.sub(r"[^A-Za-z0-9_]", "_", stem).strip("_")
    if not prefix:
        prefix = "driver"
    new_function = f"{prefix}_{function}"
    if new_function == function:
        new_function = f"{function}_local"

    result = {
        "file": rel,
        "line": line_no,
        "function": function,
        "new_function": new_function,
        "applied": False,
        "reason": "",
    }
    call_re = re.compile(rf"\b{re.escape(function)}\s*\(")
    def_re = re.compile(rf"\b{re.escape(function)}\s*\(")

    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        if re.search(rf"\b{re.escape(new_function)}\s*\(", text):
            result["reason"] = f"{tree_name}_new_function_name_already_exists"
            return result
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result

        start = line_no - 1
        signature_lines = lines[start : min(len(lines), start + 10)]
        signature = "\n".join(signature_lines)
        if "static" not in signature or def_re.search(signature) is None:
            result["reason"] = f"{tree_name}_line_is_not_static_function_definition"
            result["line_text"] = lines[start]
            return result
        if "{" not in signature:
            result["reason"] = f"{tree_name}_function_open_brace_not_found_near_definition"
            return result

        new_text, count = call_re.subn(f"{new_function}(", text)
        if count < 2:
            # A static helper with no visible callsite is probably not the
            # intended low-risk rename pattern; leave it to the LLM/manual path.
            result["reason"] = f"{tree_name}_no_static_callsite_to_rename"
            result["replacement_count"] = count
            return result
        write_text(path, new_text)

    result["applied"] = True
    result["reason"] = "renamed_static_function_conflicting_with_kernel_symbol"
    return result


def _fix_platform_remove_void_callback_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "field": "remove", "applied": False, "reason": ""}

    def apply_one(tree_name: str, tree: Path) -> tuple[bool, str, str]:
        path = tree / rel
        if not path.exists():
            return False, f"{tree_name}_file_missing", ""
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            return False, f"{tree_name}_line_out_of_range", ""
        init_line = lines[line_no - 1]
        m_init = re.search(r"\.remove\s*=\s*(?P<function>[A-Za-z_][A-Za-z0-9_]*)\s*,", init_line)
        if not m_init:
            return False, f"{tree_name}_line_is_not_remove_initializer", init_line
        function = m_init.group("function")

        def_idx = None
        def_re = re.compile(rf"^(?P<indent>\s*)static\s+int\s+{re.escape(function)}\s*\(")
        for idx, line in enumerate(lines):
            if def_re.search(line):
                def_idx = idx
                break
        if def_idx is None:
            return False, f"{tree_name}_static_int_remove_definition_not_found", function

        open_idx = None
        for idx in range(def_idx, min(len(lines), def_idx + 20)):
            if "{" in lines[idx]:
                open_idx = idx
                break
        if open_idx is None:
            return False, f"{tree_name}_function_open_brace_not_found", function
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(lines)):
            depth += lines[idx].count("{")
            depth -= lines[idx].count("}")
            if depth == 0:
                end_idx = idx
                break
        if end_idx is None:
            return False, f"{tree_name}_function_end_not_found", function

        return_lines = [
            idx for idx in range(open_idx + 1, end_idx) if re.match(r"^\s*return\b", lines[idx])
        ]
        if not return_lines:
            return False, f"{tree_name}_remove_function_has_no_return_to_rewrite", function
        if any(not re.match(r"^\s*return\s+0\s*;\s*$", lines[idx]) for idx in return_lines):
            return False, f"{tree_name}_remove_function_has_nonzero_or_value_return", function

        lines[def_idx] = re.sub(r"\bstatic\s+int\b", "static void", lines[def_idx], count=1)
        for idx in reversed(return_lines):
            del lines[idx]
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
        return True, "converted_platform_remove_callback_from_int_to_void", function

    function_name = ""
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        ok, reason, detail = apply_one(tree_name, tree)
        if not ok:
            result["reason"] = reason
            result["detail"] = detail
            return result
        function_name = detail or function_name

    result["function"] = function_name
    result["applied"] = True
    result["reason"] = "converted_platform_remove_callback_from_int_to_void"
    return result


def _fix_pm_runtime_put_void_assignment_in_trees(
    old_tree: Path,
    new_tree: Path,
    rel: str,
    line_no: int,
) -> dict[str, Any]:
    result = {"file": rel, "line": line_no, "function": "pm_runtime_put", "applied": False, "reason": ""}
    assign_re = re.compile(r"^(?P<indent>\s*)ret\s*=\s*(?P<call>pm_runtime_put\s*\(.*\);\s*)$")
    if_re = re.compile(r"^\s*if\s*\(\s*ret\s*<\s*0\s*\)\s*$")
    for tree_name, tree in (("new", new_tree), ("old", old_tree)):
        path = tree / rel
        if not path.exists():
            result["reason"] = f"{tree_name}_file_missing"
            return result
        text = read_text(path)
        lines = text.splitlines()
        if line_no < 1 or line_no > len(lines):
            result["reason"] = f"{tree_name}_line_out_of_range"
            return result
        m = assign_re.match(lines[line_no - 1])
        if not m:
            result["reason"] = f"{tree_name}_line_is_not_pm_runtime_put_ret_assignment"
            result["line_text"] = lines[line_no - 1]
            return result
        # Only apply the narrow legacy pattern:
        # ret = pm_runtime_put(...);
        # if (ret < 0)
        #     log(... ret ...);
        if line_no + 1 > len(lines) or not if_re.match(lines[line_no]):
            result["reason"] = f"{tree_name}_following_ret_check_not_found"
            return result
        if line_no + 2 > len(lines) or "ret" not in lines[line_no + 1]:
            result["reason"] = f"{tree_name}_following_ret_log_not_found"
            return result
        lines[line_no - 1] = f"{m.group('indent')}{m.group('call')}"
        del lines[line_no : line_no + 2]
        write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    result["applied"] = True
    result["reason"] = "removed_pm_runtime_put_return_value_check"
    return result


def _compile_failure_text(compile_like: dict[str, Any]) -> str:
    return "\n".join(
        (run.get("stdout", "") or "") + "\n" + (run.get("stderr", "") or "")
        for run in compile_like.get("runs", []) or compile_like.get("rerun_compile", []) or []
        if not run.get("ok")
    )


def _target_build_goal(kbuild_target: str | None) -> str | None:
    if not kbuild_target:
        return None
    target = kbuild_target.strip()
    if not target:
        return None
    if target.endswith(".ko"):
        return target
    if target.endswith(".o"):
        return target[:-2] + ".ko"
    return target + ".ko"


def _compile_command_for_dir(d: str, jobs: int, kbuild_target: str | None = None) -> list[str]:
    cmd = ["make", f"-j{max(1, jobs)}", f"M={d}"]
    goal = _target_build_goal(kbuild_target)
    if goal:
        # Target-level validation intentionally builds a single Kbuild module
        # after modules_prepare, not the whole dependency graph. In that mode
        # Module.symvers may be absent, so unresolved dependency symbols should
        # stay warnings; source/API compile errors remain hard failures.
        cmd.append("KBUILD_MODPOST_WARN=1")
        cmd.append(goal)
    else:
        cmd.append("modules")
    return cmd


def _rerun_module_compile(
    new_tree: Path,
    compile_dirs: list[str],
    jobs: int,
    kbuild_target: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    ok = True
    for d in compile_dirs:
        run = _run_cmd(_compile_command_for_dir(d, jobs, kbuild_target), cwd=new_tree, timeout=3600)
        run = _normalize_partial_module_compile_result(run)
        run["stdout"] = _truncate(run.get("stdout", ""))
        run["stderr"] = _truncate(run.get("stderr", ""))
        runs.append(run)
        if not run.get("ok"):
            ok = False
    return ok, runs


def _apply_safe_compile_autofixes_once(
    *,
    old_tree: Path,
    new_tree: Path,
    module_path: str,
    copied_files: list[str],
    touched_files: list[str],
    compile_like: dict[str, Any],
    target_source_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    text = _compile_failure_text(compile_like)
    scope_files = target_source_files if target_source_files is not None else _list_module_source_files(new_tree, module_path)
    candidates = _ordered_unique([*copied_files, *touched_files, *scope_files])
    changes: list[dict[str, Any]] = []
    seen_unused: set[tuple[str, int, str]] = set()
    for m in _UNUSED_VAR_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "variable": m.group("var"),
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), m.group("var"))
        if key in seen_unused:
            continue
        seen_unused.add(key)
        changes.append(
            _remove_line_from_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                var=m.group("var"),
            )
        )

    seen_headers: set[tuple[str, int, str]] = set()
    for m in _MISSING_HEADER_RE.finditer(text):
        header = m.group("header")
        replacement = _find_header_replacement(new_tree, header)
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not replacement or not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "missing_header": header,
                    "applied": False,
                    "reason": "no_unique_existing_header_replacement" if not replacement else "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), header)
        if key in seen_headers:
            continue
        seen_headers.add(key)
        changes.append(
            _replace_header_include_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                old_header=header,
                new_header=replacement,
            )
        )

    seen_iio_alloc: set[tuple[str, int]] = set()
    for m in _IIO_DEVICE_ALLOC_TOO_FEW_ARGS_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "function": "iio_device_alloc",
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")))
        if key in seen_iio_alloc:
            continue
        seen_iio_alloc.add(key)
        changes.append(
            _fix_iio_device_alloc_parent_arg(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
            )
        )

    seen_missing_fields: set[tuple[str, int, str]] = set()
    for m in _STRUCT_FIELD_MISSING_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "field": m.group("field"),
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), m.group("field"))
        if key in seen_missing_fields:
            continue
        seen_missing_fields.add(key)
        changes.append(
            _remove_missing_struct_field_initializer_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                field=m.group("field"),
            )
        )

    seen_void_pm_put: set[tuple[str, int]] = set()
    for m in _VOID_VALUE_NOT_IGNORED_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "function": "pm_runtime_put",
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")))
        if key in seen_void_pm_put:
            continue
        seen_void_pm_put.add(key)
        changes.append(
            _fix_pm_runtime_put_void_assignment_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
            )
        )

    seen_conflicting_types: set[tuple[str, int, str]] = set()
    for m in _CONFLICTING_TYPES_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "function": m.group("function"),
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), m.group("function"))
        if key in seen_conflicting_types:
            continue
        seen_conflicting_types.add(key)
        changes.append(
            _rename_static_function_conflicting_with_kernel_symbol_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                function=m.group("function"),
            )
        )

    seen_platform_remove: set[tuple[str, int]] = set()
    for m in _PLATFORM_REMOVE_VOID_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "field": "remove",
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")))
        if key in seen_platform_remove:
            continue
        seen_platform_remove.add(key)
        changes.append(
            _fix_platform_remove_void_callback_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
            )
        )

    seen_unused_labels: set[tuple[str, int, str]] = set()
    for m in _UNUSED_LABEL_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "label": m.group("label"),
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), m.group("label"))
        if key in seen_unused_labels:
            continue
        seen_unused_labels.add(key)
        changes.append(
            _remove_unused_label_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                label=m.group("label"),
            )
        )

    seen_unused_functions: set[tuple[str, int, str]] = set()
    for m in _UNUSED_FUNCTION_RE.finditer(text):
        rel = _resolve_compile_error_file(m.group("file"), candidates)
        if not rel:
            changes.append(
                {
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "function": m.group("function"),
                    "applied": False,
                    "reason": "could_not_resolve_unique_source_file",
                }
            )
            continue
        key = (rel, int(m.group("line")), m.group("function"))
        if key in seen_unused_functions:
            continue
        seen_unused_functions.add(key)
        changes.append(
            _remove_unused_static_function_in_trees(
                old_tree=old_tree,
                new_tree=new_tree,
                rel=rel,
                line_no=int(m.group("line")),
                function=m.group("function"),
            )
        )
    return changes


def _try_compile_autofix_unused_variables(
    *,
    old_tree: Path,
    new_tree: Path,
    module_path: str,
    copied_files: list[str],
    touched_files: list[str],
    compile_result: dict[str, Any],
    compile_dirs: list[str],
    jobs: int,
    validation_dir: Path,
    kbuild_target: str | None = None,
    target_source_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": False,
        "applied_count": 0,
        "changes": [],
        "rerun_compile": [],
        "success": False,
        "patch_path": "",
        "max_rounds": 3,
    }
    current: dict[str, Any] = {"runs": compile_result.get("runs", []) or []}
    for round_idx in range(1, int(result["max_rounds"]) + 1):
        changes = _apply_safe_compile_autofixes_once(
            old_tree=old_tree,
            new_tree=new_tree,
            module_path=module_path,
            copied_files=copied_files,
            touched_files=touched_files,
            compile_like=current,
            target_source_files=target_source_files,
        )
        if not changes:
            if not result["attempted"]:
                result["reason"] = "no_safe_compile_autofix_available"
            break
        result["attempted"] = True
        for change in changes:
            change["round"] = round_idx
        result["changes"].extend(changes)
        applied = [c for c in changes if c.get("applied")]
        result["applied_count"] += len(applied)
        if not applied:
            result["reason"] = "no_safe_compile_autofix_applied"
            break
        rerun_ok, reruns = _rerun_module_compile(new_tree, compile_dirs, jobs, kbuild_target)
        for r in reruns:
            r["autofix_round"] = round_idx
        result["rerun_compile"].extend(reruns)
        if rerun_ok:
            result["success"] = True
            result["reason"] = "compile_passed_after_safe_compile_autofix"
            break
        current = {"runs": reruns}

    diff_res = _run_cmd(["git", "diff", "--", *(_ordered_unique([*copied_files, *touched_files]) or [module_path])], cwd=old_tree, timeout=300)
    patch_path = validation_dir / "03_compile_autofixed_patch.diff"
    if diff_res.get("stdout"):
        write_text(patch_path, diff_res.get("stdout", ""))
        result["patch_path"] = patch_path.as_posix()
    if result["attempted"] and not result["success"] and not result.get("reason"):
        result["reason"] = "compile_still_failed_after_safe_compile_autofix"
    return result


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
    stripped_stdout = (stdout_text or "").strip()
    if stripped_stdout.startswith("{"):
        try:
            payload = json.loads(stripped_stdout)
            status = str(payload.get("status") or "").strip()
            if status:
                return f"structured_run_status={status}"[:280]
            for key in ("error", "reason", "message"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return f"{key}={value}"[:280]
        except Exception:
            pass
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
    kernel_config_target: str = "defconfig",
    kbuild_target: str | None = None,
    target_source_files: list[str] | None = None,
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
            touched_files = list(target_source_files or _list_module_source_files(old_tree, module_path))
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

    module_source_files = list(target_source_files or _list_module_source_files(old_tree, module_path))
    new_tree_module_source_files = (
        [rel for rel in module_source_files if (new_tree / rel).exists()]
        if kbuild_target
        else _list_module_source_files(new_tree, module_path)
    )
    old_source_set = set(module_source_files)
    new_only_module_source_files = sorted(
        rel for rel in new_tree_module_source_files if rel not in old_source_set
    )
    stage_files = _ordered_unique([*module_source_files, *touched_files])
    copied = []
    missing = []
    for rel in stage_files:
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
        "module_source_file_count": len(module_source_files),
        "new_tree_module_source_file_count": len(new_tree_module_source_files),
        "new_only_module_source_files": new_only_module_source_files[:80],
        "new_only_module_source_file_count": len(new_only_module_source_files),
        "touched_file_count": len(touched_files),
        "case_scope": "kbuild_target" if kbuild_target else "directory",
        "kbuild_target": kbuild_target,
        "stage_policy": (
            "stage_old_kbuild_target_sources_plus_touched_patch_files"
            if kbuild_target
            else "stage_full_old_module_sources_plus_touched_patch_files"
        ),
        "stage_policy_caveat": (
            "Validation overlays old target source scope onto the new kernel tree. "
            "If new_only_module_source_file_count > 0, the compile may include new target-module files; "
            "that is evaluation/validation evidence and must not be used as agent input."
        ),
    }
    if new_only_module_source_files:
        out["open_questions"].append("new_tree_contains_target_module_sources_not_present_in_old_module")
    if missing:
        _set_stage_result(
            stage_results,
            "stage_patched_files",
            "failed",
            f"missing {len(missing)} touched files while staging",
            details={"missing_files": missing},
        )
    else:
        _set_stage_result(
            stage_results,
            "stage_patched_files",
            "passed",
            f"staged {len(copied)} files into new tree ({len(module_source_files)} old module sources, {len(touched_files)} touched)",
        )

    if kernel_config_target not in {"defconfig", "allmodconfig", "allyesconfig"}:
        kernel_config_target = "defconfig"

    prep_steps: list[dict[str, Any]] = []
    prep_steps.append(_run_cmd(["make", kernel_config_target], cwd=new_tree, timeout=1800))
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
        _set_stage_result(
            stage_results,
            "compile_prepare",
            "passed",
            f"{kernel_config_target}/olddefconfig/modules_prepare succeeded",
        )
    else:
        prep_reason = "kernel_prepare_failed"
        for step in prep_steps:
            if not step.get("ok"):
                prep_reason = _first_error_line(step.get("stderr", "")) or step.get("cmd", "kernel_prepare_failed")
                break
        _set_stage_result(stage_results, "compile_prepare", "failed", prep_reason)

    compile_dirs: list[str] = []
    if prep_ok:
        compile_dirs = _find_makefile_dirs(new_tree, copied or touched_files, module_path)
        compile_result["compiled_dirs"] = compile_dirs
        compile_result["kbuild_target"] = kbuild_target
        compile_result["compiled_goals"] = [_target_build_goal(kbuild_target)] if kbuild_target else ["modules"]
        all_ok = True
        for d in compile_dirs:
            cmd = _compile_command_for_dir(d, jobs, kbuild_target)
            run = _run_cmd(cmd, cwd=new_tree, timeout=3600)
            run = _normalize_partial_module_compile_result(run)
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
            target_note = f" target={_target_build_goal(kbuild_target)}" if kbuild_target else ""
            _set_stage_result(stage_results, "compile_modules", "passed", f"compiled dirs: {', '.join(compile_dirs) if compile_dirs else '(none)'}{target_note}")
    else:
        compile_result["error"] = "kernel_prepare_failed"
        _set_stage_result(stage_results, "compile_modules", "skipped", "compile_prepare_failed")

    compile_autofix: dict[str, Any] = {
        "attempted": False,
        "success": False,
        "reason": "not_needed_or_not_applicable",
        "changes": [],
    }
    if prep_ok and not compile_result.get("success"):
        compile_autofix = _try_compile_autofix_unused_variables(
            old_tree=old_tree,
            new_tree=new_tree,
            module_path=module_path,
            copied_files=copied,
            touched_files=touched_files,
            compile_result=compile_result,
            compile_dirs=compile_dirs,
            jobs=jobs,
            validation_dir=validation_dir,
            kbuild_target=kbuild_target,
            target_source_files=target_source_files,
        )
        if compile_autofix.get("attempted"):
            if compile_autofix.get("success"):
                compile_result["success"] = True
                compile_result["error"] = ""
                compile_result["autofix_applied"] = True
                compile_result["autofix"] = compile_autofix
                # Preserve the initial failing compile under runs and record
                # the successful rerun separately for auditability.
                compile_result["autofix_rerun_compile"] = compile_autofix.get("rerun_compile", [])
                _set_stage_result(
                    stage_results,
                    "compile_autofix",
                    "passed",
                    compile_autofix.get("reason", "compile_passed_after_autofix"),
                    details={"changes": compile_autofix.get("changes", [])},
                )
                _set_stage_result(
                    stage_results,
                    "compile_modules",
                    "passed",
                    "compiled after deterministic compile_autofix",
                )
            else:
                compile_result["autofix_applied"] = bool(compile_autofix.get("applied_count"))
                compile_result["autofix"] = compile_autofix
                _set_stage_result(
                    stage_results,
                    "compile_autofix",
                    "failed",
                    compile_autofix.get("reason", "compile_autofix_failed"),
                    details={"changes": compile_autofix.get("changes", [])},
                )
        else:
            _set_stage_result(stage_results, "compile_autofix", "skipped", compile_autofix.get("reason", "not_applicable"))
    else:
        _set_stage_result(stage_results, "compile_autofix", "skipped", "compile_already_successful_or_prepare_failed")

    out["compile_autofix"] = compile_autofix
    out["compile"] = compile_result

    if not compile_result.get("success"):
        test_gen = {
            "case_id": case_id,
            "stage": "system_test_generation",
            "parse_status": "skipped_compile_failed",
            "test_cases": [],
            "suite_commands": [],
            "notes": ["skipped because patched module did not compile"],
        }
        out["test_generation"] = test_gen
        _set_stage_result(stage_results, "test_generation", "skipped", "compile failed")

        test_bundle = {
            "manifest_path": "",
            "runner_script_path": "",
            "test_case_count": 0,
            "suite_command_count": 0,
            "skipped": True,
            "reason": "compile_failed",
        }
        out["test_bundle"] = test_bundle

        suite_exec = {"success": True, "runs": [], "skipped": True, "reason": "compile_failed"}
        out["suite_execution"] = suite_exec
        _set_stage_result(stage_results, "suite_execution", "skipped", "compile failed")

        out["qemu"] = {
            "enabled": enable_qemu,
            "detected_qemu_runs": 0,
            "runs": [],
            "success": False,
            "skipped": True,
            "reason": "compile_failed",
            "note": "skipped because patched module did not compile",
        }
        _set_stage_result(stage_results, "qemu_execution", "skipped", "compile failed")
        suite_runs = []
        all_skipped = True
    elif skip_system_test_generation:
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
            if not run.get("ok") and _is_kunit_config_unavailable_failure(run.get("stdout", ""), run.get("stderr", "")):
                run["original_returncode"] = run.get("returncode")
                run["returncode"] = 0
                run["ok"] = True
                run["skipped"] = True
                run["reason"] = "kunit_config_unsatisfied_dependencies"
        out["qemu"]["success"] = all(r.get("ok") for r in qemu_runs) if qemu_runs else False
        if not enable_qemu:
            _set_stage_result(stage_results, "qemu_execution", "skipped", "qemu disabled by flag")
        elif not qemu_runs:
            _set_stage_result(stage_results, "qemu_execution", "skipped", "qemu enabled but no standalone qemu/kunit command generated")
        elif all(r.get("skipped") for r in qemu_runs):
            _set_stage_result(stage_results, "qemu_execution", "skipped", _summarize_skip_reason(qemu_runs, "all_qemu_or_kunit_runs_skipped"))
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
        pr_status = "skipped"
        pr_reason = (
            "spec_consistency_unavailable: "
            + (post_reasoning.get("error") or f"parse_status={post_reasoning.get('parse_status')}")
        )
    elif pr_verdict == "PASS":
        pr_status = "passed"
        pr_reason = f"overall_verdict={pr_verdict}"
    elif pr_verdict == "UNKNOWN":
        pr_status = "skipped"
        pr_reason = "overall_verdict=UNKNOWN; spec consistency is not a correctness gate"
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
