#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from driver_agent_pregen.llm_client import LLMClient, LLMConfig  # noqa: E402
from driver_agent_pregen.utils import now_timestamp, write_json, write_text  # noqa: E402

SOURCE_EXTS = {".c", ".h", ".rs"}


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None, check: bool = False) -> dict[str, Any]:
    start = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    out = {
        "cmd": cmd,
        "cwd": str(cwd) if cwd else None,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_sec": round(time.time() - start, 3),
        "ok": proc.returncode == 0,
    }
    if check and proc.returncode != 0:
        raise RuntimeError(json.dumps(out, indent=2))
    return out


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def export_external_source(
    *,
    repo_url: str,
    ref: str,
    source_subdir: str,
    dest: Path,
    cache_root: Path,
) -> dict[str, Any]:
    repo_name = repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
    cache = cache_root / repo_name
    if cache.exists() and (cache / ".git").exists():
        fetch = run(["git", "-C", str(cache), "fetch", "--all", "--tags"], timeout=600)
    else:
        cache_root.mkdir(parents=True, exist_ok=True)
        fetch = run(["git", "clone", repo_url, str(cache)], timeout=1200)

    commit = ""
    ref_errors = []
    for candidate in [ref, f"refs/tags/{ref}", f"refs/remotes/origin/{ref}", f"origin/{ref}"]:
        res = run(["git", "-C", str(cache), "rev-parse", "--verify", candidate], timeout=120)
        if res.get("ok"):
            commit = str(res.get("stdout", "")).strip()
            break
        ref_errors.append(res)
    if not commit:
        raise RuntimeError(json.dumps({"error": "oot_ref_not_found", "ref": ref, "attempts": ref_errors}, indent=2))

    if dest.exists():
        shutil.rmtree(dest)
    tmp = dest.parent / f"{dest.name}_tmp_flat"
    if tmp.exists():
        shutil.rmtree(tmp)
    dest.mkdir(parents=True, exist_ok=True)

    archive = subprocess.run(
        ["git", "-C", str(cache), "archive", commit, source_subdir],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if archive.returncode != 0:
        raise RuntimeError(archive.stderr.decode(errors="replace"))

    extract = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if extract.returncode != 0:
        raise RuntimeError(extract.stderr.decode(errors="replace"))

    if source_subdir in {"", "."}:
        return {"repo_url": repo_url, "ref": ref, "commit": commit, "source_subdir": source_subdir, "fetch": fetch}

    extracted = dest / source_subdir
    shutil.move(str(extracted), str(tmp))
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(tmp), str(dest))
    return {"repo_url": repo_url, "ref": ref, "commit": commit, "source_subdir": source_subdir, "fetch": fetch}


def list_sources(root: Path) -> list[dict[str, str]]:
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix() if path.exists() else ""
        if path.name.endswith(".mod.c") or path.name in {"Module.symvers", "modules.order"}:
            continue
        if "/." in f"/{rel}" or path.name.startswith("."):
            continue
        if path.is_file() and (path.suffix in SOURCE_EXTS or path.name in {"Makefile", "Kbuild"}):
            out.append({"path": rel, "content": path.read_text(errors="replace")})
    return out


def prepare_worktree(repo: Path, ref: str, dest: Path, *, keep_existing: bool = False) -> dict[str, Any]:
    if dest.exists() and keep_existing:
        return {"ok": True, "reused": True, "path": str(dest)}
    if dest.exists():
        run(["git", "-C", str(repo), "worktree", "remove", "-f", str(dest)], timeout=300)
        if dest.exists():
            shutil.rmtree(dest)
    res = run(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), ref], timeout=600)
    return res


