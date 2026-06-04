#!/usr/bin/env python3
"""Build candidate CSVs for strict testable in-tree hard-break screening.

The output is intentionally small and auditable: only module paths for which
Driver-Agent has deterministic runtime hooks are included. This avoids the
previous failure mode where large parent directories or "testable in theory"
paths entered the experiment but could not run a concrete validation hook.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path


SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".S", ".s"}


AUTO_RUNTIME_MODULES: dict[str, dict[str, str]] = {
    "drivers/iio/dummy": {
        "test_kind": "qemu_smoke",
        "test_target": "deterministic IIO dummy configfs smoke in QEMU",
        "qemu_scope": "qemu_sufficient",
        "confidence": "high",
    },
    "drivers/block/zram": {
        "test_kind": "qemu_kselftest",
        "test_target": "tools/testing/selftests/zram via isolated QEMU runner",
        "qemu_scope": "qemu_sufficient",
        "confidence": "high",
    },
    "drivers/dma-buf/heaps": {
        "test_kind": "qemu_kselftest",
        "test_target": "tools/testing/selftests/dmabuf-heaps via isolated QEMU runner",
        "qemu_scope": "qemu_sufficient",
        "confidence": "high",
    },
    "drivers/base/test": {
        "test_kind": "kunit",
        "test_target": "drivers/base/test KUnit",
        "qemu_scope": "qemu_sufficient",
        "confidence": "high",
    },
    "drivers/base/power": {
        "test_kind": "kunit",
        "test_target": "base PM KUnit generated config",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/base/regmap": {
        "test_kind": "kunit",
        "test_target": "regmap KUnit generated config",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/firewire": {
        "test_kind": "kunit",
        "test_target": "drivers/firewire KUnit",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/gpu/drm/scheduler": {
        "test_kind": "kunit",
        "test_target": "DRM scheduler KUnit",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/gpu/drm/ttm": {
        "test_kind": "kunit",
        "test_target": "DRM TTM KUnit",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/gpu/drm/vkms": {
        "test_kind": "kunit",
        "test_target": "VKMS KUnit",
        "qemu_scope": "qemu_sufficient",
        "confidence": "medium",
    },
    "drivers/gpu/drm/vc4": {
        "test_kind": "kunit",
        "test_target": "VC4 KUnit",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
    "drivers/iio/test": {
        "test_kind": "kunit",
        "test_target": "IIO KUnit generated config",
        "qemu_scope": "qemu_partial",
        "confidence": "medium",
    },
}


def git(repo: Path, args: list[str], *, check: bool = False) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git failed: {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def exists_at(repo: Path, ref: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def source_files(repo: Path, ref: str, module_path: str) -> list[str]:
    out = git(repo, ["ls-tree", "-r", "--name-only", ref, "--", module_path])
    return [x for x in out.splitlines() if Path(x).suffix in SOURCE_SUFFIXES]


def file_text(repo: Path, ref: str, path: str) -> str:
    return git(repo, ["show", f"{ref}:{path}"])


def code_loc(repo: Path, ref: str, files: list[str]) -> int:
    total = 0
    for path in files:
        text = file_text(repo, ref, path)
        total += sum(1 for line in text.splitlines() if line.strip())
    return total


def diff_stats(repo: Path, old_ref: str, new_ref: str, module_path: str) -> tuple[int, int, int, list[str]]:
    numstat = git(repo, ["diff", "--numstat", f"{old_ref}..{new_ref}", "--", module_path])
    added = deleted = changed = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            a = int(parts[0])
            d = int(parts[1])
        except ValueError:
            continue
        added += a
        deleted += d
        changed += 1
    names = [x for x in git(repo, ["diff", "--name-only", f"{old_ref}..{new_ref}", "--", module_path]).splitlines() if x]
    return added, deleted, changed, names


def build(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for old_ref in args.old_refs.split(","):
        old_ref = old_ref.strip()
        if not old_ref:
            continue
        for module_path, meta in AUTO_RUNTIME_MODULES.items():
            if not exists_at(repo, old_ref, module_path) or not exists_at(repo, args.new_ref, module_path):
                continue
            files = source_files(repo, old_ref, module_path)
            if not files:
                continue
            loc = code_loc(repo, old_ref, files)
            added, deleted, changed_count, changed_names = diff_stats(repo, old_ref, args.new_ref, module_path)
            modified_loc = added + deleted
            if modified_loc <= 0:
                continue
            if loc > args.max_code_loc or len(files) > args.max_source_files:
                continue
            if args.max_modified_loc and modified_loc > args.max_modified_loc:
                continue
            rows.append(
                {
                    "from_ref": old_ref,
                    "to_ref": args.new_ref,
                    "module_path": module_path,
                    "status": "existing",
                    "source_file_count": len(files),
                    "code_loc": loc,
                    "changed_files": changed_count,
                    "added_loc": added,
                    "deleted_loc": deleted,
                    "modified_loc": modified_loc,
                    "qemu_scope": meta["qemu_scope"],
                    "confidence": meta["confidence"],
                    "test_kind": meta["test_kind"],
                    "test_target": meta["test_target"],
                    "changed_file_samples": ";".join(changed_names[:12]),
                    "selection_note": "deterministic_runtime_hook_available; strict P0 screen still required",
                }
            )

    rows.sort(
        key=lambda r: (
            str(r["from_ref"]),
            {"qemu_sufficient": 0, "qemu_partial": 1}.get(str(r["qemu_scope"]), 9),
            int(r["modified_loc"]),
            int(r["code_loc"]),
            str(r["module_path"]),
        )
    )

    path = out_dir / "strict_testable_candidates.csv"
    fields = [
        "from_ref",
        "to_ref",
        "module_path",
        "status",
        "source_file_count",
        "code_loc",
        "changed_files",
        "added_loc",
        "deleted_loc",
        "modified_loc",
        "qemu_scope",
        "confidence",
        "test_kind",
        "test_target",
        "changed_file_samples",
        "selection_note",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[candidates] rows={len(rows)} path={path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    p.add_argument("--new-ref", default="v7.0")
    p.add_argument("--old-refs", default="v5.0,v5.4,v5.10,v5.15,v6.0,v6.6,v6.12,v6.19")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--max-code-loc", type=int, default=12000)
    p.add_argument("--max-source-files", type=int, default=20)
    p.add_argument("--max-modified-loc", type=int, default=0, help="0 disables this filter")
    args = p.parse_args()
    build(args)


if __name__ == "__main__":
    main()
