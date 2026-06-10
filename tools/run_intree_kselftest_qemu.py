#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


TARGETS: dict[str, dict[str, Any]] = {
    "drivers-dma-buf": {
        "selftest_dir": "drivers/dma-buf",
        "configs": [
            "CONFIG_BLK_DEV_INITRD=y",
            "CONFIG_DEVTMPFS=y",
            "CONFIG_DEVTMPFS_MOUNT=y",
            "CONFIG_PROC_FS=y",
            "CONFIG_SYSFS=y",
            "CONFIG_TMPFS=y",
            "CONFIG_BINFMT_ELF=y",
            "CONFIG_MEMFD_CREATE=y",
            "CONFIG_UDMABUF=y",
        ],
        "run": "/kselftest/drivers/dma-buf/udmabuf",
        "description": "drivers/dma-buf udmabuf userspace kselftest in an isolated QEMU guest.",
    },
    "dmabuf-heaps": {
        "selftest_dir": "dmabuf-heaps",
        "configs": [
            "CONFIG_BLK_DEV_INITRD=y",
            "CONFIG_DEVTMPFS=y",
            "CONFIG_DEVTMPFS_MOUNT=y",
            "CONFIG_PROC_FS=y",
            "CONFIG_SYSFS=y",
            "CONFIG_TMPFS=y",
            "CONFIG_BINFMT_ELF=y",
            "CONFIG_EXPERT=y",
            "CONFIG_DMABUF_HEAPS=y",
            "CONFIG_DMABUF_HEAPS_SYSTEM=y",
            "CONFIG_DRM=y",
            "CONFIG_DRM_VGEM=y",
        ],
        "run": "/kselftest/dmabuf-heaps/dmabuf-heap",
        "description": "DMA-BUF heap userspace kselftest in an isolated QEMU guest.",
    },
    "zram": {
        "selftest_dir": "zram",
        "configs": [
            "CONFIG_BLK_DEV_INITRD=y",
            "CONFIG_DEVTMPFS=y",
            "CONFIG_DEVTMPFS_MOUNT=y",
            "CONFIG_PROC_FS=y",
            "CONFIG_SYSFS=y",
            "CONFIG_TMPFS=y",
            "CONFIG_BINFMT_ELF=y",
            "CONFIG_SWAP=y",
            "CONFIG_ZSMALLOC=y",
            "CONFIG_ZRAM=y",
        ],
        "run": "sh /kselftest/zram/zram.sh",
        "description": "zram kselftest in an isolated QEMU guest.",
        # zram.sh needs a few GNU/userland tools that BusyBox either lacks or
        # implements without required options (for example seq -w and dd
        # oflag=append/status=none). Copy them into the initramfs so QEMU
        # results reflect zram behavior instead of test-environment gaps.
        "host_binaries": ["bash", "mkfs.ext2", "seq", "dd", "bc"],
    },
}


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> dict[str, Any]:
    start = time.time()
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return str(value)

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
            "stdout": _text(exc.stdout),
            "stderr": _text(exc.stderr) + f"\n[timeout] after {timeout} sec",
            "duration_sec": round(time.time() - start, 3),
            "ok": False,
        }


def truncate_run(res: dict[str, Any], max_chars: int = 24000) -> dict[str, Any]:
    out = dict(res)
    for key in ("stdout", "stderr"):
        text = out.get(key) or ""
        if len(text) > max_chars:
            out[key] = text[:max_chars] + "\n...[truncated]"
    return out


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_host_binary_with_libs(root: Path, binary_name: str) -> dict[str, Any]:
    src = shutil.which(binary_name)
    if not src:
        return {"ok": False, "binary": binary_name, "reason": "not_found_in_host_path"}
    src_path = Path(src).resolve()
    dst = root / "bin" / binary_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dst)
    os.chmod(dst, 0o755)

    copied_libs: list[str] = []
    ldd = run(["ldd", str(src_path)], timeout=60)
    if ldd.get("ok"):
        text = (ldd.get("stdout") or "") + "\n" + (ldd.get("stderr") or "")
        for lib in sorted(set(re.findall(r"(/[^\s]+)", text))):
            lib_path = Path(lib)
            if not lib_path.exists() or not lib_path.is_file():
                continue
            lib_dst = root / lib_path.relative_to("/")
            lib_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib_path, lib_dst)
            copied_libs.append(str(lib_path))
    return {"ok": True, "binary": binary_name, "src": str(src_path), "dst": str(dst), "copied_libs": copied_libs}


