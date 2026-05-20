#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.definition_locator import run_definition_locator
from driver_agent_pregen.impact_graph_builder import run_impact_graph_builder
from driver_agent_pregen.kernel_diff_builder import run_kernel_diff_builder
from driver_agent_pregen.module_source_loader import run_module_source_loader
from driver_agent_pregen.repo_utils import RepositoryAccessor
from driver_agent_pregen.symbol_scanner import (  # private helpers reused intentionally
    _extract_c_like_functions,
    _extract_rust_functions,
    _strip_comments,
    run_symbol_scanner,
)
from driver_agent_pregen.utils import now_timestamp, slugify, write_json

SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}


@dataclass
class CaseResult:
    idx: int
    module_path: str
    status: str
    affected: set[tuple[str, str]]
    modified_non_global_raw: set[tuple[str, str]]
    modified_non_global: set[tuple[str, str]]
    modified_global_files: set[str]
    missing: set[tuple[str, str]]
    extra: set[tuple[str, str]]
    filtered_comment_only: set[tuple[str, str]]
    error: str = ""


@dataclass
class EvalConfig:
    repo: Path
    old_ref: str
    new_ref: str
    work_root: Path
    keep_case_artifacts: bool
    collect_commit_evidence: bool
    affected_func_signal_mode: str
    allowed_symbol_kinds: Optional[set[str]]
    scan_changed_files_only: bool
    semantic_obligation_table: Optional[str]
    ignore_comment_only_function_changes: bool


def _parse_diff_changed_lines(diff_text: str) -> dict[str, dict[str, set[int]]]:
    out: dict[str, dict[str, set[int]]] = {}
    cur_old_path = None
    cur_new_path = None
    old_line = None
    new_line = None

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git a/"):
            cur_old_path = None
            cur_new_path = None
            old_line = None
            new_line = None
            continue

        if raw.startswith("--- "):
            token = raw[4:].strip()
            if token.startswith("a/"):
                cur_old_path = token[2:]
            elif token == "/dev/null":
                cur_old_path = None
            else:
                cur_old_path = token
            continue

        if raw.startswith("+++ "):
            token = raw[4:].strip()
            if token.startswith("b/"):
                cur_new_path = token[2:]
            elif token == "/dev/null":
                cur_new_path = None
            else:
                cur_new_path = token
            continue

        if raw.startswith("@@ "):
            try:
                # @@ -a,b +c,d @@
                parts = raw.split("@@", 2)[1].strip().split(" ")
                old_part = parts[0]
                new_part = parts[1]
                old_line = int(old_part.split(",")[0].lstrip("-"))
                new_line = int(new_part.split(",")[0].lstrip("+"))
            except Exception:
                old_line = None
                new_line = None
            continue

        if old_line is None or new_line is None:
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue

        if raw.startswith(" "):
            old_line += 1
            new_line += 1
            continue

        if raw.startswith("-"):
            if cur_old_path:
                file_rec = out.setdefault(cur_old_path, {"old": set(), "new": set()})
                file_rec["old"].add(old_line)
            old_line += 1
            continue

        if raw.startswith("+"):
            if cur_new_path:
                file_rec = out.setdefault(cur_new_path, {"old": set(), "new": set()})
                file_rec["new"].add(new_line)
            new_line += 1
            continue

    return out


def _extract_functions(text: str, rel_file: str) -> list[tuple[str, int, int]]:
    suffix = Path(rel_file).suffix.lower()
    if suffix == ".rs":
        funcs = _extract_rust_functions(text, rel_file)
    elif suffix in SOURCE_SUFFIXES:
        funcs = _extract_c_like_functions(text, rel_file)
    else:
        return []

    out: list[tuple[str, int, int]] = []
    for fn in funcs:
        out.append((fn.name, int(fn.start_line), int(fn.end_line)))
    return out


def _normalize_semantic_text(code: str) -> str:
    code = _strip_comments(code)
    code = re.sub(r"\s+", "", code)
    return code