def _apply_kernel_config_options(tree: Path, config_options: list[str] | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    script = tree / "scripts" / "config"
    if not config_options or not script.exists():
        return results
    for raw in config_options:
        item = (raw or "").strip()
        if not item:
            continue
        if item.startswith("CONFIG_"):
            item = item[len("CONFIG_") :]
        if "=" in item:
            name, value = item.split("=", 1)
            value = value.strip().lower()
        else:
            name, value = item, "y"
        name = name.strip()
        if value in {"m", "module"}:
            cmd = [str(script), "--module", name]
        elif value in {"n", "disable", "off"}:
            cmd = [str(script), "--disable", name]
        else:
            cmd = [str(script), "--enable", name]
        results.append(run(cmd, cwd=tree, timeout=120))
    return results


def prepare_kernel(
    tree: Path,
    jobs: int,
    *,
    config_target: str = "defconfig",
    config_options: list[str] | None = None,
) -> list[dict[str, Any]]:
    results = []
    if not (tree / ".config").exists():
        target = config_target if config_target in {"defconfig", "allmodconfig", "allyesconfig"} else "defconfig"
        results.append(run(["make", f"-j{jobs}", target], cwd=tree, timeout=7200))
    results.extend(_apply_kernel_config_options(tree, config_options))
    results.append(run(["make", f"-j{jobs}", "olddefconfig"], cwd=tree, timeout=1800))
    results.append(run(["make", f"-j{jobs}", "modules_prepare"], cwd=tree, timeout=3600))
    return results


def build_external(kernel_tree: Path, module_dir: Path, jobs: int, extra_make_args: list[str] | None = None) -> dict[str, Any]:
    # The runner uses modules_prepare for speed. Recent kernels may make missing
    # Module.symvers fatal at modpost time; keep API/C compilation errors fatal
    # but do not fail solely because the full kernel was not built.
    return run(
        [
            "make",
            "-C",
            str(kernel_tree),
            f"M={module_dir}",
            "KBUILD_MODPOST_WARN=1",
            *(extra_make_args or []),
            f"-j{jobs}",
            "modules",
        ],
        timeout=1800,
    )


def _kernel_release(kernel_tree: Path) -> str:
    res = run(["make", "-s", "kernelrelease"], cwd=kernel_tree, timeout=120)
    if res.get("returncode") != 0:
        return ""
    return (res.get("stdout") or "").strip()


def prepare_qemu_module_tree(
    run_dir: Path,
    kernel_tree: Path,
    oot_module_ko: Path,
    jobs: int,
    dependency_module_dirs: list[str] | None = None,
    dependency_module_targets: list[str] | None = None,
    extra_make_args: list[str] | None = None,
) -> dict[str, Any]:
    """Build/copy selected dependency modules and place OOT module under extra/.

    External modules may compile while still depending on in-kernel modules
    such as videodev.ko. QEMU validation needs a real /lib/modules tree so the
    guest can use modprobe rather than a bare insmod with unresolved symbols.
    Building all kernel modules is too expensive, so callers should provide the
    small in-tree module directories required by the OOT module.
    """

    stage = run_dir / "kernel_modules_staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    krel = _kernel_release(kernel_tree)
    if not krel:
        return {"ok": False, "reason": "kernelrelease_failed"}
    copied: list[str] = []
    build_runs: list[dict[str, Any]] = []
    full_modules_fallback_done = False

    def copy_kernel_module(ko: Path) -> None:
        if not ko.exists():
            return
        dest = stage / "lib" / "modules" / krel / "kernel" / ko.relative_to(kernel_tree)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ko, dest)
        copied.append(dest.relative_to(stage).as_posix())

    for target in dependency_module_targets or []:
        target = target.strip().strip("/")
        if not target:
            continue
        copied_before = len(copied)
        build = run(["make", "KBUILD_MODPOST_WARN=1", f"-j{jobs}", target], cwd=kernel_tree, timeout=3600)
        build_runs.append(build | {"target": target, "nonfatal": True})
        copy_kernel_module(kernel_tree / target)
        if len(copied) == copied_before:
            build_runs.append({"ok": False, "reason": "dependency_target_no_ko", "target": target})

    for rel in dependency_module_dirs or []:
        rel = rel.strip().strip("/")
        if not rel:
            continue
        src_dir = kernel_tree / rel
        if not src_dir.exists():
            build_runs.append({"ok": False, "reason": "dependency_dir_missing", "dir": rel})
            continue
        # For in-tree dependency dirs, directory targets reliably build the
        # modules selected by .config. `M=... modules` is intended primarily for
        # external module builds and may produce no .ko for some in-tree paths.
        copied_before = len(copied)
        build = run(["make", "KBUILD_MODPOST_WARN=1", f"-j{jobs}", f"{rel}/"], cwd=kernel_tree, timeout=3600)
        build_runs.append(build | {"dir": rel, "method": "path_target", "nonfatal": True})
        for ko in sorted(src_dir.rglob("*.ko")):
            copy_kernel_module(ko)
        if build.get("ok") and len(copied) == copied_before:
            # Some composite in-tree modules do not materialize through the
            # path target form even though the directory build exits 0. Use
            # the module-build interface as a conservative fallback before
            # declaring the dependency unavailable for QEMU.
            fallback = run(
                ["make", "KBUILD_MODPOST_WARN=1", f"-j{jobs}", f"M={src_dir}", "modules"],
                cwd=kernel_tree,
                timeout=3600,
            )
            build_runs.append(fallback | {"dir": rel, "method": "M_modules_fallback", "nonfatal": True})
            for ko in sorted(src_dir.rglob("*.ko")):
                copy_kernel_module(ko)
        if len(copied) == copied_before:
            # If the in-tree directory still did not produce modules, fall
            # back to the normal configured kernel module build once. This
            # preserves in-tree Kbuild semantics for composite modules such as
            # media/videodev, while still copying only the requested dirs into
            # the guest module tree.
            if not full_modules_fallback_done:
                full = run(
                    ["make", "KBUILD_MODPOST_WARN=1", f"-j{jobs}", "modules"],
                    cwd=kernel_tree,
                    timeout=7200,
                )
                build_runs.append(full | {"method": "full_modules_fallback", "nonfatal": True})
                full_modules_fallback_done = True
            for ko in sorted(src_dir.rglob("*.ko")):
                copy_kernel_module(ko)
        if len(copied) == copied_before:
            build_runs.append({"ok": False, "reason": "dependency_dir_no_ko", "dir": rel})
    # Rebuild the OOT module after dependency modules have been built so
    # modpost can use Module.symvers and depmod can infer depends= entries.
    oot_rebuild = build_external(kernel_tree, oot_module_ko.parent, jobs, extra_make_args or [])
    extra = stage / "lib" / "modules" / krel / "extra"
    extra.mkdir(parents=True, exist_ok=True)
    shutil.copy2(oot_module_ko, extra / oot_module_ko.name)
    depmod = run(["depmod", "-b", str(stage), krel], timeout=300)
    modules_root = stage / "lib" / "modules"
    build_ok = all(bool(x.get("ok")) or bool(x.get("nonfatal")) for x in build_runs) if build_runs else True
    ok = build_ok and bool(oot_rebuild.get("ok")) and bool(depmod.get("ok"))
    return {
        "ok": ok,
        "reason": "prepared" if ok else "module_tree_prepare_failed",
        "kernel_release": krel,
        "modules_root": modules_root.as_posix(),
        "dependency_module_dirs": dependency_module_dirs or [],
        "dependency_module_targets": dependency_module_targets or [],
        "copied_dependency_modules": copied,
        "build_dependency_modules": build_runs,
        "oot_rebuild_after_dependency_modules": oot_rebuild,
        "depmod": depmod,
    }


