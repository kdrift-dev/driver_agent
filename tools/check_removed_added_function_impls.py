#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.symbol_scanner import _extract_c_like_functions, _extract_rust_functions
from driver_agent_pregen.utils import now_timestamp, slugify, write_json

SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}


@dataclass
class ModuleResult:
    idx: int
    module_path: str
    status: str
    changed_source_files: int
    removed_funcs: set[str]
    added_funcs: set[str]
    error: str = ""


def _run_git(repo: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo)] + args
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _path_exists(repo: Path, ref: str, path: str) -> bool:
    p = _run_git(repo, ["cat-file", "-e", f"{ref}:{path}"], check=False)
    return p.returncode == 0


def _read_file(repo: Path, ref: str, path: str) -> Optional[str]:
    p = _run_git(repo, ["show", f"{ref}:{path}"], check=False)
    if p.returncode != 0:
        return None
    return p.stdout


def _parse_diff_changed_lines(diff_text: str) -> dict[str, dict[str, set[int]]]:
    out: dict[str, dict[str, set[int]]] = {}
    old_path = None
    new_path = None
    old_ln = None
    new_ln = None

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git a/"):
            old_path = None
            new_path = None
            old_ln = None
            new_ln = None
            continue

        if raw.startswith("--- "):
            tok = raw[4:].strip()
            if tok == "/dev/null":
                old_path = None
            elif tok.startswith("a/"):
                old_path = tok[2:]
            else:
                old_path = tok
            continue

        if raw.startswith("+++ "):
            tok = raw[4:].strip()
            if tok == "/dev/null":
                new_path = None
            elif tok.startswith("b/"):
                new_path = tok[2:]
            else:
                new_path = tok
            continue

        if raw.startswith("@@ "):
            try:
                mid = raw.split("@@", 2)[1].strip()
                a, b = mid.split(" ")[:2]
                old_ln = int(a.split(",")[0].lstrip("-"))
                new_ln = int(b.split(",")[0].lstrip("+"))
            except Exception:
                old_ln = None
                new_ln = None
            continue

        if old_ln is None or new_ln is None:
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue

        if raw.startswith(" "):
            old_ln += 1
            new_ln += 1
            continue

        if raw.startswith("-"):
            if old_path:
                rec = out.setdefault(old_path, {"old": set(), "new": set()})
                rec["old"].add(old_ln)
            old_ln += 1
            continue

        if raw.startswith("+"):
            if new_path:
                rec = out.setdefault(new_path, {"old": set(), "new": set()})
                rec["new"].add(new_ln)
            new_ln += 1
            continue

    return out


def _extract_functions(text: str, rel_file: str) -> list[tuple[str, int, int]]:
    suffix = Path(rel_file).suffix.lower()
    if suffix == ".rs":
        fs = _extract_rust_functions(text, rel_file)
    elif suffix in SOURCE_SUFFIXES:
        fs = _extract_c_like_functions(text, rel_file)
    else:
        return []

    out: list[tuple[str, int, int]] = []
    for f in fs:
        out.append((f.name, int(f.start_line), int(f.end_line)))
    return out


def _hit_funcs(line_set: set[int], funcs: list[tuple[str, int, int]]) -> set[str]:
    hits = set()
    if not line_set:
        return hits
    for name, start, end in funcs:
        # overlap check
        for ln in line_set:
            if start <= ln <= end:
                hits.add(name)
                break
    return hits


def _eval_one(idx: int, module_path: str, repo: Path, old_ref: str, new_ref: str) -> ModuleResult:
    try:
        diff = _run_git(repo, ["diff", "--no-color", "--unified=0", old_ref, new_ref, "--", module_path], check=False).stdout
        changed = _parse_diff_changed_lines(diff)

        changed_src_paths = [p for p in changed.keys() if Path(p).suffix.lower() in SOURCE_SUFFIXES]
        if not changed_src_paths:
            return ModuleResult(idx, module_path, "skipped_no_source_change", 0, set(), set())

        removed_funcs: set[str] = set()
        added_funcs: set[str] = set()

        for rel in sorted(set(changed_src_paths)):
            lines_old = changed.get(rel, {}).get("old", set())
            lines_new = changed.get(rel, {}).get("new", set())

            old_txt = _read_file(repo, old_ref, rel) if _path_exists(repo, old_ref, rel) else None
            new_txt = _read_file(repo, new_ref, rel) if _path_exists(repo, new_ref, rel) else None

            old_defs = _extract_functions(old_txt or "", rel) if old_txt is not None else []
            new_defs = _extract_functions(new_txt or "", rel) if new_txt is not None else []

            old_names = {n for n, _, _ in old_defs}
            new_names = {n for n, _, _ in new_defs}

            removed_candidates = old_names - new_names
            added_candidates = new_names - old_names

            old_map = {n: (s, e) for n, s, e in old_defs}
            new_map = {n: (s, e) for n, s, e in new_defs}

            old_hit = _hit_funcs(lines_old, [(n, *old_map[n]) for n in removed_candidates if n in old_map])
            new_hit = _hit_funcs(lines_new, [(n, *new_map[n]) for n in added_candidates if n in new_map])

            for n in sorted(old_hit):
                removed_funcs.add(f"{rel}:{n}")
            for n in sorted(new_hit):
                added_funcs.add(f"{rel}:{n}")

        return ModuleResult(idx, module_path, "ok", len(changed_src_paths), removed_funcs, added_funcs)

    except Exception as exc:
        return ModuleResult(idx, module_path, "failed", 0, set(), set(), error=str(exc))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check modules for removed old function impl + added new function impl from git diff only")
    p.add_argument("--csv", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=16)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--module-regex", default=None)
    p.add_argument("--only-changed", action="store_true", default=True)
    p.add_argument("--include-unchanged", action="store_true")
    return p