def print_summary(result: dict[str, Any]) -> None:
    qemu = result.get("qemu") if isinstance(result.get("qemu"), dict) else {}
    summary = {
        "target": result.get("target"),
        "status": result.get("status"),
        "result_path": str(Path(result.get("out_dir", "")) / "result.json") if result.get("out_dir") else "",
        "qemu_output_path": str(Path(result.get("out_dir", "")) / "qemu_output.txt") if result.get("out_dir") else "",
        "qemu_ok": bool(qemu.get("ok")),
        "success_marker": bool(qemu.get("success_marker")),
        "failure_marker": bool(qemu.get("failure_marker")),
        "error": result.get("error", ""),
    }
    print(json.dumps(summary, indent=2))


def append_config(kernel_tree: Path, configs: list[str]) -> dict[str, Any]:
    config_path = kernel_tree / ".config"
    with config_path.open("a", encoding="utf-8") as f:
        f.write("\n# Driver-Agent kselftest QEMU config\n")
        for line in configs:
            f.write(line + "\n")
    return {"ok": True, "path": str(config_path), "configs": configs}


def verify_config(kernel_tree: Path, configs: list[str]) -> dict[str, Any]:
    text = (kernel_tree / ".config").read_text(errors="replace") if (kernel_tree / ".config").exists() else ""
    missing = []
    for cfg in configs:
        if cfg.startswith("CONFIG_") and cfg not in text:
            missing.append(cfg)
    return {"ok": not missing, "missing": missing}