def _extract_function_semantic_map(text: str, rel_file: str) -> dict[str, set[str]]:
    suffix = Path(rel_file).suffix.lower()
    if suffix == ".rs":
        funcs = _extract_rust_functions(text, rel_file)
    elif suffix in SOURCE_SUFFIXES:
        funcs = _extract_c_like_functions(text, rel_file)
    else:
        return {}

    out: dict[str, set[str]] = {}
    for fn in funcs:
        norm = _normalize_semantic_text(fn.body)
        out.setdefault(fn.name, set()).add(norm)
    return out


def _is_comment_only_function_change(
    rel_file: str,
    fn_name: str,
    old_text: Optional[str],
    new_text: Optional[str],
    old_sem_cache: dict[str, dict[str, set[str]]],
    new_sem_cache: dict[str, dict[str, set[str]]],
) -> bool:
    if old_text is None or new_text is None:
        return False

    if rel_file not in old_sem_cache:
        old_sem_cache[rel_file] = _extract_function_semantic_map(old_text, rel_file)
    if rel_file not in new_sem_cache:
        new_sem_cache[rel_file] = _extract_function_semantic_map(new_text, rel_file)

    old_sigs = old_sem_cache.get(rel_file, {}).get(fn_name)
    new_sigs = new_sem_cache.get(rel_file, {}).get(fn_name)
    if not old_sigs or not new_sigs:
        return False

    return old_sigs == new_sigs


def _map_lines_to_functions(
    line_set: set[int],
    funcs: list[tuple[str, int, int]],
) -> tuple[set[str], bool]:
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


def _set_to_json_array(items: set[tuple[str, str]]) -> str:
    arr = [f"{p}:{f}" for p, f in sorted(items)]
    return json.dumps(arr, ensure_ascii=False)


def _filter_driver_symbols(symbols_payload: dict[str, Any], allowed_kinds: Optional[set[str]]) -> dict[str, Any]:
    if not allowed_kinds:
        return symbols_payload
    symbols = symbols_payload.get("symbols", [])
    filtered = [s for s in symbols if str(s.get("kind", "unknown")) in allowed_kinds]
    return {"symbols": filtered}


