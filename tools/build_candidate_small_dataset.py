#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.symbol_scanner import _extract_c_like_functions, _extract_rust_functions
from driver_agent_pregen.utils import now_timestamp, write_json

SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}


@dataclass
class ModuleAnalysis:
    idx: int
    module_path: str
    status: str
    changed_source_files: int
    modified_non_global_funcs: set[str]
    modified_global_files: set[str]
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
    return [(f.name, int(f.start_line), int(f.end_line)) for f in fs]


def _map_lines_to_functions(line_set: set[int], funcs: list[tuple[str, int, int]]) -> tuple[set[str], bool]:
    hits: set[str] = set()
    has_global = False
    if not line_set:
        return hits, has_global

    for ln in line_set:
        matched = False
        for name, start, end in funcs:
            if start <= ln <= end:
                hits.add(name)
                matched = True
                break
        if not matched:
            has_global = True
    return hits, has_global


def _hit_funcs(line_set: set[int], funcs: list[tuple[str, int, int]]) -> set[str]:
    hits = set()
    if not line_set:
        return hits
    for name, start, end in funcs:
        for ln in line_set:
            if start <= ln <= end:
                hits.add(name)
                break
    return hits


def _is_changed_case(row: dict[str, str]) -> bool:
    for k in ("changed_files", "added_loc", "deleted_loc", "modified_loc"):
        try:
            if int(row.get(k) or 0) > 0:
                return True
        except ValueError:
            pass
    return False


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _quantiles(values: list[int]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)

    def pct(p: float) -> int:
        idx = int(round((p / 100.0) * (n - 1)))
        idx = max(0, min(n - 1, idx))
        return s[idx]

    return {
        "count": n,
        "min": s[0],
        "p25": pct(25),
        "p50": pct(50),
        "p75": pct(75),
        "p90": pct(90),
        "p95": pct(95),
        "p99": pct(99),
        "max": s[-1],
        "mean": (sum(s) / n),
    }