def collect_class_create_evidence(repo: Path, old_ref: str, new_ref: str, out_dir: Path) -> dict[str, Any]:
    paths = ["include/linux/device/class.h", "drivers/base/class.c"]
    evidence: dict[str, Any] = {"symbol": "class_create", "old_ref": old_ref, "new_ref": new_ref, "files": paths}
    evidence["old_grep"] = run(["git", "-C", str(repo), "grep", "-n", "class_create", old_ref, "--", *paths], timeout=120)
    evidence["new_grep"] = run(["git", "-C", str(repo), "grep", "-n", "class_create", new_ref, "--", *paths], timeout=120)
    evidence["definition_diff"] = run(["git", "-C", str(repo), "diff", f"{old_ref}..{new_ref}", "--", *paths], timeout=180)
    write_json(out_dir / "kernel_evidence" / "class_create_evidence.json", evidence)
    write_text(out_dir / "kernel_evidence" / "class_create.diff", evidence["definition_diff"].get("stdout", ""))
    return evidence


def patch_prompt(input_payload: dict[str, Any]) -> str:
    return textwrap.dedent(
        """
        You are patching a small out-of-tree Linux driver for a kernel API migration.

        Boundary:
        - You may use only the old out-of-tree driver source and the old/new kernel-side API evidence in the input.
        - Do not assume any new-version driver source exists.
        - Keep the patch minimal and preserve cleanup/error behavior.
        - Output JSON only.

        Required JSON schema:
        {
          "verdict": "PATCH" | "NO_PATCH" | "UNKNOWN",
          "rationale": "short evidence-grounded explanation",
          "touched_files": ["relative/path.c"],
          "patch": "unified diff with paths relative to the out-of-tree driver root"
        }

        Patch requirements:
        - Use standard unified diff format, preferably git-style paths like a/oot_class_demo.c and b/oot_class_demo.c.
        - Do not edit comments only.
        - Do not modify unrelated logic.
        - If a compiler diagnostic shows a new kernel-side callback/signature/API contract, update the old driver to match it.
        - Preserve error handling and cleanup behavior.
        - In iterative repair, generate an incremental patch against current_driver_sources, not against the original source.

        Input JSON follows:
        """
    ).strip() + "\n\n```json\n" + json.dumps(input_payload, indent=2) + "\n```\n"


def _tail_text(text: str, max_lines: int = 120, max_chars: int = 20000) -> str:
    lines = (text or "").splitlines()
    out = "\n".join(lines[-max_lines:])
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out