def _eval_one_case(idx: int, row: dict[str, str], cfg: EvalConfig) -> CaseResult:
    module_path = row["module_path"].strip()
    case_slug = f"{idx:05d}_{slugify(module_path.replace('/', '_'))}"
    case_dir = cfg.work_root / case_slug
    extracted_dir = case_dir / "extracted"

    accessor = RepositoryAccessor.from_args(
        repo=str(cfg.repo),
        old_ref=cfg.old_ref,
        new_ref=cfg.new_ref,
        old_tree=None,
        new_tree=None,
    )

    try:
        diff_text = accessor.refs_diff(pathspecs=[module_path], max_chars=8_000_000)
        changed = _parse_diff_changed_lines(diff_text)
        changed_source_files = [
            p for p in changed.keys() if Path(p).suffix.lower() in SOURCE_SUFFIXES
        ]
        if not changed_source_files:
            return CaseResult(
                idx=idx,
                module_path=module_path,
                status="skipped_no_source_file_change",
                affected=set(),
                modified_non_global_raw=set(),
                modified_non_global=set(),
                modified_global_files=set(),
                missing=set(),
                extra=set(),
                filtered_comment_only=set(),
            )

        restrict_files = set(changed_source_files) if cfg.scan_changed_files_only else None
        module_result = run_module_source_loader(
            accessor,
            module_path=module_path,
            extracted_dir=extracted_dir,
            restrict_files_old=restrict_files,
        )
        if not module_result.old_exists or not module_result.source_files_old:
            return CaseResult(
                idx=idx,
                module_path=module_path,
                status="skipped_no_old_sources",
                affected=set(),
                modified_non_global_raw=set(),
                modified_non_global=set(),
                modified_global_files=set(),
                missing=set(),
                extra=set(),
                filtered_comment_only=set(),
            )

        scan_payload = run_symbol_scanner(module_result, extracted_dir)
        filtered_symbols = _filter_driver_symbols(scan_payload["driver_used_symbols"], cfg.allowed_symbol_kinds)
        locations = run_definition_locator(
            accessor,
            extracted_dir,
            filtered_symbols,
            module_path=module_path,
        )
        kernel_diffs = run_kernel_diff_builder(
            accessor,
            extracted_dir,
            locations,
            module_path,
            collect_commits=cfg.collect_commit_evidence,
            affected_func_signal_mode=cfg.affected_func_signal_mode,
            old_ref=cfg.old_ref,
            new_ref=cfg.new_ref,
            semantic_obligation_table=cfg.semantic_obligation_table,
        )
        impact = run_impact_graph_builder(module_result, extracted_dir, scan_payload, kernel_diffs)

        affected: set[tuple[str, str]] = set()
        for afn in impact.get("affected_functions", []):
            f = afn.get("function")
            p = afn.get("file")
            if f and p:
                affected.add((p, f))

        modified_non_global: set[tuple[str, str]] = set()
        modified_global_files: set[str] = set()
        old_text_cache: dict[str, Optional[str]] = {}
        new_text_cache: dict[str, Optional[str]] = {}
        old_sem_cache: dict[str, dict[str, set[str]]] = {}
        new_sem_cache: dict[str, dict[str, set[str]]] = {}

        for rel_file, lines in changed.items():
            old_lines = lines.get("old", set())
            new_lines = lines.get("new", set())

            if old_lines and accessor.path_exists("old", rel_file):
                old_text = accessor.read_file("old", rel_file)
                old_text_cache[rel_file] = old_text
                old_funcs = _extract_functions(old_text, rel_file)
                fn_hits, has_global = _map_lines_to_functions(old_lines, old_funcs)
                for fn in fn_hits:
                    modified_non_global.add((rel_file, fn))
                if has_global:
                    modified_global_files.add(rel_file)
            elif old_lines:
                old_text_cache[rel_file] = None
                modified_global_files.add(rel_file)

            if new_lines and accessor.path_exists("new", rel_file):
                new_text = accessor.read_file("new", rel_file)
                new_text_cache[rel_file] = new_text
                new_funcs = _extract_functions(new_text, rel_file)
                fn_hits, has_global = _map_lines_to_functions(new_lines, new_funcs)
                for fn in fn_hits:
                    modified_non_global.add((rel_file, fn))
                if has_global:
                    modified_global_files.add(rel_file)
            elif new_lines:
                new_text_cache[rel_file] = None
                modified_global_files.add(rel_file)

        filtered_comment_only: set[tuple[str, str]] = set()
        if cfg.ignore_comment_only_function_changes and modified_non_global:
            for rel_file, fn_name in sorted(modified_non_global):
                old_text = old_text_cache.get(rel_file)
                new_text = new_text_cache.get(rel_file)
                if old_text is None and accessor.path_exists("old", rel_file):
                    old_text = accessor.read_file("old", rel_file)
                    old_text_cache[rel_file] = old_text
                if new_text is None and accessor.path_exists("new", rel_file):
                    new_text = accessor.read_file("new", rel_file)
                    new_text_cache[rel_file] = new_text
                if _is_comment_only_function_change(
                    rel_file,
                    fn_name,
                    old_text,
                    new_text,
                    old_sem_cache,
                    new_sem_cache,
                ):
                    filtered_comment_only.add((rel_file, fn_name))
            if filtered_comment_only:
                modified_non_global -= filtered_comment_only
        modified_non_global_raw = modified_non_global | filtered_comment_only

        missing = modified_non_global - affected
        extra = affected - modified_non_global

        return CaseResult(
            idx=idx,
            module_path=module_path,
            status="ok",
            affected=affected,
            modified_non_global_raw=modified_non_global_raw,
            modified_non_global=modified_non_global,
            modified_global_files=modified_global_files,
            missing=missing,
            extra=extra,
            filtered_comment_only=filtered_comment_only,
        )

    except Exception as exc:
        return CaseResult(
            idx=idx,
            module_path=module_path,
            status="failed",
            affected=set(),
            modified_non_global_raw=set(),
            modified_non_global=set(),
            modified_global_files=set(),
            missing=set(),
            extra=set(),
            filtered_comment_only=set(),
            error=f"{exc}\n{traceback.format_exc()}",
        )
    finally:
        if not cfg.keep_case_artifacts:
            shutil.rmtree(case_dir, ignore_errors=True)