def _is_changed_case(row: dict[str, str]) -> bool:
    for k in ("changed_files", "added_loc", "deleted_loc", "modified_loc"):
        try:
            if int(row.get(k) or 0) > 0:
                return True
        except ValueError:
            pass
    return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    csv_path = Path(args.csv).resolve()
    repo = Path(args.repo).resolve()
    out_root = Path(args.out_dir).resolve()

    # quick checks
    _run_git(repo, ["rev-parse", "--verify", args.old_ref], check=True)
    _run_git(repo, ["rev-parse", "--verify", args.new_ref], check=True)

    rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8")))
    selected: list[tuple[int, str]] = []

    rgx = re.compile(args.module_regex) if args.module_regex else None

    for i, row in enumerate(rows, start=1):
        mp = (row.get("module_path") or "").strip()
        if not mp:
            continue
        try:
            src_count = int(row.get("source_file_count") or 0)
        except ValueError:
            src_count = 0
        if src_count <= 0:
            continue
        if not args.include_unchanged and args.only_changed and not _is_changed_case(row):
            continue
        if rgx and not rgx.search(mp):
            continue
        selected.append((i, mp))

    if args.limit and args.limit > 0:
        selected = selected[: args.limit]

    run_dir = out_root / f"removed_added_impls__{args.old_ref}__{args.new_ref}__{now_timestamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "csv": str(csv_path),
        "repo": str(repo),
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "jobs": args.jobs,
        "total_csv_rows": len(rows),
        "selected_cases": len(selected),
        "only_changed": bool(args.only_changed and not args.include_unchanged),
    }
    write_json(run_dir / "meta.json", meta)

    out_csv = run_dir / "per_module_removed_added_impls.csv"

    stats = {
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "modules_with_removed_funcs": 0,
        "modules_with_added_funcs": 0,
        "modules_with_both": 0,
        "total_removed_funcs": 0,
        "total_added_funcs": 0,
    }

    t0 = time.time()
    done = 0

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "idx",
                "module_path",
                "status",
                "changed_source_files",
                "removed_func_count",
                "added_func_count",
                "has_removed_and_added",
                "removed_funcs",
                "added_funcs",
                "error",
            ],
        )
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = {ex.submit(_eval_one, idx, mp, repo, args.old_ref, args.new_ref): (idx, mp) for idx, mp in selected}
            for fut in as_completed(futs):
                res = fut.result()
                done += 1

                if res.status == "ok":
                    stats["ok"] += 1
                    if res.removed_funcs:
                        stats["modules_with_removed_funcs"] += 1
                    if res.added_funcs:
                        stats["modules_with_added_funcs"] += 1
                    if res.removed_funcs and res.added_funcs:
                        stats["modules_with_both"] += 1
                    stats["total_removed_funcs"] += len(res.removed_funcs)
                    stats["total_added_funcs"] += len(res.added_funcs)
                elif res.status.startswith("skipped"):
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1

                writer.writerow(
                    {
                        "idx": res.idx,
                        "module_path": res.module_path,
                        "status": res.status,
                        "changed_source_files": res.changed_source_files,
                        "removed_func_count": len(res.removed_funcs),
                        "added_func_count": len(res.added_funcs),
                        "has_removed_and_added": str(bool(res.removed_funcs and res.added_funcs)).lower(),
                        "removed_funcs": json.dumps(sorted(res.removed_funcs), ensure_ascii=False),
                        "added_funcs": json.dumps(sorted(res.added_funcs), ensure_ascii=False),
                        "error": res.error,
                    }
                )
                f.flush()

                elapsed = max(1e-6, time.time() - t0)
                rate = done / elapsed
                eta_sec = (len(selected) - done) / rate if rate > 0 else 0.0
                print(
                    f"[progress] {done}/{len(selected)} module={res.module_path} status={res.status} "
                    f"removed={len(res.removed_funcs)} added={len(res.added_funcs)} rate={rate:.2f}/s eta={eta_sec/60:.1f}m",
                    flush=True,
                )

    summary = {
        **meta,
        **stats,
        "ratio_modules_with_both_over_ok": (stats["modules_with_both"] / stats["ok"]) if stats["ok"] else 0.0,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "summary.json", summary)
    print("[done]", json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
