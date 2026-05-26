#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CONFIGS = [
    "CONFIG_BLK_DEV_INITRD=y",
    "CONFIG_DEVTMPFS=y",
    "CONFIG_DEVTMPFS_MOUNT=y",
    "CONFIG_PROC_FS=y",
    "CONFIG_SYSFS=y",
    "CONFIG_TMPFS=y",
    "CONFIG_CONFIGFS_FS=y",
    "CONFIG_BINFMT_ELF=y",
    "CONFIG_EXPERT=y",
    "CONFIG_IIO=y",
    "CONFIG_IIO_CONFIGFS=y",
    "CONFIG_IIO_SW_DEVICE=y",
    "CONFIG_IIO_SIMPLE_DUMMY=y",
    # Keep the runtime smoke focused on software-device creation. Event/buffer
    # paths are valuable but can require IRQ simulation details that are noisy in
    # a tiny QEMU initramfs and are not needed to prove basic dummy-device probe.
    "CONFIG_IIO_SIMPLE_DUMMY_EVENTS=n",
    "CONFIG_IIO_SIMPLE_DUMMY_BUFFER=n",
]


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> dict[str, Any]:
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "duration_sec": round(time.time() - start, 3), "ok": proc.returncode == 0}
    except subprocess.TimeoutExpired as exc:
        return {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": -1, "stdout": exc.stdout or "", "stderr": (exc.stderr or "") + f"\n[timeout] after {timeout} sec", "duration_sec": round(time.time() - start, 3), "ok": False}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def trunc(res: dict[str, Any], max_chars: int = 24000) -> dict[str, Any]:
    out = dict(res)
    for key in ("stdout", "stderr"):
        text = out.get(key) or ""
        if len(text) > max_chars:
            out[key] = text[:max_chars] + "\n...[truncated]"
    return out


def append_config(kernel_tree: Path) -> dict[str, Any]:
    with (kernel_tree / ".config").open("a", encoding="utf-8") as f:
        f.write("\n# Driver-Agent IIO dummy QEMU smoke config\n")
        for cfg in CONFIGS:
            f.write(cfg + "\n")
    return {"cmd": ["append_config"], "ok": True, "returncode": 0, "stdout": "\n".join(CONFIGS), "stderr": "", "duration_sec": 0}


def verify_config(kernel_tree: Path) -> dict[str, Any]:
    text = (kernel_tree / ".config").read_text(errors="replace") if (kernel_tree / ".config").exists() else ""
    missing = []
    for cfg in CONFIGS:
        if not cfg.startswith("CONFIG_"):
            continue
        if cfg.endswith("=n"):
            name = cfg.split("=", 1)[0]
            if cfg not in text and f"# {name} is not set" not in text:
                missing.append(cfg)
        elif cfg not in text:
            missing.append(cfg)
    return {"cmd": ["verify_config"], "ok": not missing, "returncode": 0 if not missing else 1, "stdout": json.dumps({"missing": missing}, indent=2), "stderr": "", "duration_sec": 0}


def build_kernel(kernel_tree: Path, jobs: int) -> tuple[bool, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    for cmd, timeout in [
        (["make", "ARCH=x86_64", "mrproper"], 900),
        (["make", "ARCH=x86_64", "defconfig"], 1200),
    ]:
        steps.append(run(cmd, cwd=kernel_tree, timeout=timeout))
        if not steps[-1]["ok"]:
            return False, steps
    steps.append(append_config(kernel_tree))
    steps.append(run(["make", "ARCH=x86_64", "olddefconfig"], cwd=kernel_tree, timeout=1200))
    if not steps[-1]["ok"]:
        return False, steps
    steps.append(verify_config(kernel_tree))
    if not steps[-1]["ok"]:
        return False, steps
    steps.append(run(["make", f"-j{jobs}", "ARCH=x86_64", "bzImage"], cwd=kernel_tree, timeout=7200))
    return bool(steps[-1]["ok"]), steps


def make_initramfs(out_dir: Path) -> Path:
    root = out_dir / "initramfs_root"
    if root.exists():
        shutil.rmtree(root)
    for d in ["bin", "sbin", "proc", "sys", "dev", "tmp"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2("/usr/bin/busybox", root / "bin" / "busybox")
    for app in ["sh", "mount", "cat", "grep", "awk", "mknod", "poweroff", "echo", "sleep", "find", "test", "ls", "printf", "dmesg", "uname", "mkdir", "rmdir", "head", "wc"]:
        target = root / "bin" / app
        if not target.exists():
            target.symlink_to("busybox")
    init = """#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t tmpfs tmpfs /tmp 2>/dev/null || true
mount -t configfs configfs /sys/kernel/config 2>/dev/null || true
export PATH=/bin:/sbin:$PATH
echo DRIVER_AGENT_IIO_DUMMY_BEGIN
if [ ! -d /sys/kernel/config/iio/devices/dummy ]; then
  echo DRIVER_AGENT_IIO_DUMMY_FAIL no_configfs_dummy_dir
  dmesg | tail -80
  poweroff -f
fi
mkdir /sys/kernel/config/iio/devices/dummy/driver_agent_dummy0 2>/tmp/create.err
rc=$?
cat /tmp/create.err 2>/dev/null || true
if [ "$rc" != "0" ]; then
  echo DRIVER_AGENT_IIO_DUMMY_FAIL create_dummy_failed rc=$rc
  dmesg | tail -80
  poweroff -f
fi
sleep 1
DEV=$(find /sys/bus/iio/devices -maxdepth 1 -name 'iio:device*' | head -1)
echo DRIVER_AGENT_IIO_DUMMY_DEVICE $DEV
if [ -z "$DEV" ]; then
  echo DRIVER_AGENT_IIO_DUMMY_FAIL no_iio_device
  find /sys/bus/iio/devices -maxdepth 2 -print 2>/dev/null | head -80
  poweroff -f
fi
if [ -f "$DEV/name" ]; then
  echo DRIVER_AGENT_IIO_DUMMY_NAME $(cat "$DEV/name")
fi
find "$DEV" -maxdepth 1 -type f -print 2>/dev/null | head -20
rmdir /sys/kernel/config/iio/devices/dummy/driver_agent_dummy0 2>/dev/null || true
echo DRIVER_AGENT_QEMU_IIO_DUMMY_PASS
poweroff -f
"""
    write_text(root / "init", init)
    os.chmod(root / "init", 0o755)
    initrd = out_dir / "initramfs.cpio.gz"
    cmd = f"find . -print0 | cpio --null -ov --format=newc | gzip -9 > {initrd}"
    proc = subprocess.run(cmd, cwd=str(root), shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"initramfs build failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return initrd


def run_qemu(kernel_tree: Path, initrd: Path, out_dir: Path, timeout_sec: int) -> dict[str, Any]:
    cmd = [
        "qemu-system-x86_64", "-nodefaults", "-serial", "mon:stdio", "-nographic", "-no-reboot", "-m", "1G",
        "-kernel", str(kernel_tree / "arch/x86/boot/bzImage"), "-initrd", str(initrd), "-append", "console=ttyS0 panic=-1 quiet",
    ]
    res = run(cmd, cwd=kernel_tree, timeout=timeout_sec)
    text = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    res["success_marker"] = "DRIVER_AGENT_QEMU_IIO_DUMMY_PASS" in text
    res["failure_marker"] = "DRIVER_AGENT_IIO_DUMMY_FAIL" in text
    res["ok"] = bool(res["success_marker"])
    write_text(out_dir / "qemu_output.txt", text)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Run IIO dummy in-tree driver smoke test inside QEMU")
    ap.add_argument("--kernel-tree", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout-sec", type=int, default=900)
    args = ap.parse_args()
    kernel_tree = Path(args.kernel_tree).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"target": "iio-dummy-smoke", "kernel_tree": str(kernel_tree), "out_dir": str(out_dir), "status": "unknown", "configs": CONFIGS, "steps": []}
    try:
        ok, steps = build_kernel(kernel_tree, args.jobs)
        result["steps"] = [trunc(s) for s in steps]
        if not ok:
            result["status"] = "build_failed"
            write_json(out_dir / "result.json", result)
            print(json.dumps({"target": result["target"], "status": result["status"], "result_path": str(out_dir / "result.json")}, indent=2))
            return 2
        initrd = make_initramfs(out_dir)
        qemu = run_qemu(kernel_tree, initrd, out_dir, args.timeout_sec)
        result["qemu"] = trunc(qemu)
        result["status"] = "passed" if qemu.get("ok") else "qemu_failed"
        write_json(out_dir / "result.json", result)
        print(json.dumps({"target": result["target"], "status": result["status"], "result_path": str(out_dir / "result.json"), "qemu_output_path": str(out_dir / "qemu_output.txt"), "qemu_ok": bool(qemu.get("ok"))}, indent=2))
        return 0 if qemu.get("ok") else 3
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        write_json(out_dir / "result.json", result)
        print(json.dumps({"target": result["target"], "status": result["status"], "error": str(exc), "result_path": str(out_dir / "result.json")}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