def _analyze_one(idx: int, module_path: str, repo: Path, old_ref: str, new_ref: str) -> ModuleAnalysis:
    try:
        diff = _run_git(repo, ["diff", "--no-color", "--unified=0", old_ref, new_ref, "--", module_path], check=False).stdout
        changed = _parse_diff_changed_lines(diff)
        changed_src = [p for p in changed.keys() if Path(p).suffix.lower() in SOURCE_SUFFIXES]
        if not changed_src:
            return ModuleAnalysis(
                idx=idx,
                module_path=module_path,
                status="skipped_no_source_change",
                changed_source_files=0,
                modified_non_global_funcs=set(),
                modified_global_files=set(),
                removed_funcs=set(),
                added_funcs=set(),
            )

        modified_non_global_funcs: set[str] = set()
        modified_global_files: set[str] = set()
        removed_funcs: set[str] = set()
        added_funcs: set[str] = set()

        for rel in sorted(set(changed_src)):
            lines_old = changed.get(rel, {}).get("old", set())
            lines_new = changed.get(rel, {}).get("new", set())

            old_txt = _read_file(repo, old_ref, rel) if _path_exists(repo, old_ref, rel) else None
            new_txt = _read_file(repo, new_ref, rel) if _path_exists(repo, new_ref, rel) else None

            old_defs = _extract_functions(old_txt or "", rel) if old_txt is not None else []
            new_defs = _extract_functions(new_txt or "", rel) if new_txt is not None else []

            old_hits, old_has_global = _map_lines_to_functions(lines_old, old_defs)
            new_hits, new_has_global = _map_lines_to_functions(lines_new, new_defs)

            for fn in old_hits:
                modified_non_global_funcs.add(f"{rel}:{fn}")
            for fn in new_hits:
                modified_non_global_funcs.add(f"{rel}:{fn}")
            if old_has_global or new_has_global:
                modified_global_files.add(rel)

            old_names = {n for n, _, _ in old_defs}
            new_names = {n for n, _, _ in new_defs}
            removed_candidates = old_names - new_names
            added_candidates = new_names - old_names

            old_map = {n: (s, e) for n, s, e in old_defs}
            new_map = {n: (s, e) for n, s, e in new_defs}

            old_hit = _hit_funcs(lines_old, [(n, *old_map[n]) for n in removed_candidates if n in old_map])
            new_hit = _hit_funcs(lines_new, [(n, *new_map[n]) for n in added_candidates if n in new_map])

            for fn in old_hit:
                removed_funcs.add(f"{rel}:{fn}")
            for fn in new_hit:
                added_funcs.add(f"{rel}:{fn}")

        return ModuleAnalysis(
            idx=idx,
            module_path=module_path,
            status="ok",
            changed_source_files=len(changed_src),
            modified_non_global_funcs=modified_non_global_funcs,
            modified_global_files=modified_global_files,
            removed_funcs=removed_funcs,
            added_funcs=added_funcs,
        )
    except Exception as exc:
        return ModuleAnalysis(
            idx=idx,
            module_path=module_path,
            status="failed",
            changed_source_files=0,
            modified_non_global_funcs=set(),
            modified_global_files=set(),
            removed_funcs=set(),
            added_funcs=set(),
            error=str(exc),
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build candidate_small dataset (modified_loc <= threshold) with function-level stats")
    p.add_argument("--csv", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=16)
    p.add_argument("--min-modified-loc", type=int, default=1)
    p.add_argument("--max-modified-loc", type=int, default=50)
    p.add_argument("--include-unchanged", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    csv_path = Path(args.csv).resolve()
    repo = Path(args.repo).resolve()
    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    _run_git(repo, ["rev-parse", "--verify", args.old_ref], check=True)
    _run_git(repo, ["rev-parse", "--verify", args.new_ref], check=True)

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        input_fields = list(reader.fieldnames or [])
        all_rows = list(reader)

    selected_rows: list[tuple[int, dict[str, str]]] = []
    for idx, row in enumerate(all_rows, start=1):
        module_path = (row.get("module_path") or "").strip()
        if not module_path:
            continue
        if _to_int(row.get("source_file_count"), 0) <= 0:
            continue
        if not args.include_unchanged and not _is_changed_case(row):
            continue
        mod_loc = _to_int(row.get("modified_loc"), 0)
        if mod_loc < args.min_modified_loc or mod_loc > args.max_modified_loc:
            continue
        selected_rows.append((idx, row))

    ts = now_timestamp()
    run_dir = out_root / f"candidate_small__{args.old_ref}__{args.new_ref}__{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "csv": str(csv_path),
        "repo": str(repo),
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "jobs": args.jobs,
        "min_modified_loc": args.min_modified_loc,
        "max_modified_loc": args.max_modified_loc,
        "include_unchanged": bool(args.include_unchanged),
        "total_csv_rows": len(all_rows),
        "selected_candidates": len(selected_rows),
    }
    write_json(run_dir / "meta.json", meta)

    candidate_csv = run_dir / "candidate_small_modules.csv"
    with candidate_csv.open("w", encoding="utf-8", newline="") as f:
        fields = ["source_row_idx"] + input_fields
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for idx, row in selected_rows:
            out = {"source_row_idx": idx}
            out.update(row)
            w.writerow(out)

    analysis_csv = run_dir / "candidate_small_analysis.csv"
    stats = {
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "modules_with_removed_funcs": 0,
        "modules_with_added_funcs": 0,
        "modules_with_removed_and_added": 0,
        "total_modified_non_global_funcs": 0,
        "total_removed_funcs": 0,
        "total_added_funcs": 0,
        "total_modified_global_files": 0,
    }
    modified_loc_vals: list[int] = []
    modified_func_count_vals: list[int] = []
    removed_count_vals: list[int] = []
    added_count_vals: list[int] = []
    fn_name_counter: Counter[str] = Counter()

    t0 = time.time()
    done = 0

    with analysis_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source_row_idx",
                "module_path",
                "status",
                "modified_loc",
                "code_loc",
                "changed_files",
                "source_file_count",
                "changed_source_files",
                "modified_non_global_func_count",
                "modified_global_file_count",
                "removed_func_count",
                "added_func_count",
                "has_removed_and_added",
                "modified_non_global_funcs",
                "modified_global_files",
                "removed_funcs",
                "added_funcs",
                "error",
            ],
        )
        writer.writeheader()

        row_by_module = {row["module_path"]: (idx, row) for idx, row in selected_rows if row.get("module_path")}
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = {
                ex.submit(_analyze_one, idx, row["module_path"], repo, args.old_ref, args.new_ref): (idx, row)
                for idx, row in selected_rows
            }
            for fut in as_completed(futs):
                res = fut.result()
                done += 1

                idx, src_row = row_by_module.get(res.module_path, (res.idx, {}))
                mod_loc = _to_int(src_row.get("modified_loc"), 0)
                code_loc = _to_int(src_row.get("code_loc"), 0)
                changed_files = _to_int(src_row.get("changed_files"), 0)
                src_cnt = _to_int(src_row.get("source_file_count"), 0)

                if res.status == "ok":
                    stats["ok"] += 1
                    if res.removed_funcs:
                        stats["modules_with_removed_funcs"] += 1
                    if res.added_funcs:
                        stats["modules_with_added_funcs"] += 1
                    if res.removed_funcs and res.added_funcs:
                        stats["modules_with_removed_and_added"] += 1
                    stats["total_modified_non_global_funcs"] += len(res.modified_non_global_funcs)
                    stats["total_removed_funcs"] += len(res.removed_funcs)
                    stats["total_added_funcs"] += len(res.added_funcs)
                    stats["total_modified_global_files"] += len(res.modified_global_files)
                    modified_loc_vals.append(mod_loc)
                    modified_func_count_vals.append(len(res.modified_non_global_funcs))
                    removed_count_vals.append(len(res.removed_funcs))
                    added_count_vals.append(len(res.added_funcs))
                    for item in res.modified_non_global_funcs:
                        fn_name_counter[item.split(":")[-1]] += 1
                elif res.status.startswith("skipped"):
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1

                writer.writerow(
                    {
                        "source_row_idx": idx,
                        "module_path": res.module_path,
                        "status": res.status,
                        "modified_loc": mod_loc,
                        "code_loc": code_loc,
                        "changed_files": changed_files,
                        "source_file_count": src_cnt,
                        "changed_source_files": res.changed_source_files,
                        "modified_non_global_func_count": len(res.modified_non_global_funcs),
                        "modified_global_file_count": len(res.modified_global_files),
                        "removed_func_count": len(res.removed_funcs),
                        "added_func_count": len(res.added_funcs),
                        "has_removed_and_added": str(bool(res.removed_funcs and res.added_funcs)).lower(),
                        "modified_non_global_funcs": json.dumps(sorted(res.modified_non_global_funcs), ensure_ascii=False),
                        "modified_global_files": json.dumps(sorted(res.modified_global_files), ensure_ascii=False),
                        "removed_funcs": json.dumps(sorted(res.removed_funcs), ensure_ascii=False),
                        "added_funcs": json.dumps(sorted(res.added_funcs), ensure_ascii=False),
                        "error": res.error,
                    }
                )
                f.flush()

                elapsed = max(1e-6, time.time() - t0)
                rate = done / elapsed
                eta = (len(selected_rows) - done) / rate if rate > 0 else 0.0
                print(
                    f"[progress] {done}/{len(selected_rows)} module={res.module_path} status={res.status} "
                    f"mod_funcs={len(res.modified_non_global_funcs)} removed={len(res.removed_funcs)} "
                    f"added={len(res.added_funcs)} rate={rate:.2f}/s eta={eta/60:.1f}m",
                    flush=True,
                )

    top_fn_names = [{"function": name, "count": count} for name, count in fn_name_counter.most_common(200)]
    write_json(
        run_dir / "modified_function_stats.json",
        {
            "top_modified_function_names": top_fn_names,
            "modified_loc_quantiles": _quantiles(modified_loc_vals),
            "modified_non_global_func_count_quantiles": _quantiles(modified_func_count_vals),
            "removed_func_count_quantiles": _quantiles(removed_count_vals),
            "added_func_count_quantiles": _quantiles(added_count_vals),
        },
    )

    summary = {
        **meta,
        **stats,
        "modified_loc_quantiles": _quantiles(modified_loc_vals),
        "modified_non_global_func_count_quantiles": _quantiles(modified_func_count_vals),
        "removed_func_count_quantiles": _quantiles(removed_count_vals),
        "added_func_count_quantiles": _quantiles(added_count_vals),
        "ratio_modules_with_removed_and_added_over_ok": (
            stats["modules_with_removed_and_added"] / stats["ok"] if stats["ok"] else 0.0
        ),
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "summary.json", summary)

    print("[done]", json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

