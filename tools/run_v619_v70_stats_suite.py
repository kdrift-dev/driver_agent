#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CASES_CSV = "/mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv"
DEFAULT_REPO = "/mnt/disk7/jw_ai4os/linux_source_code/linux.git"
DEFAULT_OLD_REF = "v6.19"
DEFAULT_NEW_REF = "v7.0-rc1"


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_changed_case(row: dict[str, str]) -> bool:
    return any(_to_int(row.get(k), 0) > 0 for k in ("changed_files", "added_loc", "deleted_loc", "modified_loc"))


def _path_depth(path: str) -> int:
    return len([p for p in path.strip("/").split("/") if p])


def _quantiles(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
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
        "mean": sum(s) / n,
    }


def _modified_loc_bin(v: int) -> str:
    if v <= 0:
        return "0"
    if v <= 10:
        return "1-10"
    if v <= 50:
        return "11-50"
    if v <= 100:
        return "51-100"
    if v <= 500:
        return "101-500"
    return ">500"


def _load_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_key_value_csv(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "value"])
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            writer.writerow([key, value])


def _find_parent_paths(paths: Iterable[str]) -> set[str]:
    sorted_paths = sorted(set(p.strip("/") for p in paths if p))
    parents: set[str] = set()
    for i, p in enumerate(sorted_paths):
        prefix = p + "/"
        # The dataset is small enough for this explicit scan, and it keeps the rule auditable.
        for q in sorted_paths[i + 1 :]:
            if q.startswith(prefix):
                parents.add(p)
                break
            if q > prefix and not q.startswith(prefix[: max(0, len(prefix) - 1)]):
                # Best-effort early break without relying on path trie complexity.
                pass
    return parents