def _is_changed_case(row: dict[str, str]) -> bool:
    keys = ["changed_files", "added_loc", "deleted_loc", "modified_loc"]
    for k in keys:
        try:
            if int(row.get(k) or 0) > 0:
                return True
        except ValueError:
            pass
    return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evaluate affected_functions coverage against actual modified functions")
    p.add_argument("--csv", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--module-regex", default=None)
    p.add_argument("--only-changed", action="store_true", default=True)
    p.add_argument("--include-unchanged", action="store_true", help="override --only-changed")
    p.add_argument("--keep-case-artifacts", action="store_true")
    p.set_defaults(collect_commit_evidence=True)
    p.add_argument(
        "--collect-commit-evidence",
        dest="collect_commit_evidence",
        action="store_true",
        help="collect per-symbol git log/commit evidence in kernel diff builder (default: enabled)",
    )
    p.add_argument(
        "--no-collect-commit-evidence",
        dest="collect_commit_evidence",
        action="store_false",
        help="disable commit evidence collection for faster but non-equivalent runs",
    )
    p.add_argument(
        "--max-subprocs",
        type=int,
        default=0,
        help="global subprocess concurrency cap; 0 means auto (resource-safe default)",
    )
    p.add_argument(
        "--git-grep-threads",
        type=int,
        default=0,
        help="thread count for each git grep; 0 means auto (resource-safe default)",
    )
    p.add_argument(
        "--symbol-kinds",
        default="",
        help="comma-separated kinds to query in kernel locator/diff; empty means ALL kinds (default)",
    )
    p.add_argument(
        "--scan-changed-files-only",
        action="store_true",
        help="speed mode for eval: scan only changed source files in module (may reduce completeness)",
    )
    p.add_argument(
        "--affected-func-signal-mode",
        type=str,
        choices=["kernel_only", "kernel_plus_other_drivers"],
        default="kernel_only",
        help=(
            "signal source mode for affected-functions: "
            "kernel_only (strict kernel-side, default) or "
            "kernel_plus_other_drivers (opt-in, allow non-target drivers hints)"
        ),
    )
    p.add_argument(
        "--semantic-obligation-table",
        default=None,
        help="optional semantic table JSON to enrich kernel diff evidence for affected-function detection",
    )
    p.set_defaults(ignore_comment_only_function_changes=True)
    p.add_argument(
        "--ignore-comment-only-function-changes",
        dest="ignore_comment_only_function_changes",
        action="store_true",
        help="exclude function-level changes that are comments/whitespace only (default: enabled)",
    )
    p.add_argument(
        "--no-ignore-comment-only-function-changes",
        dest="ignore_comment_only_function_changes",
        action="store_false",
        help="count comment-only function changes in modified function set",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    csv_path = Path(args.csv).resolve()
    repo_path = Path(args.repo).resolve()
    out_root = Path(args.out_dir).resolve()

    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    ts = now_timestamp()
    run_dir = out_root / f"affected_eval__{args.old_ref}__{args.new_ref}__{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = run_dir / "case_work"
    work_root.mkdir(parents=True, exist_ok=True)

    max_subprocs = int(args.max_subprocs or 0)
    if max_subprocs <= 0:
        # Keep enough parallelism, but avoid oversubscribing IO-heavy git subprocesses.
        max_subprocs = max(8, min(24, int(args.jobs) * 2))
    os.environ["DRIVER_AGENT_MAX_SUBPROCS"] = str(max_subprocs)

    git_grep_threads = int(args.git_grep_threads or 0)
    if git_grep_threads <= 0:
        # Prevent N(worker) * N(git-grep internal threads) oversubscription.
        git_grep_threads = 1 if int(args.jobs) >= 8 else 2
    os.environ["DRIVER_AGENT_GIT_GREP_THREADS"] = str(git_grep_threads)

    accessor = RepositoryAccessor.from_args(
        repo=str(repo_path),
        old_ref=args.old_ref,
        new_ref=args.new_ref,
        old_tree=None,
        new_tree=None,
    )
    accessor.validate()

    with csv_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    filtered: list[tuple[int, dict[str, str]]] = []
    for i, row in enumerate(rows, start=1):
        module_path = (row.get("module_path") or "").strip()
        if not module_path:
            continue
        try:
            src_count = int(row.get("source_file_count") or 0)
        except ValueError:
            src_count = 0
        if src_count <= 0:
            continue
        if not args.include_unchanged and args.only_changed and not _is_changed_case(row):
            continue
        if args.module_regex:
            import re

            if not re.search(args.module_regex, module_path):
                continue
        filtered.append((i, row))

    if args.limit and args.limit > 0:
        filtered = filtered[: args.limit]

    parsed_symbol_kinds = {
        k.strip() for k in str(args.symbol_kinds or "").split(",") if k.strip()
    }
    allowed_symbol_kinds: Optional[set[str]] = parsed_symbol_kinds or None
    symbol_kinds_meta = sorted(allowed_symbol_kinds) if allowed_symbol_kinds else ["ALL"]
    semantic_obligation_table = (
        args.semantic_obligation_table
        if args.affected_func_signal_mode == "kernel_plus_other_drivers"
        else None
    )

    meta = {
        "csv": str(csv_path),
        "repo": str(repo_path),
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "jobs": args.jobs,
        "max_subprocs": max_subprocs,
        "git_grep_threads": git_grep_threads,
        "collect_commit_evidence": bool(args.collect_commit_evidence),
        "affected_func_signal_mode": args.affected_func_signal_mode,
        "semantic_obligation_table": semantic_obligation_table,
        "semantic_obligation_table_ignored": (
            {
                "path": args.semantic_obligation_table,
                "reason": "affected_func_signal_mode=kernel_only uses kernel-side evidence only",
            }
            if args.semantic_obligation_table and semantic_obligation_table is None
            else None
        ),
        "allowed_symbol_kinds": symbol_kinds_meta,
        "scan_changed_files_only": bool(args.scan_changed_files_only),
        "total_csv_rows": len(rows),
        "selected_cases": len(filtered),
        "only_changed": bool(args.only_changed and not args.include_unchanged),
        "ignore_comment_only_function_changes": bool(args.ignore_comment_only_function_changes),
    }
    write_json(run_dir / "meta.json", meta)
    print(
        f"[start] selected_cases={len(filtered)} jobs={args.jobs} "
        f"max_subprocs={max_subprocs} git_grep_threads={git_grep_threads} "
        f"symbol_kinds={','.join(symbol_kinds_meta)}",
        flush=True,
    )

    per_case_path = run_dir / "per_case_results.csv"
    with per_case_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "idx",
                "module_path",
                "status",
                "affected_count",
                "modified_non_global_raw_count",
                "modified_non_global_count",
                "modified_global_file_count",
                "overlap_count",
                "missing_count",
                "extra_count",
                "coverage_all_modified_non_global",
                "filtered_comment_only_count",
                "filtered_comment_only_functions",
                "missing_functions",
                "extra_functions",
                "error",
            ],
        )
        writer.writeheader()

        cfg = EvalConfig(
            repo=repo_path,
            old_ref=args.old_ref,
            new_ref=args.new_ref,
            work_root=work_root,
            keep_case_artifacts=args.keep_case_artifacts,
            collect_commit_evidence=bool(args.collect_commit_evidence),
            affected_func_signal_mode=str(args.affected_func_signal_mode),
            allowed_symbol_kinds=allowed_symbol_kinds,
            scan_changed_files_only=bool(args.scan_changed_files_only),
            semantic_obligation_table=semantic_obligation_table,
            ignore_comment_only_function_changes=bool(args.ignore_comment_only_function_changes),
        )

        t0 = time.time()
        done = 0
        agg = {
            "ok_cases": 0,
            "failed_cases": 0,
            "skipped_cases": 0,
            "cases_with_modified_non_global": 0,
            "cases_fully_covered_non_global": 0,
            "total_affected_functions": 0,
            "total_modified_non_global_raw_functions": 0,
            "total_modified_non_global_functions": 0,
            "total_missing_non_global_functions": 0,
            "total_extra_affected_functions": 0,
            "total_modified_global_files": 0,
            "total_filtered_comment_only_functions": 0,
        }

        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            fut_map = {ex.submit(_eval_one_case, idx, row, cfg): (idx, row) for idx, row in filtered}
            print(f"[start] submitted_futures={len(fut_map)}", flush=True)
            for fut in as_completed(fut_map):
                res = fut.result()
                done += 1

                overlap = len(res.affected & res.modified_non_global)
                coverage = len(res.missing) == 0

                if res.status == "ok":
                    agg["ok_cases"] += 1
                    agg["total_affected_functions"] += len(res.affected)
                    agg["total_modified_non_global_raw_functions"] += len(res.modified_non_global_raw)
                    agg["total_modified_non_global_functions"] += len(res.modified_non_global)
                    agg["total_missing_non_global_functions"] += len(res.missing)
                    agg["total_extra_affected_functions"] += len(res.extra)
                    agg["total_modified_global_files"] += len(res.modified_global_files)
                    agg["total_filtered_comment_only_functions"] += len(res.filtered_comment_only)
                    if res.modified_non_global:
                        agg["cases_with_modified_non_global"] += 1
                        if coverage:
                            agg["cases_fully_covered_non_global"] += 1
                elif res.status.startswith("skipped"):
                    agg["skipped_cases"] += 1
                else:
                    agg["failed_cases"] += 1

                writer.writerow(
                    {
                        "idx": res.idx,
                        "module_path": res.module_path,
                        "status": res.status,
                        "affected_count": len(res.affected),
                        "modified_non_global_raw_count": len(res.modified_non_global_raw),
                        "modified_non_global_count": len(res.modified_non_global),
                        "modified_global_file_count": len(res.modified_global_files),
                        "overlap_count": overlap,
                        "missing_count": len(res.missing),
                        "extra_count": len(res.extra),
                        "coverage_all_modified_non_global": str(coverage).lower(),
                        "filtered_comment_only_count": len(res.filtered_comment_only),
                        "filtered_comment_only_functions": _set_to_json_array(res.filtered_comment_only),
                        "missing_functions": _set_to_json_array(res.missing),
                        "extra_functions": _set_to_json_array(res.extra),
                        "error": res.error[:2000],
                    }
                )
                f.flush()

                elapsed = max(1e-6, time.time() - t0)
                rate = done / elapsed
                eta = (len(filtered) - done) / rate if rate > 0 else 0.0
                print(
                    f"[progress] {done}/{len(filtered)} module={res.module_path} status={res.status} "
                    f"missing={len(res.missing)} extra={len(res.extra)} rate={rate:.2f}/s eta={eta/60:.1f}m",
                    flush=True,
                )

    coverage_ratio = 0.0
    if agg["cases_with_modified_non_global"] > 0:
        coverage_ratio = agg["cases_fully_covered_non_global"] / float(agg["cases_with_modified_non_global"])

    summary = {
        **meta,
        **agg,
        "coverage_ratio_non_global": coverage_ratio,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "summary.json", summary)

    print("[done]", json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
