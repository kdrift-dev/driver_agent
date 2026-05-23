#!/usr/bin/env python3
"""Classify small in-tree driver migration cases by QEMU testability.

This is an evaluation/planning helper. It does not feed reference driver diffs
or testability labels back into Driver-Agent inputs.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Case:
    module_path: str
    modified_loc: int
    code_loc: int
    source_file_count: int
    changed_files_count: int
    changed_file_samples: list[str]
    qemu_scope: str
    confidence: str
    test_target: str
    reason: str


def run_git(repo: str, args: list[str]) -> str:
    return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL)


def changed_files(repo: str, old_ref: str, new_ref: str, module_path: str) -> list[str]:
    out = run_git(repo, ["diff", "--name-only", f"{old_ref}..{new_ref}", "--", module_path])
    return [line.strip() for line in out.splitlines() if line.strip()]


def selftest_exists(repo: str, ref: str, target: str) -> bool:
    try:
        run_git(repo, ["ls-tree", "-r", "--name-only", ref, "--", target])
    except subprocess.CalledProcessError:
        return False
    return True


def contains_any(texts: Iterable[str], needles: Iterable[str]) -> bool:
    joined = "\n".join(texts)
    return any(needle in joined for needle in needles)


def classify(module_path: str, files: list[str], repo: str, ref: str) -> tuple[str, str, str, str]:
    """Return qemu_scope, confidence, test_target, reason."""
    all_paths = [module_path, *files]

    exact_rules = [
        (
            lambda: module_path == "drivers/dma-buf/heaps" or contains_any(files, ["drivers/dma-buf/heaps/"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/dmabuf-heaps",
            "dmabuf heaps expose a userspace-visible heap interface with dedicated kselftest",
        ),
        (
            lambda: module_path == "drivers/dma-buf" or contains_any(files, ["drivers/dma-buf/udmabuf.c"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/drivers/dma-buf",
            "udmabuf has a dedicated driver selftest and no physical device dependency",
        ),
        (
            lambda: module_path == "drivers/block/zram" or contains_any(files, ["drivers/block/zram/"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/zram",
            "zram is software-only and has dedicated kselftests",
        ),
        (
            lambda: module_path == "drivers/net/netdevsim" or contains_any(files, ["drivers/net/netdevsim/"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/drivers/net/netdevsim",
            "netdevsim is a software simulator with dedicated kselftests",
        ),
        (
            lambda: module_path == "drivers/net/wireguard" or contains_any(files, ["drivers/net/wireguard/"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/wireguard",
            "wireguard has dedicated namespace/QEMU selftests and no physical NIC requirement",
        ),
        (
            lambda: module_path == "drivers/block/null_blk" or contains_any(files, ["drivers/block/null_blk/"]),
            "qemu_sufficient",
            "medium",
            "custom QEMU configfs/block smoke",
            "null_blk is a software block-device driver that can be instantiated without physical hardware",
        ),
        (
            lambda: module_path == "drivers/vdpa/vdpa_sim" or contains_any(files, ["drivers/vdpa/vdpa_sim/"]),
            "qemu_sufficient",
            "medium",
            "custom QEMU vdpa_sim smoke",
            "vdpa_sim is a software simulator; useful coverage depends on vdpa tooling in the guest",
        ),
        (
            lambda: module_path == "drivers/net/netconsole" or contains_any(files, ["drivers/net/netconsole.c"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/drivers/net/netconsole",
            "netconsole has dedicated driver selftests and can run over virtual networking",
        ),
        (
            lambda: module_path in {"drivers/virtio", "drivers/vhost", "drivers/s390/virtio"}
            or contains_any(files, ["drivers/net/virtio_net.c", "drivers/virtio/", "drivers/vhost/"]),
            "qemu_sufficient",
            "medium",
            "tools/testing/selftests/drivers/net/virtio_net; tools/virtio",
            "virtio/vhost paths are QEMU-native, though exact coverage depends on changed file",
        ),
        (
            lambda: module_path == "drivers/gpu/drm/virtio" or contains_any(files, ["drivers/gpu/drm/virtio/"]),
            "qemu_sufficient",
            "medium",
            "custom QEMU virtio-gpu smoke; DRM/KMS tools",
            "virtio-gpu is QEMU-native, though display/KMS coverage depends on guest setup",
        ),
        (
            lambda: module_path.startswith("drivers/gpio")
            and contains_any(files, ["gpio-mockup", "gpio-sim", "gpio-aggregator"]),
            "qemu_sufficient",
            "high",
            "tools/testing/selftests/gpio",
            "GPIO mockup/sim/aggregator are software-visible and covered by gpio selftests",
        ),
        (
            lambda: module_path == "drivers/iio/dummy" or contains_any(files, ["drivers/iio/dummy/"]),
            "qemu_sufficient",
            "medium",
            "custom QEMU configfs/sysfs smoke test",
            "iio dummy is a synthetic driver with software-visible interfaces",
        ),
        (
            lambda: module_path == "drivers/base/test" or contains_any(files, ["drivers/base/test/"]),
            "qemu_sufficient",
            "high",
            "KUnit: drivers/base/test/.kunitconfig",
            "driver core tests are KUnit-based and do not require physical hardware",
        ),
        (
            lambda: module_path == "drivers/iio/test" or contains_any(files, ["drivers/iio/test/"]),
            "qemu_sufficient",
            "medium",
            "KUnit: IIO test modules",
            "IIO test drivers are KUnit-oriented synthetic tests",
        ),
        (
            lambda: module_path.startswith("drivers/gpu/drm")
            and contains_any(files, ["drivers/gpu/drm/tests/", "drivers/gpu/drm/vkms/", "drivers/gpu/drm/ttm/tests/"]),
            "qemu_sufficient",
            "medium",
            "KUnit: DRM/VKMS/TTM tests",
            "DRM test/VKMS/TTM paths have KUnit or software-driver coverage",
        ),
        (
            lambda: module_path.startswith("drivers/dax") or module_path.startswith("drivers/nvdimm"),
            "qemu_partial",
            "medium",
            "tools/testing/nvdimm; custom QEMU memmap smoke",
            "nvdimm/dax have test scaffolding, but setup is more specialized than generic QEMU selftests",
        ),
        (
            lambda: module_path.startswith("drivers/vfio"),
            "qemu_partial",
            "medium",
            "tools/testing/selftests/vfio",
            "VFIO has selftests but often needs special device/IOMMU setup",
        ),
        (
            lambda: module_path == "drivers/ntb/test" or contains_any(files, ["drivers/ntb/test/"]),
            "qemu_partial",
            "medium",
            "tools/testing/selftests/ntb",
            "NTB test modules exist, but meaningful coverage needs NTB emulation or suitable topology",
        ),
        (
            lambda: module_path.startswith("drivers/target/loopback") or module_path.startswith("drivers/target/tcm_remote"),
            "qemu_partial",
            "medium",
            "custom QEMU target-core smoke",
            "target loopback/remote paths are software-facing but require target-core setup",
        ),
        (
            lambda: module_path.startswith("drivers/pps") and contains_any(files, ["pps-ktimer", "pps_loop", "pps_cdev"]),
            "qemu_partial",
            "low",
            "custom QEMU module/sysfs smoke",
            "some PPS paths are software-visible, but many clients require hardware/parport",
        ),
    ]
    for pred, scope, confidence, target, reason in exact_rules:
        if pred():
            if target.startswith("tools/") and not selftest_exists(repo, ref, target.split(";")[0].strip()):
                confidence = "medium" if confidence == "high" else confidence
                reason += "; configured test target was not found exactly in this ref"
            return scope, confidence, target, reason

    hardware_prefixes = (
        "drivers/gpu/drm/amd",
        "drivers/media/",
        "drivers/sound",
        "drivers/hid/",
        "drivers/usb/",
        "drivers/i2c/",
        "drivers/spi/",
        "drivers/pci/",
        "drivers/mmc/",
        "drivers/net/ethernet/",
        "drivers/net/wireless/",
        "drivers/input/",
        "drivers/power/",
        "drivers/thermal/",
        "drivers/firmware/",
        "drivers/platform/",
        "drivers/pinctrl/",
        "drivers/clk/",
        "drivers/dma/",
    )
    hardware_parent_paths = {
        "drivers/usb",
        "drivers/i2c",
        "drivers/spi",
        "drivers/mmc",
        "drivers/dma",
        "drivers/media",
        "drivers/video",
        "drivers/sound",
        "drivers/hid",
        "drivers/net/ethernet",
        "drivers/net/wireless",
    }
    if module_path in hardware_parent_paths or module_path.startswith(hardware_prefixes):
        return (
            "hardware_required",
            "medium",
            "",
            "path indicates physical bus/subsystem behavior; QEMU can at most provide build/load smoke without matching hardware",
        )

    if contains_any(all_paths, ["test", "dummy", "virt", "vhost", "sim", "mock", "loopback", "null_blk"]):
        return (
            "qemu_partial",
            "low",
            "custom QEMU smoke",
            "name suggests synthetic or software-visible behavior, but no direct test target was matched",
        )

    return (
        "hardware_required",
        "low",
        "",
        "no direct QEMU/kselftest/KUnit signal matched; treat as hardware-required until manually reviewed",
    )


def write_outputs(cases: list[Case], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "qemu_testable_intree_cases.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(cases[0]).keys()) if cases else [
            "module_path",
            "modified_loc",
            "code_loc",
            "source_file_count",
            "changed_files_count",
            "changed_file_samples",
            "qemu_scope",
            "confidence",
            "test_target",
            "reason",
        ])
        writer.writeheader()
        for case in cases:
            row = asdict(case)
            row["changed_file_samples"] = ";".join(case.changed_file_samples)
            writer.writerow(row)
    (out_dir / "qemu_testable_intree_cases.json").write_text(
        json.dumps([asdict(c) for c in cases], indent=2), encoding="utf-8"
    )

    counts: dict[str, int] = {}
    for case in cases:
        counts[case.qemu_scope] = counts.get(case.qemu_scope, 0) + 1
    high = [c for c in cases if c.qemu_scope == "qemu_sufficient" and c.confidence in {"high", "medium"}]
    partial = [c for c in cases if c.qemu_scope == "qemu_partial"]
    summary = {
        "total_cases": len(cases),
        "scope_counts": counts,
        "qemu_sufficient_high_or_medium": len(high),
        "qemu_partial": len(partial),
    }
    (out_dir / "qemu_testable_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# QEMU-Testable In-Tree Cases",
        "",
        "This is an evaluation/planning artifact. Labels are heuristic and must be manually reviewed before making paper claims.",
        "",
        "## Summary",
        "",
        f"- Total changed modules with modified_loc filter: {len(cases)}",
        *[f"- {k}: {v}" for k, v in sorted(counts.items())],
        "",
        "## Recommended Shortlist",
        "",
        "| module_path | scope | confidence | modified_loc | test target | reason |",
        "|---|---:|---:|---:|---|---|",
    ]
    for case in high[:40]:
        lines.append(
            f"| `{case.module_path}` | {case.qemu_scope} | {case.confidence} | {case.modified_loc} | "
            f"`{case.test_target}` | {case.reason} |"
        )
    lines.extend([
        "",
        "## Partial Candidates",
        "",
        "| module_path | scope | confidence | modified_loc | test target | reason |",
        "|---|---:|---:|---:|---|---|",
    ])
    for case in partial[:40]:
        lines.append(
            f"| `{case.module_path}` | {case.qemu_scope} | {case.confidence} | {case.modified_loc} | "
            f"`{case.test_target}` | {case.reason} |"
        )
    (out_dir / "qemu_testable_intree_cases.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-csv", default="/mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv")
    ap.add_argument("--linux-repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    ap.add_argument("--old-ref", default="v6.19")
    ap.add_argument("--new-ref", default="v7.0-rc1")
    ap.add_argument("--max-modified-loc", type=int, default=100)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    cases: list[Case] = []
    with open(args.cases_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                modified_loc = int(row.get("modified_loc") or 0)
                changed_files_count = int(row.get("changed_files") or 0)
                source_file_count = int(row.get("source_file_count") or 0)
                code_loc = int(row.get("code_loc") or 0)
            except ValueError:
                continue
            if modified_loc <= 0 or modified_loc > args.max_modified_loc:
                continue
            if changed_files_count <= 0 or source_file_count <= 0:
                continue
            module_path = row["module_path"]
            files = changed_files(args.linux_repo, args.old_ref, args.new_ref, module_path)
            scope, confidence, target, reason = classify(module_path, files, args.linux_repo, args.new_ref)
            cases.append(Case(
                module_path=module_path,
                modified_loc=modified_loc,
                code_loc=code_loc,
                source_file_count=source_file_count,
                changed_files_count=changed_files_count,
                changed_file_samples=files[:10],
                qemu_scope=scope,
                confidence=confidence,
                test_target=target,
                reason=reason,
            ))

    order = {"qemu_sufficient": 0, "qemu_partial": 1, "hardware_required": 2}
    conf_order = {"high": 0, "medium": 1, "low": 2}
    cases.sort(key=lambda c: (order.get(c.qemu_scope, 9), conf_order.get(c.confidence, 9), c.modified_loc, c.module_path))
    write_outputs(cases, Path(args.out_dir))
    print(json.dumps({
        "out_dir": args.out_dir,
        "total_cases": len(cases),
        "qemu_sufficient": sum(c.qemu_scope == "qemu_sufficient" for c in cases),
        "qemu_partial": sum(c.qemu_scope == "qemu_partial" for c in cases),
        "hardware_required": sum(c.qemu_scope == "hardware_required" for c in cases),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