def _build_overview(fields: list[str], rows: list[dict[str, str]], out_dir: Path) -> dict[str, Any]:
    paths = [(row.get("module_path") or "").strip() for row in rows]
    parent_paths = _find_parent_paths(paths)
    module_rows = []

    counts = Counter()
    bins_all = Counter()
    bins_changed_source = Counter()
    depths = Counter()
    code_locs_changed_source: list[int] = []
    src_counts_changed_source: list[int] = []
    modified_locs_changed_source: list[int] = []
    changed_files_changed_source: list[int] = []

    for idx, row in enumerate(rows, start=1):
        module_path = (row.get("module_path") or "").strip()
        source_file_count = _to_int(row.get("source_file_count"), 0)
        code_loc = _to_int(row.get("code_loc"), 0)
        changed_files = _to_int(row.get("changed_files"), 0)
        added_loc = _to_int(row.get("added_loc"), 0)
        deleted_loc = _to_int(row.get("deleted_loc"), 0)
        modified_loc = _to_int(row.get("modified_loc"), 0)
        changed = _is_changed_case(row)
        has_source = source_file_count > 0
        changed_source = changed and has_source
        depth = _path_depth(module_path)
        loc_bin = _modified_loc_bin(modified_loc)

        counts["total_rows"] += 1
        counts["source_modules"] += int(has_source)
        counts["changed_modules"] += int(changed)
        counts["changed_source_modules"] += int(changed_source)
        counts["modified_loc_positive_source_modules"] += int(has_source and modified_loc > 0)
        counts["modified_loc_le50_changed_source"] += int(changed_source and 0 < modified_loc <= 50)
        counts["modified_loc_le100_changed_source"] += int(changed_source and 0 < modified_loc <= 100)
        counts["parent_path_modules"] += int(module_path in parent_paths)
        counts["shallow_depth_le2_modules"] += int(depth <= 2)
        counts["shallow_depth_le3_modules"] += int(depth <= 3)

        bins_all[loc_bin] += 1
        depths[str(depth)] += 1
        if changed_source:
            bins_changed_source[loc_bin] += 1
            code_locs_changed_source.append(code_loc)
            src_counts_changed_source.append(source_file_count)
            modified_locs_changed_source.append(modified_loc)
            changed_files_changed_source.append(changed_files)

        module_rows.append(
            {
                "source_row_idx": idx,
                "module_path": module_path,
                "source_file_count": source_file_count,
                "code_loc": code_loc,
                "changed_files": changed_files,
                "added_loc": added_loc,
                "deleted_loc": deleted_loc,
                "modified_loc": modified_loc,
                "changed": str(changed).lower(),
                "changed_source": str(changed_source).lower(),
                "modified_loc_bin": loc_bin,
                "path_depth": depth,
                "is_parent_path": str(module_path in parent_paths).lower(),
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "module_overview.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(module_rows[0].keys()) if module_rows else [])
        if module_rows:
            writer.writeheader()
            writer.writerows(module_rows)

    with (out_dir / "modified_loc_distribution.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bin", "all_modules", "changed_source_modules"])
        for b in ["0", "1-10", "11-50", "51-100", "101-500", ">500"]:
            writer.writerow([b, bins_all.get(b, 0), bins_changed_source.get(b, 0)])

    summary = {
        **dict(counts),
        "modified_loc_bins_all_modules": dict(bins_all),
        "modified_loc_bins_changed_source_modules": dict(bins_changed_source),
        "path_depth_counts": dict(sorted(depths.items(), key=lambda kv: int(kv[0]))),
        "changed_source_code_loc_quantiles": _quantiles(code_locs_changed_source),
        "changed_source_source_file_count_quantiles": _quantiles(src_counts_changed_source),
        "changed_source_modified_loc_quantiles": _quantiles(modified_locs_changed_source),
        "changed_source_changed_files_quantiles": _quantiles(changed_files_changed_source),
        "parent_path_samples": sorted(parent_paths)[:100],
        "note": "modified_loc is a patch-size filter. code_loc/source_file_count describe module scope size.",
    }
    _write_json(out_dir / "dataset_overview.json", summary)
    _write_key_value_csv(out_dir / "dataset_overview.csv", summary)
    return summary


def _write_filtered_csv(fields: list[str], rows: list[dict[str, str]], out_path: Path, max_modified_loc: int) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            if _to_int(row.get("source_file_count"), 0) <= 0:
                continue
            if not _is_changed_case(row):
                continue
            modified_loc = _to_int(row.get("modified_loc"), 0)
            if not (0 < modified_loc <= max_modified_loc):
                continue
            writer.writerow(row)
            count += 1
    return count


def _run_logged(cmd: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        ret = proc.wait()
        log.write(f"[exit] {ret}\n")
        return ret


def _latest_child_with(path: Path, filename: str) -> Path | None:
    if not path.exists():
        return None
    candidates = [p for p in path.iterdir() if p.is_dir() and (p / filename).exists()]
    if not candidates:
        if (path / filename).exists():
            return path
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_json_or_none(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _calc_function_recall(summary: dict[str, Any] | None) -> float | None:
    if not summary:
        return None
    total = int(summary.get("total_modified_non_global_functions") or 0)
    missing = int(summary.get("total_missing_non_global_functions") or 0)
    if total <= 0:
        return None
    return (total - missing) / total


def _collect_stage_summaries(out_dir: Path) -> dict[str, Any]:
    candidate50_dir = _latest_child_with(out_dir / "candidate_small_le50", "summary.json")
    candidate100_dir = _latest_child_with(out_dir / "candidate_small_le100", "summary.json")
    affected50_dir = _latest_child_with(out_dir / "affected_kernel_only_le50", "summary.json")
    affected100_dir = _latest_child_with(out_dir / "affected_kernel_only_le100", "summary.json")
    qemu_dir = out_dir / "qemu_testability_le100"

    candidate50 = _read_json_or_none(candidate50_dir / "summary.json" if candidate50_dir else None)
    candidate100 = _read_json_or_none(candidate100_dir / "summary.json" if candidate100_dir else None)
    affected50 = _read_json_or_none(affected50_dir / "summary.json" if affected50_dir else None)
    affected100 = _read_json_or_none(affected100_dir / "summary.json" if affected100_dir else None)
    qemu = _read_json_or_none(qemu_dir / "qemu_testable_summary.json")

    return {
        "candidate_small_le50": {"run_dir": str(candidate50_dir) if candidate50_dir else None, "summary": candidate50},
        "candidate_small_le100": {"run_dir": str(candidate100_dir) if candidate100_dir else None, "summary": candidate100},
        "affected_kernel_only_le50": {
            "run_dir": str(affected50_dir) if affected50_dir else None,
            "summary": affected50,
            "function_recall": _calc_function_recall(affected50),
        },
        "affected_kernel_only_le100": {
            "run_dir": str(affected100_dir) if affected100_dir else None,
            "summary": affected100,
            "function_recall": _calc_function_recall(affected100),
        },
        "qemu_testability_le100": {"run_dir": str(qemu_dir) if qemu else None, "summary": qemu},
    }


def _write_report(out_dir: Path, overview: dict[str, Any], stages: dict[str, Any], manifest: dict[str, Any]) -> None:
    failed_commands = [
        c for c in manifest.get("commands", [])
        if c.get("exit_code") not in (None, 0)
    ]
    lines = [
        "# v6.19 -> v7.0-rc1 Driver Statistics",
        "",
        "This run is designed to support workflow decisions, not to inflate ablation tables.",
        "",
        "## Run Status",
        "",
        "- status: " + ("INCOMPLETE" if failed_commands else "COMPLETE"),
        f"- failed_stages: {len(failed_commands)}",
        "",
    ]
    if failed_commands:
        lines.extend([
            "| stage | exit_code | log |",
            "|---|---:|---|",
            *[
                f"| `{c.get('stage')}` | {c.get('exit_code')} | `logs/{c.get('stage')}.log` |"
                for c in failed_commands
            ],
            "",
        ])
    lines.extend([
        "## Resource Cap",
        "",
        f"- jobs: {manifest['jobs']}",
        f"- max_subprocs: {manifest['max_subprocs']}",
        f"- git_grep_threads: {manifest['git_grep_threads']}",
        f"- cpu_count: {manifest['cpu_count']}",
        "",
        "## Dataset Overview",
        "",
        f"- total_rows: {overview.get('total_rows')}",
        f"- changed_source_modules: {overview.get('changed_source_modules')}",
        f"- modified_loc <= 50 changed source modules: {overview.get('modified_loc_le50_changed_source')}",
        f"- modified_loc <= 100 changed source modules: {overview.get('modified_loc_le100_changed_source')}",
        f"- parent_path_modules: {overview.get('parent_path_modules')}",
        "",
        "## Stage Summaries",
        "",
    ])

    c50 = stages.get("candidate_small_le50", {})
    if c50.get("summary"):
        s = c50["summary"]
        lines.extend([
            "### Candidate Small <=50",
            "",
            f"- run_dir: `{c50.get('run_dir')}`",
            f"- selected_candidates: {s.get('selected_candidates')}",
            f"- ok: {s.get('ok')}, failed: {s.get('failed')}, skipped: {s.get('skipped')}",
            f"- modules_with_removed_funcs: {s.get('modules_with_removed_funcs')}",
            f"- modules_with_added_funcs: {s.get('modules_with_added_funcs')}",
            f"- modules_with_removed_and_added: {s.get('modules_with_removed_and_added')}",
            "",
        ])

    a50 = stages.get("affected_kernel_only_le50", {})
    if a50.get("summary"):
        s = a50["summary"]
        lines.extend([
            "### Affected Coverage <=50 (Kernel-Only)",
            "",
            f"- run_dir: `{a50.get('run_dir')}`",
            f"- selected_cases: {s.get('selected_cases')}",
            f"- ok_cases: {s.get('ok_cases')}, failed_cases: {s.get('failed_cases')}, skipped_cases: {s.get('skipped_cases')}",
            f"- cases_with_modified_non_global: {s.get('cases_with_modified_non_global')}",
            f"- cases_fully_covered_non_global: {s.get('cases_fully_covered_non_global')}",
            f"- case coverage ratio: {s.get('coverage_ratio_non_global')}",
            f"- function recall: {a50.get('function_recall')}",
            f"- total_missing_non_global_functions: {s.get('total_missing_non_global_functions')}",
            f"- total_extra_affected_functions: {s.get('total_extra_affected_functions')}",
            "",
        ])

    c100 = stages.get("candidate_small_le100", {})
    if c100.get("summary"):
        s = c100["summary"]
        lines.extend([
            "### Candidate Small <=100",
            "",
            f"- run_dir: `{c100.get('run_dir')}`",
            f"- selected_candidates: {s.get('selected_candidates')}",
            f"- ok: {s.get('ok')}, failed: {s.get('failed')}, skipped: {s.get('skipped')}",
            "",
        ])

    qemu = stages.get("qemu_testability_le100", {})
    if qemu.get("summary"):
        s = qemu["summary"]
        lines.extend([
            "### QEMU Testability Heuristic <=100",
            "",
            f"- run_dir: `{qemu.get('run_dir')}`",
            f"- total_cases: {s.get('total_cases')}",
            f"- scope_counts: `{json.dumps(s.get('scope_counts', {}), ensure_ascii=False)}`",
            "",
        ])

    if stages.get("affected_kernel_only_le100", {}).get("summary"):
        a100 = stages["affected_kernel_only_le100"]
        s = a100["summary"]
        lines.extend([
            "### Affected Coverage <=100 (Kernel-Only)",
            "",
            f"- run_dir: `{a100.get('run_dir')}`",
            f"- selected_cases: {s.get('selected_cases')}",
            f"- cases_fully_covered_non_global: {s.get('cases_fully_covered_non_global')}",
            f"- function recall: {a100.get('function_recall')}",
            "",
        ])

    lines.extend([
        "## Reproducibility",
        "",
        "Exact commands and environment are saved in `run_manifest.json`.",
        "Raw command logs are under `logs/`.",
        "",
    ])
    (out_dir / "stats_report.md").write_text("\n".join(lines), encoding="utf-8")


def _auto_jobs(cpu_count: int) -> tuple[int, int, int]:
    half = max(1, cpu_count // 2)
    jobs = max(4, min(48, half // 2))
    max_subprocs = max(8, min(96, half))
    git_grep_threads = 1
    return jobs, max_subprocs, git_grep_threads


def main() -> int:
    ap = argparse.ArgumentParser(description="Run v6.19->v7.0-rc1 driver stats with resource caps")
    ap.add_argument("--cases-csv", default=DEFAULT_CASES_CSV)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--old-ref", default=DEFAULT_OLD_REF)
    ap.add_argument("--new-ref", default=DEFAULT_NEW_REF)
    ap.add_argument("--out-dir", default="/mnt/disk7/jw_ai4os/driver-agent/workflow/runs")
    ap.add_argument("--jobs", type=int, default=0, help="0 means auto, capped below half CPU")
    ap.add_argument("--max-subprocs", type=int, default=0, help="0 means auto")
    ap.add_argument("--git-grep-threads", type=int, default=0, help="0 means auto")
    ap.add_argument("--run-affected-loc100", action="store_true", help="also run heavy affected coverage for modified_loc <= 100")
    ap.add_argument("--skip-affected", action="store_true")
    ap.add_argument("--skip-qemu-testability", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    workflow_root = Path(__file__).resolve().parents[1]
    repo_root = workflow_root.parent
    cases_csv = Path(args.cases_csv).resolve()
    linux_repo = Path(args.repo).resolve()
    out_root = Path(args.out_dir).resolve()
    run_dir = out_root / f"stats_v619_to_v70rc1_{_now()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    cpu_count = os.cpu_count() or 8
    auto_jobs, auto_max_subprocs, auto_git_grep_threads = _auto_jobs(cpu_count)
    jobs = args.jobs if args.jobs > 0 else auto_jobs
    max_subprocs = args.max_subprocs if args.max_subprocs > 0 else auto_max_subprocs
    git_grep_threads = args.git_grep_threads if args.git_grep_threads > 0 else auto_git_grep_threads

    # Hard guardrail: do not exceed half of observed CPUs through this wrapper.
    half_cpu = max(1, cpu_count // 2)
    jobs = min(jobs, max(1, half_cpu))
    max_subprocs = min(max_subprocs, max(1, half_cpu))

    env = os.environ.copy()
    env["DRIVER_AGENT_MAX_SUBPROCS"] = str(max_subprocs)
    env["DRIVER_AGENT_GIT_GREP_THREADS"] = str(git_grep_threads)
    env.pop("DRIVER_AGENT_ALLOW_DRIVER_EVIDENCE", None)

    fields, rows = _load_rows(cases_csv)
    overview_dir = run_dir / "overview"
    overview = _build_overview(fields, rows, overview_dir)
    le50_count = _write_filtered_csv(fields, rows, run_dir / "filtered_cases_le50.csv", 50)
    le100_count = _write_filtered_csv(fields, rows, run_dir / "filtered_cases_le100.csv", 100)

    manifest: dict[str, Any] = {
        "run_dir": str(run_dir),
        "cases_csv": str(cases_csv),
        "repo": str(linux_repo),
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "cpu_count": cpu_count,
        "jobs": jobs,
        "max_subprocs": max_subprocs,
        "git_grep_threads": git_grep_threads,
        "filtered_le50_cases": le50_count,
        "filtered_le100_cases": le100_count,
        "commands": [],
        "policy": {
            "agent_input_boundary": "old target module source + old/new kernel definitions/diffs only",
            "affected_signal_mode": "kernel_only",
            "commit_evidence": "disabled for this statistics run",
            "target_driver_new_diff": "evaluation only in coverage scripts, never agent input",
        },
    }
    _write_json(run_dir / "run_manifest.json", manifest)

    py = sys.executable
    stages: list[tuple[str, list[str]]] = []
    stages.append(
        (
            "candidate_small_le50",
            [
                py,
                str(workflow_root / "tools" / "build_candidate_small_dataset.py"),
                "--csv",
                str(cases_csv),
                "--repo",
                str(linux_repo),
                "--old-ref",
                args.old_ref,
                "--new-ref",
                args.new_ref,
                "--out-dir",
                str(run_dir / "candidate_small_le50"),
                "--jobs",
                str(jobs),
                "--min-modified-loc",
                "1",
                "--max-modified-loc",
                "50",
            ],
        )
    )
    stages.append(
        (
            "candidate_small_le100",
            [
                py,
                str(workflow_root / "tools" / "build_candidate_small_dataset.py"),
                "--csv",
                str(cases_csv),
                "--repo",
                str(linux_repo),
                "--old-ref",
                args.old_ref,
                "--new-ref",
                args.new_ref,
                "--out-dir",
                str(run_dir / "candidate_small_le100"),
                "--jobs",
                str(jobs),
                "--min-modified-loc",
                "1",
                "--max-modified-loc",
                "100",
            ],
        )
    )

    if not args.skip_qemu_testability:
        stages.append(
            (
                "qemu_testability_le100",
                [
                    py,
                    str(workflow_root / "tools" / "list_qemu_testable_intree_cases.py"),
                    "--cases-csv",
                    str(cases_csv),
                    "--linux-repo",
                    str(linux_repo),
                    "--old-ref",
                    args.old_ref,
                    "--new-ref",
                    args.new_ref,
                    "--max-modified-loc",
                    "100",
                    "--out-dir",
                    str(run_dir / "qemu_testability_le100"),
                ],
            )
        )

    if not args.skip_affected:
        stages.append(
            (
                "affected_kernel_only_le50",
                [
                    py,
                    str(workflow_root / "tools" / "check_affected_vs_modified.py"),
                    "--csv",
                    str(run_dir / "filtered_cases_le50.csv"),
                    "--repo",
                    str(linux_repo),
                    "--old-ref",
                    args.old_ref,
                    "--new-ref",
                    args.new_ref,
                    "--out-dir",
                    str(run_dir / "affected_kernel_only_le50"),
                    "--jobs",
                    str(jobs),
                    "--max-subprocs",
                    str(max_subprocs),
                    "--git-grep-threads",
                    str(git_grep_threads),
                    "--no-collect-commit-evidence",
                    "--affected-func-signal-mode",
                    "kernel_only",
                    "--ignore-comment-only-function-changes",
                ],
            )
        )
        if args.run_affected_loc100:
            loc100_jobs = max(1, min(jobs // 2 if jobs > 1 else 1, half_cpu))
            loc100_subprocs = max(1, min(max_subprocs // 2 if max_subprocs > 1 else 1, half_cpu))
            stages.append(
                (
                    "affected_kernel_only_le100",
                    [
                        py,
                        str(workflow_root / "tools" / "check_affected_vs_modified.py"),
                        "--csv",
                        str(run_dir / "filtered_cases_le100.csv"),
                        "--repo",
                        str(linux_repo),
                        "--old-ref",
                        args.old_ref,
                        "--new-ref",
                        args.new_ref,
                        "--out-dir",
                        str(run_dir / "affected_kernel_only_le100"),
                        "--jobs",
                        str(loc100_jobs),
                        "--max-subprocs",
                        str(loc100_subprocs),
                        "--git-grep-threads",
                        str(git_grep_threads),
                        "--no-collect-commit-evidence",
                        "--affected-func-signal-mode",
                        "kernel_only",
                        "--ignore-comment-only-function-changes",
                    ],
                )
            )

    _write_json(run_dir / "run_manifest.json", manifest)

    failures: list[dict[str, Any]] = []
    started = time.time()
    for name, cmd in stages:
        t0 = time.time()
        manifest["commands"].append({"stage": name, "cmd": cmd, "start_time": datetime.now().isoformat()})
        _write_json(run_dir / "run_manifest.json", manifest)
        print(f"[stage-start] {name}", flush=True)
        ret = _run_logged(cmd, repo_root, run_dir / "logs" / f"{name}.log", env)
        elapsed = time.time() - t0
        manifest["commands"][-1].update({"exit_code": ret, "elapsed_sec": elapsed, "end_time": datetime.now().isoformat()})
        _write_json(run_dir / "run_manifest.json", manifest)
        print(f"[stage-done] {name} exit={ret} elapsed_sec={elapsed:.1f}", flush=True)
        if ret != 0:
            failures.append({"stage": name, "exit_code": ret, "log": str(run_dir / "logs" / f"{name}.log")})
            # Continue so lightweight reports still get produced from completed stages.

    stage_summaries = _collect_stage_summaries(run_dir)
    final_summary = {
        "run_dir": str(run_dir),
        "elapsed_sec": time.time() - started,
        "failures": failures,
        "overview": overview,
        "stages": stage_summaries,
    }
    _write_json(run_dir / "stats_summary.json", final_summary)
    _write_report(run_dir, overview, stage_summaries, manifest)
    print("[stats-suite-done] " + json.dumps({"run_dir": str(run_dir), "failures": failures}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