def _build_feedback(build: dict[str, Any] | None) -> dict[str, Any]:
    if not build:
        return {}
    return {
        "ok": bool(build.get("ok")),
        "returncode": build.get("returncode"),
        "cmd": build.get("cmd"),
        "stdout_tail": _tail_text(build.get("stdout", "")),
        "stderr_tail": _tail_text(build.get("stderr", "")),
    }


def _patch_feedback(patch_apply: dict[str, Any] | None) -> dict[str, Any]:
    if not patch_apply:
        return {}
    return {
        "ok": bool(patch_apply.get("ok")),
        "reason": patch_apply.get("reason"),
        "method": patch_apply.get("method"),
        "git_apply_check_stderr": _tail_text(((patch_apply.get("git_apply_check") or {}).get("stderr") or ""), 80, 8000),
        "fuzzy_patch_check_stderr": _tail_text(((patch_apply.get("fuzzy_patch_check") or {}).get("stderr") or ""), 80, 8000),
    }


def run_patch_llm(
    run_dir: Path,
    args: argparse.Namespace,
    evidence: dict[str, Any],
    current_sources: list[dict[str, str]],
    *,
    iteration: int,
    new_kernel_build_feedback: dict[str, Any] | None = None,
    previous_iterations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stage_dir = run_dir / "llm_calls" / f"patch_generation_iter_{iteration:02d}"
    input_payload = {
        "case_id": getattr(args, "case_id", "oot_case"),
        "iteration": iteration,
        "max_patch_iterations": args.max_patch_iterations,
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "current_driver_sources": current_sources,
        "kernel_evidence": {
            "symbol": evidence["symbol"],
            "old_grep": evidence["old_grep"].get("stdout", ""),
            "new_grep": evidence["new_grep"].get("stdout", ""),
            "definition_diff_excerpt": evidence["definition_diff"].get("stdout", "")[:12000],
        },
        "new_kernel_build_feedback": new_kernel_build_feedback or {},
        "previous_iterations": previous_iterations or [],
        "instruction": (
            "Generate a minimal out-of-tree driver patch for the new kernel API. "
            "If this is not the first iteration, fix the remaining compile errors without reverting earlier valid fixes."
        ),
    }
    default = {"verdict": "UNKNOWN", "rationale": "LLM not executed", "touched_files": [], "patch": ""}
    client = LLMClient(
        LLMConfig(
            provider=args.llm_provider,
            model=args.llm_model,
            enabled=args.enable_llm,
            config_ini=args.config_ini,
            timeout_sec=args.llm_timeout_sec,
            max_retries=args.llm_max_retries,
            max_tokens=args.llm_max_tokens,
        )
    )
    return client.call_json(
        stage_dir=stage_dir,
        prompt_text=patch_prompt(input_payload),
        input_payload=input_payload,
        dry_run=args.dry_run,
        default_output=default,
    )


def apply_generated_patch(run_dir: Path, patched_dir: Path, patch_text: str, *, iteration: int | None = None) -> dict[str, Any]:
    iter_dir = run_dir if iteration is None else run_dir / "patch_iterations" / f"iter_{iteration:02d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    patch_path = iter_dir / "generated_patch.diff"
    write_text(patch_path, patch_text or "")
    write_text(run_dir / "generated_patch.diff", patch_text or "")
    if not patch_text.strip():
        return {"ok": False, "reason": "empty_patch", "patch_path": str(patch_path)}
    git_dir = patched_dir / ".git"
    if not git_dir.exists():
        run(["git", "init"], cwd=patched_dir, timeout=120)
        run(["git", "add", "."], cwd=patched_dir, timeout=120)
        run(["git", "commit", "-m", "old oot driver"], cwd=patched_dir, timeout=120)
    check = run(["git", "apply", "--check", str(patch_path)], cwd=patched_dir, timeout=120)
    if check["returncode"] != 0:
        # LLMs often produce correct hunks with stale line numbers or slightly
        # stale context. GNU patch gives us the same kind of conservative fuzzy
        # repair used by the main Driver-Agent line-fix stage.
        dry = run(["patch", "-p1", "--dry-run", "--forward", "--batch", "-i", str(patch_path)], cwd=patched_dir, timeout=120)
        if dry["returncode"] != 0:
            return {
                "ok": False,
                "reason": "patch_apply_check_failed",
                "patch_path": str(patch_path),
                "git_apply_check": check,
                "fuzzy_patch_check": dry,
            }
        apply_res = run(["patch", "-p1", "--forward", "--batch", "--no-backup-if-mismatch", "-i", str(patch_path)], cwd=patched_dir, timeout=120)
        method = "patch_fuzzy"
    else:
        apply_res = run(["git", "apply", str(patch_path)], cwd=patched_dir, timeout=120)
        method = "git_apply"
    diff = run(["git", "diff", "--", "."], cwd=patched_dir, timeout=120)
    write_text(iter_dir / "applied_driver.diff", diff.get("stdout", ""))
    write_text(run_dir / "applied_driver.diff", diff.get("stdout", ""))
    return {
        "ok": apply_res["returncode"] == 0,
        "reason": "applied" if apply_res["returncode"] == 0 else "apply_failed",
        "method": method,
        "iteration": iteration,
        "patch_path": str(patch_path),
        "apply": apply_res,
        "applied_diff_path": str(iter_dir / "applied_driver.diff"),
        "cumulative_applied_diff_path": str(run_dir / "applied_driver.diff"),
    }


def make_initramfs(
    run_dir: Path,
    module_ko: Path,
    *,
    module_name: str,
    device_node: str,
    write_data: str,
    expect_data: str,
    qemu_check_script: Path | None = None,
    modules_root: Path | None = None,
) -> Path:
    root = run_dir / "initramfs_root"
    if root.exists():
        shutil.rmtree(root)
    for d in ["bin", "sbin", "proc", "sys", "dev", "lib/modules"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.environ.get("DRIVER_AGENT_BUSYBOX", "/usr/bin/busybox"), root / "bin" / "busybox")
    shutil.copy2(module_ko, root / module_ko.name)
    if modules_root and modules_root.exists():
        shutil.copytree(modules_root, root / "lib" / "modules", dirs_exist_ok=True, symlinks=True)
    for app in [
        "sh",
        "mount",
        "cat",
        "grep",
        "awk",
        "mknod",
        "insmod",
        "modprobe",
        "rmmod",
        "poweroff",
        "echo",
        "sleep",
        "find",
        "test",
        "ls",
        "head",
        "tail",
        "wc",
        "cut",
        "sort",
        "uniq",
        "printf",
        "dmesg",
        "uname",
    ]:
        target = root / "bin" / app
        if not target.exists():
            target.symlink_to("busybox")
    if qemu_check_script:
        check_body = qemu_check_script.read_text(errors="replace")
        init = f"""#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
export DRIVER_AGENT_MODULE_KO=/{module_ko.name}
export DRIVER_AGENT_MODULE_NAME={module_name}
driver_agent_load_module() {{
  if [ -d /lib/modules/$(uname -r) ]; then
    /bin/modprobe "$DRIVER_AGENT_MODULE_NAME" "$@" && return 0
  fi
  /bin/insmod "$DRIVER_AGENT_MODULE_KO" "$@"
}}
driver_agent_unload_module() {{
  /bin/rmmod "$DRIVER_AGENT_MODULE_NAME"
}}
{check_body}
"""
    else:
        init = f"""#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
if [ -d /lib/modules/$(uname -r) ]; then
  /bin/modprobe {module_name} || {{ echo DRIVER_AGENT_MODPROBE_FAIL; /bin/poweroff -f; }}
else
  /bin/insmod /{module_ko.name} || {{ echo DRIVER_AGENT_INSMOD_FAIL; /bin/poweroff -f; }}
fi
if [ ! -e {device_node} ]; then
  echo DRIVER_AGENT_DEVICE_NODE_MISSING {device_node}
  /bin/poweroff -f
fi
exec 3<> {device_node} || {{ echo DRIVER_AGENT_OPEN_FAIL; /bin/poweroff -f; }}
echo -n {write_data!r} >&3 || {{ echo DRIVER_AGENT_WRITE_FAIL; /bin/poweroff -f; }}
cat <&3 | grep {expect_data!r} || {{ echo DRIVER_AGENT_READBACK_FAIL; /bin/poweroff -f; }}
exec 3>&-
/bin/rmmod {module_name} || {{ echo DRIVER_AGENT_RMMOD_FAIL; /bin/poweroff -f; }}
echo DRIVER_AGENT_QEMU_SMOKE_PASS
poweroff -f
"""
    write_text(root / "init", init)
    os.chmod(root / "init", 0o755)
    initrd = run_dir / "initramfs.cpio.gz"
    cmd = f"find . -print0 | cpio --null -ov --format=newc | gzip -9 > {initrd}"
    res = subprocess.run(cmd, cwd=str(root), shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"initramfs build failed\nstdout={res.stdout}\nstderr={res.stderr}")
    return initrd


def run_qemu_smoke(
    run_dir: Path,
    new_tree: Path,
    module_ko: Path,
    jobs: int,
    timeout_sec: int,
    *,
    module_name: str,
    device_node: str,
    write_data: str,
    expect_data: str,
    qemu_check_script: Path | None = None,
    dependency_module_dirs: list[str] | None = None,
    dependency_module_targets: list[str] | None = None,
    make_extra_args: list[str] | None = None,
) -> dict[str, Any]:
    bz = new_tree / "arch" / "x86" / "boot" / "bzImage"
    build_kernel: dict[str, Any] | None = None
    if not bz.exists():
        build_kernel = run(["make", f"-j{jobs}", "bzImage"], cwd=new_tree, timeout=7200)
        if build_kernel["returncode"] != 0:
            return {"ok": False, "reason": "bzImage_build_failed", "build_kernel": build_kernel}
    module_tree = prepare_qemu_module_tree(
        run_dir,
        new_tree,
        module_ko,
        jobs,
        dependency_module_dirs=dependency_module_dirs or [],
        dependency_module_targets=dependency_module_targets or [],
        extra_make_args=make_extra_args or [],
    )
    if not module_tree.get("ok"):
        return {
            "ok": False,
            "reason": module_tree.get("reason") or "module_tree_prepare_failed",
            "build_kernel": build_kernel,
            "module_tree": module_tree,
        }
    initrd = make_initramfs(
        run_dir,
        module_ko,
        module_name=module_name,
        device_node=device_node,
        write_data=write_data,
        expect_data=expect_data,
        qemu_check_script=qemu_check_script,
        modules_root=Path(module_tree["modules_root"]) if module_tree.get("modules_root") else None,
    )
    qemu_cmd = [
        "qemu-system-x86_64",
        "-nodefaults",
        "-serial",
        "mon:stdio",
        "-nographic",
        "-no-reboot",
        "-m",
        "512M",
        "-kernel",
        str(bz),
        "-initrd",
        str(initrd),
        "-append",
        "console=ttyS0 panic=-1 quiet",
    ]
    res = run(qemu_cmd, timeout=timeout_sec)
    text = (res.get("stdout", "") or "") + "\n" + (res.get("stderr", "") or "")
    res["success_marker"] = "DRIVER_AGENT_QEMU_SMOKE_PASS" in text
    res["ok"] = res["success_marker"]
    return {
        "ok": res["ok"],
        "reason": "success" if res["ok"] else "success_marker_missing",
        "build_kernel": build_kernel,
        "module_tree": module_tree,
        "qemu": res,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a small OOT Linux driver migration case with optional QEMU smoke validation")
    ap.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    ap.add_argument("--old-ref", default="v6.1")
    ap.add_argument("--new-ref", default="v7.0-rc1")
    ap.add_argument("--case-dir", default=str(ROOT / "datasets/oot_qemu_cases/class_create_char_v6_1_to_v7_0"))
    ap.add_argument("--external-repo-url", default=None, help="optional real OOT git repository URL")
    ap.add_argument("--external-repo-ref", default=None, help="commit/ref to export from the real OOT repository")
    ap.add_argument("--external-source-subdir", default=None, help="source subdirectory inside the real OOT repository")
    ap.add_argument("--out-dir", default=str(ROOT / "runs/oot_qemu"))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--enable-llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--llm-provider", default="openrouter")
    ap.add_argument("--llm-model", default="anthropic/claude-sonnet-4.6")
    ap.add_argument("--config-ini", default=str(ROOT / "config.ini"))
    ap.add_argument("--llm-timeout-sec", type=int, default=300)
    ap.add_argument("--llm-max-tokens", type=int, default=10000)
    ap.add_argument("--llm-max-retries", type=int, default=2)
    ap.add_argument("--max-patch-iterations", type=int, default=1, help="max LLM patch/build repair iterations")
    ap.add_argument("--enable-qemu", action="store_true")
    ap.add_argument("--qemu-timeout-sec", type=int, default=900)
    ap.add_argument("--module-ko", default="oot_class_demo.ko")
    ap.add_argument("--module-name", default="oot_class_demo")
    ap.add_argument("--device-node", default="/dev/oot_class_demo")
    ap.add_argument("--qemu-write-data", default="qemu-ok")
    ap.add_argument("--qemu-expect-data", default="qemu-ok")
    ap.add_argument(
        "--qemu-check-script",
        default=None,
        help="optional shell script body to run as init after proc/sys/dev mounts; receives DRIVER_AGENT_MODULE_KO and DRIVER_AGENT_MODULE_NAME",
    )
    ap.add_argument(
        "--make-extra-arg",
        action="append",
        default=[],
        help="extra make argument for OOT module builds, e.g. KCFLAGS=-Wno-error=type-limits",
    )
    ap.add_argument(
        "--kernel-config",
        action="append",
        default=[],
        help="kernel config option needed by the OOT module, e.g. MEDIA_SUPPORT=m or CONFIG_VIDEO_DEV=m",
    )
    ap.add_argument(
        "--kernel-module-dir",
        action="append",
        default=[],
        help="in-tree kernel module directory to build/copy into QEMU initramfs, e.g. drivers/media/v4l2-core",
    )
    ap.add_argument(
        "--kernel-module",
        action="append",
        default=[],
        help="specific in-tree .ko target to build/copy into QEMU initramfs, e.g. drivers/media/v4l2-core/videodev.ko",
    )
    ap.add_argument("--keep-worktrees", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    case_dir = Path(args.case_dir)
    external_source = None
    if args.external_repo_url:
        if not args.external_repo_ref or not args.external_source_subdir:
            raise ValueError("--external-repo-url requires --external-repo-ref and --external-source-subdir")
        old_driver = None
        case_slug = args.external_repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
    else:
        old_driver = case_dir / "old_driver"
        if not old_driver.exists():
            raise FileNotFoundError(old_driver)
        case_slug = "oot_class_create_char"

    run_dir = Path(args.out_dir) / f"{case_slug}__{args.old_ref}__{args.new_ref}" / now_timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    started = time.time()

    metadata = {
        "case_id": f"{case_slug}__{args.old_ref}__{args.new_ref}",
        "case_type": "real_out_of_tree_qemu_char_driver" if args.external_repo_url else "controlled_out_of_tree_qemu_char_driver",
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "repo": str(repo),
        "external_repo_url": args.external_repo_url,
        "external_repo_ref": args.external_repo_ref,
        "external_source_subdir": args.external_source_subdir,
        "input_boundary": "old OOT driver source + old/new kernel-side API evidence only",
        "run_dir": str(run_dir),
    }
    args.case_id = metadata["case_id"]
    write_json(run_dir / "metadata.json", metadata)

    source_old = run_dir / "source_old"
    source_patched = run_dir / "source_patched"
    if args.external_repo_url:
        external_source = export_external_source(
            repo_url=args.external_repo_url,
            ref=args.external_repo_ref,
            source_subdir=args.external_source_subdir,
            dest=source_old,
            cache_root=ROOT / "external_repos",
        )
        copytree(source_old, source_patched)
    else:
        assert old_driver is not None
        copytree(old_driver, source_old)
        copytree(old_driver, source_patched)
    old_sources_for_llm = list_sources(source_old)

    t = time.time()
    evidence = collect_class_create_evidence(repo, args.old_ref, args.new_ref, run_dir)
    timings["kernel_evidence_sec"] = round(time.time() - t, 3)

    worktrees = run_dir / "worktrees"
    old_tree = worktrees / "old"
    new_tree = worktrees / "new"
    t = time.time()
    wt_old = prepare_worktree(repo, args.old_ref, old_tree)
    wt_new = prepare_worktree(repo, args.new_ref, new_tree)
    timings["worktree_prepare_sec"] = round(time.time() - t, 3)

    t = time.time()
    old_prep = prepare_kernel(old_tree, args.jobs, config_options=args.kernel_config or [])
    new_prep = prepare_kernel(new_tree, args.jobs, config_options=args.kernel_config or [])
    timings["kernel_prepare_sec"] = round(time.time() - t, 3)

    t = time.time()
    old_build = build_external(old_tree, source_old, args.jobs, args.make_extra_arg)
    new_pre_build = build_external(new_tree, source_old, args.jobs, args.make_extra_arg)
    timings["pre_patch_builds_sec"] = round(time.time() - t, 3)

    patch_iterations: list[dict[str, Any]] = []
    llm_out: dict[str, Any] = {"verdict": "UNKNOWN", "patch": "", "rationale": "no iterations executed"}
    patch_apply: dict[str, Any] = {"ok": False, "reason": "not_attempted"}
    post_build: dict[str, Any] = {"ok": False, "reason": "patch_not_applied"}
    feedback_build = new_pre_build
    t = time.time()
    max_iters = max(1, int(args.max_patch_iterations or 1))
    for iteration in range(1, max_iters + 1):
        iter_started = time.time()
        current_sources = old_sources_for_llm if iteration == 1 else list_sources(source_patched)
        llm_out = run_patch_llm(
            run_dir,
            args,
            evidence,
            current_sources,
            iteration=iteration,
            new_kernel_build_feedback=_build_feedback(feedback_build),
            previous_iterations=patch_iterations,
        )
        patch_apply = apply_generated_patch(run_dir, source_patched, llm_out.get("patch", ""), iteration=iteration)
        post_build = {"ok": False, "reason": "patch_not_applied"}
        if patch_apply.get("ok"):
            post_build = build_external(new_tree, source_patched, args.jobs, args.make_extra_arg)
        iteration_record = {
            "iteration": iteration,
            "duration_sec": round(time.time() - iter_started, 3),
            "llm": {
                "verdict": llm_out.get("verdict"),
                "rationale": llm_out.get("rationale"),
                "touched_files": llm_out.get("touched_files", []),
                "parse_status": llm_out.get("parse_status"),
            },
            "patch_apply": _patch_feedback(patch_apply),
            "patched_build": _build_feedback(post_build),
        }
        patch_iterations.append(iteration_record)
        write_json(run_dir / "patch_iterations" / f"iter_{iteration:02d}" / "iteration_result.json", iteration_record)
        if post_build.get("ok"):
            break
        feedback_build = post_build if patch_apply.get("ok") else {"ok": False, "stderr": json.dumps(_patch_feedback(patch_apply), indent=2), "stdout": "", "returncode": 1}
    timings["iterative_patch_repair_sec"] = round(time.time() - t, 3)

    qemu = {"ok": False, "skipped": True, "reason": "qemu disabled"}
    if args.enable_qemu and post_build.get("ok"):
        ko = source_patched / args.module_ko
        t = time.time()
        qemu = run_qemu_smoke(
            run_dir,
            new_tree,
            ko,
            args.jobs,
            args.qemu_timeout_sec,
            module_name=args.module_name,
            device_node=args.device_node,
            write_data=args.qemu_write_data,
            expect_data=args.qemu_expect_data,
            qemu_check_script=Path(args.qemu_check_script) if args.qemu_check_script else None,
            dependency_module_dirs=args.kernel_module_dir or [],
            dependency_module_targets=args.kernel_module or [],
            make_extra_args=args.make_extra_arg or [],
        )
        qemu["skipped"] = False
        timings["qemu_smoke_sec"] = round(time.time() - t, 3)

    timings["total_sec"] = round(time.time() - started, 3)
    result = {
        "metadata": metadata,
        "timings": timings,
        "worktree_prepare": {"old": wt_old, "new": wt_new},
        "kernel_prepare": {"old": old_prep, "new": new_prep},
        "builds": {
            "old_driver_on_old_kernel": old_build,
            "old_driver_on_new_kernel_pre_patch": new_pre_build,
            "patched_driver_on_new_kernel": post_build,
        },
        "llm_patch_generation": llm_out,
        "patch_iterations": patch_iterations,
        "selected_iteration": next((it["iteration"] for it in patch_iterations if (it.get("patched_build") or {}).get("ok")), patch_iterations[-1]["iteration"] if patch_iterations else None),
        "patch_apply": patch_apply,
        "qemu_smoke": qemu,
        "external_source": external_source,
        "verdict": {
            "old_build_passes": bool(old_build.get("ok")),
            "old_source_fails_on_new_kernel": not bool(new_pre_build.get("ok")),
            "patch_applies": bool(patch_apply.get("ok")),
            "patched_build_passes_new_kernel": bool(post_build.get("ok")),
            "qemu_smoke_passes": bool(qemu.get("ok")) if args.enable_qemu else None,
        },
    }
    result["verdict"]["case_study_success"] = (
        result["verdict"]["old_build_passes"]
        and result["verdict"]["old_source_fails_on_new_kernel"]
        and result["verdict"]["patch_applies"]
        and result["verdict"]["patched_build_passes_new_kernel"]
        and (not args.enable_qemu or result["verdict"]["qemu_smoke_passes"])
    )
    write_json(run_dir / "result.json", result)

    md = [
        "# OOT QEMU Case Result",
        "",
        f"- Run: `{run_dir}`",
        f"- Old ref: `{args.old_ref}`",
        f"- New ref: `{args.new_ref}`",
        f"- Old build passes: {result['verdict']['old_build_passes']}",
        f"- Old source fails on new kernel: {result['verdict']['old_source_fails_on_new_kernel']}",
        f"- Patch applies: {result['verdict']['patch_applies']}",
        f"- Patched build passes new kernel: {result['verdict']['patched_build_passes_new_kernel']}",
        f"- QEMU smoke passes: {result['verdict']['qemu_smoke_passes']}",
        f"- Case study success: {result['verdict']['case_study_success']}",
        "",
        "## Timing Seconds",
        "",
    ]
    for k, v in timings.items():
        md.append(f"- {k}: {v}")
    write_text(run_dir / "result.md", "\n".join(md) + "\n")

    print(f"[oot-case] completed: {run_dir}")
    for key, val in result["verdict"].items():
        print(f"[oot-case] {key}: {val}")
    return 0 if result["verdict"]["case_study_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
