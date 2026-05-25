#!/usr/bin/env python3
"""Build a reproducible in-tree/OOT testcase catalog.

This is a planning/evaluation helper. It must not feed target-driver new-version
diffs or hand-written case-specific fixes into Driver-Agent.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


OOT_CANDIDATES: list[dict[str, Any]] = [
    {
        "case_id": "oot_v4l2loopback_v0_14_0",
        "kind": "oot",
        "module_path": "oot/v4l2loopback",
        "repo_url": "https://github.com/v4l2loopback/v4l2loopback.git",
        "repo_ref": "v0.14.0",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "v4l2loopback.ko",
        "module_name": "v4l2loopback",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
        "test_target": "insmod + /sys/devices/virtual/video4linux smoke",
        "reason": "popular virtual V4L2 loopback driver; no physical camera required. OOT old-kernel baseline is case-specific and still needs probing.",
        "run_priority": 30,
    },
    {
        "case_id": "oot_webcamoid_akvcam_1_2_7",
        "kind": "oot",
        "module_path": "oot/akvcam",
        "repo_url": "https://github.com/webcamoid/akvcam.git",
        "repo_ref": "1.2.7",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "akvcam.ko",
        "module_name": "akvcam",
        "qemu_scope": "qemu_sufficient",
        "confidence": "medium",
        "test_target": "insmod + virtual video device smoke",
        "reason": "virtual camera driver; should be testable without camera hardware, but build layout needs probe",
        "run_priority": 2,
    },
    {
        "case_id": "oot_evdi_v1_14_9",
        "kind": "oot",
        "module_path": "oot/evdi",
        "repo_url": "https://github.com/DisplayLink/evdi.git",
        "repo_ref": "v1.14.9",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": "module",
        "module_ko": "evdi.ko",
        "module_name": "evdi",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
        "test_target": "insmod load/unload; DRM userspace requires extra setup",
        "reason": "virtual display kernel module; useful compile/load case, full behavior needs libevdi/userspace",
        "run_priority": 3,
    },
    {
        "case_id": "oot_simplefs_rel2025_0",
        "kind": "oot",
        "module_path": "oot/simplefs",
        "repo_url": "https://github.com/sysprog21/simplefs.git",
        "repo_ref": "rel2025.0",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "simplefs.ko",
        "module_name": "simplefs",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
        "test_target": "insmod + mount prepared simplefs image",
        "reason": "small OOT filesystem; meaningful runtime test needs a prepared block image",
        "run_priority": 4,
    },
    {
        "case_id": "oot_udmabuf_v5_4_2",
        "kind": "oot",
        "module_path": "oot/udmabuf",
        "repo_url": "https://github.com/ikwzm/udmabuf.git",
        "repo_ref": "v5.4.2",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "u-dma-buf.ko",
        "module_name": "u-dma-buf",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
        "test_target": "insmod + /dev/u-dma-buf smoke if device node appears",
        "reason": "DMA buffer OOT module; loadable without special hardware, but memory/device setup may vary",
        "run_priority": 5,
    },
    {
        "case_id": "oot_vendor_reset_v0_1_0",
        "kind": "oot",
        "module_path": "oot/vendor-reset",
        "repo_url": "https://github.com/gnif/vendor-reset.git",
        "repo_ref": "v0.1.0",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "vendor-reset.ko",
        "module_name": "vendor-reset",
        "qemu_scope": "hardware_required",
        "confidence": "medium",
        "test_target": "compile/load only; reset behavior needs target PCI GPU",
        "reason": "popular OOT PCI reset module; good API-adaptation compile case, not a QEMU functional proof",
        "run_priority": 9,
    },
    {
        "case_id": "oot_gve_v1_4_5",
        "kind": "oot",
        "module_path": "oot/gve",
        "repo_url": "https://github.com/GoogleCloudPlatform/compute-virtual-ethernet-linux.git",
        "repo_ref": "v1.4.5",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": ".",
        "module_ko": "gve.ko",
        "module_name": "gve",
        "qemu_scope": "hardware_required",
        "confidence": "medium",
        "test_target": "compile only unless GCE virtual NIC is available",
        "reason": "real OOT cloud virtual NIC driver; functional test needs GCE hardware/device model",
        "run_priority": 10,
    },
    {
        "case_id": "oot_lddbus_eg22_5_7_8",
        "kind": "oot",
        "module_path": "oot/Linux-Device-Driver/eg_22_lddbus",
        "repo_url": "https://github.com/d0u9/Linux-Device-Driver.git",
        "repo_ref": "HEAD",
        "kernel_old_ref": "v6.1",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "full_module",
        "source_subdir": "eg_22_lddbus",
        "module_ko": "lddbus.ko",
        "module_name": "lddbus",
        "qemu_scope": "qemu_sufficient",
        "confidence": "high",
        "test_target": "insmod + sysfs/device-node smoke",
        "reason": "known small OOT educational driver; validated as v6.1 -> v7.0-rc1 with QEMU load/interface smoke",
        "run_priority": 1,
    },
    {
        "case_id": "oot_ldd3_scull_master",
        "kind": "oot",
        "module_path": "oot/ldd3/scull",
        "repo_url": "https://github.com/martinezjavier/ldd3.git",
        "repo_ref": "master",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": "scull",
        "module_ko": "scull.ko",
        "module_name": "scull",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
        "test_target": "insmod + mknod/read-write smoke after build-layout probe",
        "reason": "classic char-device example; useful OOT smoke case if it still builds after API migration",
        "run_priority": 6,
    },
    {
        "case_id": "oot_lkmc_v3_0_hello",
        "kind": "oot",
        "module_path": "oot/linux-kernel-module-cheat/hello",
        "repo_url": "https://github.com/cirosantilli/linux-kernel-module-cheat.git",
        "repo_ref": "v3.0",
        "kernel_old_ref": "",
        "kernel_new_ref": "v7.0-rc1",
        "patch_source_mode": "",
        "source_subdir": "kernel_modules/hello",
        "module_ko": "hello.ko",
        "module_name": "hello",
        "qemu_scope": "qemu_partial",
        "confidence": "low",
        "test_target": "compile/load smoke after build-layout probe",
        "reason": "large educational kernel-module repo; useful only if source_subdir remains stable",
        "run_priority": 8,
    },
]


FIELDNAMES = [
    "case_id",
    "kind",
    "module_path",
    "modified_loc",
    "code_loc",
    "source_file_count",
    "qemu_scope",
    "confidence",
    "test_target",
    "reason",
    "repo_url",
    "repo_ref",
    "kernel_old_ref",
    "kernel_new_ref",
    "patch_source_mode",
    "source_subdir",
    "module_ko",
    "module_name",
    "recommended_run",
]


def _safe_ref(ref: str) -> str:
    return ref.replace("/", "_").replace(".", "_").replace("-", "_")


def _intree_case_id(module_path: str, old_ref: str, new_ref: str) -> str:
    base = module_path.replace("/", "_")
    if old_ref == "v6.19" and new_ref == "v7.0-rc1":
        return f"intree_{base}"
    return f"intree_{base}__{_safe_ref(old_ref)}__{_safe_ref(new_ref)}"


def _read_intree_cases(path: Path, limit: int, old_ref: str, new_ref: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("qemu_scope") not in {"qemu_sufficient", "qemu_partial"}:
                continue
            out.append(
                {
                    "case_id": _intree_case_id(row["module_path"], old_ref, new_ref),
                    "kind": "intree",
                    "module_path": row["module_path"],
                    "modified_loc": row.get("modified_loc", ""),
                    "code_loc": row.get("code_loc", ""),
                    "source_file_count": row.get("source_file_count", ""),
                    "qemu_scope": row.get("qemu_scope", ""),
                    "confidence": row.get("confidence", ""),
                    "test_target": row.get("test_target", ""),
                    "reason": row.get("reason", ""),
                    "repo_url": "",
                    "repo_ref": "",
                    "kernel_old_ref": old_ref,
                    "kernel_new_ref": new_ref,
                    "patch_source_mode": "focused",
                    "source_subdir": "",
                    "module_ko": "",
                    "module_name": "",
                    "recommended_run": "",
                }
            )
            if len(out) >= limit:
                break
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def _render_md(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Testcase Catalog",
        "",
        "Scope: in-tree cases carry per-case `kernel_old_ref` and `kernel_new_ref`. OOT cases may use a case-specific known-good old kernel ref, but target v7.0-rc1. Reference/new target-driver diffs are evaluation-only.",
        "",
        "| case_id | kind | module_path | scope | confidence | modified_loc | source LOC | test target |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row.get('case_id')}` | {row.get('kind')} | `{row.get('module_path')}` | "
            f"{row.get('qemu_scope')} | {row.get('confidence')} | {row.get('modified_loc', '')} | "
            f"{row.get('code_loc', '')} | `{row.get('test_target')}` |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intree-csv", required=True)
    ap.add_argument("--intree-old-ref", default="v6.19")
    ap.add_argument("--intree-new-ref", default="v7.0-rc1")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--intree-limit", type=int, default=22)
    ap.add_argument("--oot-limit", type=int, default=10)
    ap.add_argument("--catalog-size", type=int, default=30)
    args = ap.parse_args()

    intree = _read_intree_cases(Path(args.intree_csv), args.intree_limit, args.intree_old_ref, args.intree_new_ref)
    oot = sorted(OOT_CANDIDATES, key=lambda r: (int(r.get("run_priority", 99)), r["case_id"]))[: args.oot_limit]
    rows = (intree + oot)[: args.catalog_size]

    default_preferred_modules = {
        "drivers/dma-buf/heaps",
        "drivers/net/wireguard",
        "drivers/net/netdevsim",
        "drivers/iio/dummy",
        "drivers/gpu/drm/ttm/tests",
        "drivers/block/null_blk",
        "drivers/vdpa/vdpa_sim",
        "drivers/gpu/drm/vkms",
        "drivers/target/tcm_remote",
    }
    v60_preferred_modules = {
        "drivers/dma-buf/heaps",
        "drivers/base/test",
        "drivers/s390/virtio",
        "drivers/iio/dummy",
        "drivers/vfio/platform/reset",
        "drivers/dax/pmem",
        "drivers/ntb/test",
        "drivers/target/loopback",
        "drivers/vfio/fsl-mc",
        "drivers/dax/hmem",
    }
    if args.intree_old_ref == "v6.0" and args.intree_new_ref == "v7.0-rc1":
        preferred = {_intree_case_id(m, args.intree_old_ref, args.intree_new_ref) for m in v60_preferred_modules}
    else:
        # Mark a small run set: prefer v6.19->v7.0-rc1 in-tree cases that already
        # passed the current no-QEMU compile flow, plus the OOT lddbus case that has
        # its own known-good old kernel ref and target v7.0-rc1.
        preferred = {_intree_case_id(m, args.intree_old_ref, args.intree_new_ref) for m in default_preferred_modules}
        preferred.add("oot_lddbus_eg22_5_7_8")
    for row in rows:
        row["recommended_run"] = "yes" if row["case_id"] in preferred else "no"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "testcase_catalog.csv", rows)
    (out_dir / "testcase_catalog.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (out_dir / "testcase_catalog.md").write_text(_render_md(rows), encoding="utf-8")
    (out_dir / "run10_cases.txt").write_text(
        "\n".join(row["case_id"] for row in rows if row.get("recommended_run") == "yes") + "\n",
        encoding="utf-8",
    )
    summary = {
        "total": len(rows),
        "intree": sum(r["kind"] == "intree" for r in rows),
        "oot": sum(r["kind"] == "oot" for r in rows),
        "recommended_run": sum(r.get("recommended_run") == "yes" for r in rows),
        "scope_counts": {},
    }
    for row in rows:
        scope = row.get("qemu_scope", "unknown")
        summary["scope_counts"][scope] = summary["scope_counts"].get(scope, 0) + 1
    (out_dir / "testcase_catalog_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