def build_kernel_and_selftest(kernel_tree: Path, target: str, spec: dict[str, Any], out_dir: Path, jobs: int) -> tuple[bool, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    runs.append(run(["make", "ARCH=x86_64", "mrproper"], cwd=kernel_tree, timeout=900))
    if not runs[-1]["ok"]:
        return False, runs
    runs.append(run(["make", "ARCH=x86_64", "defconfig"], cwd=kernel_tree, timeout=1200))
    if not runs[-1]["ok"]:
        return False, runs
    runs.append(append_config(kernel_tree, spec["configs"]))
    runs.append(run(["make", "ARCH=x86_64", "olddefconfig"], cwd=kernel_tree, timeout=1200))
    if not runs[-1]["ok"]:
        return False, runs
    verify = verify_config(kernel_tree, spec["configs"])
    runs.append({"cmd": ["verify_config"], "ok": verify["ok"], "returncode": 0 if verify["ok"] else 1, "stdout": json.dumps(verify), "stderr": "", "duration_sec": 0})
    if not verify["ok"]:
        return False, runs
    runs.append(run(["make", f"-j{jobs}", "ARCH=x86_64", "bzImage"], cwd=kernel_tree, timeout=7200))
    if not runs[-1]["ok"]:
        return False, runs
    runs.append(run(["make", f"-j{jobs}", "ARCH=x86_64", "headers"], cwd=kernel_tree, timeout=1800))
    if not runs[-1]["ok"]:
        return False, runs
    runs.append(run(["make", "-C", "tools/testing/selftests", f"TARGETS={spec['selftest_dir']}", f"-j{jobs}"], cwd=kernel_tree, timeout=2400))
    if not runs[-1]["ok"]:
        return False, runs
    install_dir = out_dir / "kselftest_install"
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    runs.append(run(["make", "-C", "tools/testing/selftests", f"TARGETS={spec['selftest_dir']}", f"INSTALL_PATH={install_dir}", "install"], cwd=kernel_tree, timeout=2400))
    if not runs[-1]["ok"]:
        return False, runs
    return True, runs


def make_initramfs(out_dir: Path, install_dir: Path, spec: dict[str, Any]) -> Path:
    root = out_dir / "initramfs_root"
    if root.exists():
        shutil.rmtree(root)
    for d in ["bin", "sbin", "proc", "sys", "dev", "tmp", "kselftest"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.environ.get("DRIVER_AGENT_BUSYBOX", "/usr/bin/busybox"), root / "bin" / "busybox")
    host_binary_results = [
        copy_host_binary_with_libs(root, name)
        for name in (spec.get("host_binaries") or [])
    ]
    for app in [
        "sh",
        "bash",
        "mount",
        "umount",
        "cat",
        "cut",
        "grep",
        "awk",
        "mknod",
        "poweroff",
        "rm",
        "echo",
        "sleep",
        "find",
        "test",
        "ls",
        "printf",
        "dmesg",
        "uname",
        "mkdir",
        "chmod",
        "id",
        "seq",
        "wc",
        "which",
        "modprobe",
        "rmmod",
        "swapon",
        "swapoff",
        "mkswap",
    ]:
        target = root / "bin" / app
        if not target.exists():
            target.symlink_to("busybox")
    src = install_dir / spec["selftest_dir"]
    dst = root / "kselftest" / spec["selftest_dir"]
    shutil.copytree(src, dst)
    init = f"""#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t tmpfs tmpfs /tmp 2>/dev/null || true
export PATH=/bin:/sbin:/kselftest/{spec['selftest_dir']}:$PATH
cd /kselftest/{spec['selftest_dir']} || {{ echo DRIVER_AGENT_KSELFTEST_CD_FAIL; poweroff -f; }}
echo DRIVER_AGENT_KSELFTEST_BEGIN {spec['selftest_dir']}
{spec['run']}
rc=$?
echo DRIVER_AGENT_KSELFTEST_EXIT $rc
if [ "$rc" = "0" ]; then
  echo DRIVER_AGENT_QEMU_KSELFTEST_PASS
else
  echo DRIVER_AGENT_QEMU_KSELFTEST_FAIL
fi
poweroff -f
"""
    if host_binary_results:
        init_lines = init.splitlines()
        init_lines.insert(1, "echo DRIVER_AGENT_HOST_BINARIES " + json.dumps(host_binary_results).replace("'", "'\"'\"'"))
        init = "\n".join(init_lines) + "\n"
    write_text(root / "init", init)
    os.chmod(root / "init", 0o755)
    initrd = out_dir / "initramfs.cpio.gz"
    cmd = f"find . -print0 | cpio --null -ov --format=newc | gzip -9 > {initrd}"
    proc = subprocess.run(cmd, cwd=str(root), shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"initramfs build failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return initrd


def run_qemu(kernel_tree: Path, initrd: Path, out_dir: Path, timeout_sec: int) -> dict[str, Any]:
    bz = kernel_tree / "arch/x86/boot/bzImage"
    cmd = [
        "qemu-system-x86_64",
        "-nodefaults",
        "-serial", "mon:stdio",
        "-nographic",
        "-no-reboot",
        "-m", "1G",
        "-kernel", str(bz),
        "-initrd", str(initrd),
        "-append", "console=ttyS0 panic=-1 quiet",
    ]
    res = run(cmd, cwd=kernel_tree, timeout=timeout_sec)
    text = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    res["success_marker"] = "DRIVER_AGENT_QEMU_KSELFTEST_PASS" in text
    failure_lines = [
        line
        for line in text.splitlines()
        if re.search(r"(^|\s)(FAIL|not ok|\[FAIL\])(\s|:|$)", line)
    ]
    res["failure_marker"] = "DRIVER_AGENT_QEMU_KSELFTEST_FAIL" in text or bool(failure_lines)
    res["failure_lines"] = failure_lines[:20]
    res["ok"] = bool(res["success_marker"]) and not bool(res["failure_marker"])
    write_text(out_dir / "qemu_output.txt", text)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an in-tree Linux kselftest target inside an isolated QEMU initramfs")
    ap.add_argument("--kernel-tree", required=True)
    ap.add_argument("--target", required=True, choices=sorted(TARGETS))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout-sec", type=int, default=900)
    args = ap.parse_args()

    kernel_tree = Path(args.kernel_tree).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = TARGETS[args.target]
    result: dict[str, Any] = {"target": args.target, "kernel_tree": str(kernel_tree), "out_dir": str(out_dir), "spec": spec, "status": "unknown", "steps": []}

    try:
        ok, steps = build_kernel_and_selftest(kernel_tree, args.target, spec, out_dir, args.jobs)
        result["steps"] = [truncate_run(s) for s in steps]
        if not ok:
            result["status"] = "build_failed"
            write_json(out_dir / "result.json", result)
            print_summary(result)
            return 2
        initrd = make_initramfs(out_dir, out_dir / "kselftest_install", spec)
        qemu = run_qemu(kernel_tree, initrd, out_dir, args.timeout_sec)
        result["qemu"] = truncate_run(qemu)
        result["status"] = "passed" if qemu.get("ok") else "qemu_failed"
        write_json(out_dir / "result.json", result)
        print_summary(result)
        return 0 if qemu.get("ok") else 3
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        write_json(out_dir / "result.json", result)
        print_summary(result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
