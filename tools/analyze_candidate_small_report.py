#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt


def _run_git(repo: Path, args: list[str]) -> str:
    p = subprocess.run(["git", "-C", str(repo)] + args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"git failed: {' '.join(args)}\n{p.stderr}")
    return p.stdout


def _to_int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


def _bucket_modified_loc(v: int) -> str:
    if v <= 5:
        return "01-05"
    if v <= 10:
        return "06-10"
    if v <= 20:
        return "11-20"
    if v <= 30:
        return "21-30"
    if v <= 40:
        return "31-40"
    return "41-50"


def _risk_tier(mod_func_cnt: int, removed_cnt: int, added_cnt: int, has_global: bool) -> str:
    # Heuristic tiering for downstream agent strategy design.
    if removed_cnt + added_cnt >= 20:
        return "T3_api_reshape"
    if mod_func_cnt >= 50:
        return "T3_heavy_surface"
    if removed_cnt + added_cnt > 0:
        return "T2_structure_shift"
    if has_global:
        return "T2_global_scope_change"
    if mod_func_cnt >= 10:
        return "T1_medium_edit"
    return "T0_light_edit"


def _save_bar_chart(labels: list[str], values: list[int], title: str, xlabel: str, ylabel: str, out_path: Path) -> None:
    plt.figure(figsize=(12, 5))
    plt.bar(labels, values)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _save_hist(values: list[int], bins: int, title: str, xlabel: str, ylabel: str, out_path: Path) -> None:
    plt.figure(figsize=(10, 5))
    plt.hist(values, bins=bins)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _save_scatter(xs: list[int], ys: list[int], title: str, xlabel: str, ylabel: str, out_path: Path) -> None:
    plt.figure(figsize=(8, 6))
    plt.scatter(xs, ys, alpha=0.7)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def _changed_non_source_files(repo: Path, old_ref: str, new_ref: str, module_path: str) -> list[str]:
    out = _run_git(repo, ["diff", "--name-only", old_ref, new_ref, "--", module_path])
    files = [line.strip() for line in out.splitlines() if line.strip()]
    non_src = [p for p in files if Path(p).suffix.lower() not in {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}]
    return non_src


def main() -> int:
    p = argparse.ArgumentParser(description="Create enriched analysis report and charts for candidate_small run")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    args = p.parse_args()

    run_dir = Path(args.run_dir).resolve()
    repo = Path(args.repo).resolve()
    analysis_csv = run_dir / "candidate_small_analysis.csv"
    summary_json = run_dir / "summary.json"
    if not analysis_csv.exists():
        raise FileNotFoundError(analysis_csv)
    if not summary_json.exists():
        raise FileNotFoundError(summary_json)

    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(analysis_csv.open("r", encoding="utf-8")))
    ok_rows = [r for r in rows if r["status"] == "ok"]
    skipped_rows = [r for r in rows if r["status"].startswith("skipped")]

    enriched_rows = []
    for r in rows:
        mod_loc = _to_int(r["modified_loc"])
        mod_funcs = _to_int(r["modified_non_global_func_count"])
        removed_cnt = _to_int(r["removed_func_count"])
        added_cnt = _to_int(r["added_func_count"])
        changed_src = _to_int(r["changed_source_files"])
        global_cnt = _to_int(r["modified_global_file_count"])
        density = _safe_div(mod_funcs, max(1, changed_src))
        api_shift = removed_cnt + added_cnt
        has_global = global_cnt > 0
        tier = _risk_tier(mod_funcs, removed_cnt, added_cnt, has_global)

        out = dict(r)
        out["modified_loc_bucket"] = _bucket_modified_loc(mod_loc)
        out["mod_func_per_changed_source_file"] = f"{density:.3f}"
        out["api_shift_count"] = str(api_shift)
        out["risk_tier"] = tier
        enriched_rows.append(out)

    enriched_csv = run_dir / "candidate_small_analysis_enriched.csv"
    with enriched_csv.open("w", encoding="utf-8", newline="") as f:
        fields = list(enriched_rows[0].keys()) if enriched_rows else []
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(enriched_rows)

    # Skipped detail with concrete non-source files.
    skipped_detail = []
    for r in skipped_rows:
        module_path = r["module_path"]
        non_src = _changed_non_source_files(repo, args.old_ref, args.new_ref, module_path)
        skipped_detail.append(
            {
                "module_path": module_path,
                "status": r["status"],
                "modified_loc": _to_int(r["modified_loc"]),
                "changed_files": _to_int(r["changed_files"]),
                "changed_source_files": _to_int(r["changed_source_files"]),
                "non_source_changed_files": non_src,
            }
        )
    (run_dir / "skipped_reason_detail.json").write_text(json.dumps(skipped_detail, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Focus tables.
    both_rows = [r for r in ok_rows if r["has_removed_and_added"] == "true"]
    both_rows = sorted(both_rows, key=lambda r: _to_int(r["removed_func_count"]) + _to_int(r["added_func_count"]), reverse=True)
    both_csv = run_dir / "modules_removed_and_added.csv"
    with both_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(both_rows[0].keys()) if both_rows else list(rows[0].keys()))
        w.writeheader()
        w.writerows(both_rows)

    top_mod_rows = sorted(ok_rows, key=lambda r: _to_int(r["modified_non_global_func_count"]), reverse=True)[:100]
    top_csv = run_dir / "top_modules_by_modified_functions.csv"
    with top_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(top_mod_rows[0].keys()) if top_mod_rows else list(rows[0].keys()))
        w.writeheader()
        w.writerows(top_mod_rows)

    # Charts.
    chart_dir = run_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    # 1) modified_loc bucket bars (OK only)
    buckets = ["01-05", "06-10", "11-20", "21-30", "31-40", "41-50"]
    bucket_counts = {k: 0 for k in buckets}
    for r in ok_rows:
        bucket_counts[_bucket_modified_loc(_to_int(r["modified_loc"]))] += 1
    _save_bar_chart(
        buckets,
        [bucket_counts[k] for k in buckets],
        "Candidate Small: Modified LOC Bucket Distribution",
        "modified_loc bucket",
        "module count",
        chart_dir / "modified_loc_bucket_bar.png",
    )

    # 2) modified function count histogram
    mod_func_vals = [_to_int(r["modified_non_global_func_count"]) for r in ok_rows]
    _save_hist(
        mod_func_vals,
        bins=40,
        title="Candidate Small: Modified Non-Global Function Count Histogram",
        xlabel="modified_non_global_func_count",
        ylabel="module count",
        out_path=chart_dir / "modified_function_count_hist.png",
    )

    # 3) removed vs added scatter
    removed_vals = [_to_int(r["removed_func_count"]) for r in ok_rows]
    added_vals = [_to_int(r["added_func_count"]) for r in ok_rows]
    _save_scatter(
        removed_vals,
        added_vals,
        "Candidate Small: Removed vs Added Function Count",
        "removed_func_count",
        "added_func_count",
        chart_dir / "removed_vs_added_scatter.png",
    )

    # 4) top modules by modified funcs
    top20 = sorted(ok_rows, key=lambda r: _to_int(r["modified_non_global_func_count"]), reverse=True)[:20]
    _save_bar_chart(
        [r["module_path"] for r in top20],
        [_to_int(r["modified_non_global_func_count"]) for r in top20],
        "Top 20 Modules by Modified Non-Global Function Count",
        "module_path",
        "modified_non_global_func_count",
        chart_dir / "top20_modules_modified_functions.png",
    )

    # 5) risk tier distribution
    tier_order = ["T0_light_edit", "T1_medium_edit", "T2_global_scope_change", "T2_structure_shift", "T3_heavy_surface", "T3_api_reshape"]
    tier_count = {k: 0 for k in tier_order}
    for r in enriched_rows:
        if r["status"] != "ok":
            continue
        tier_count[r["risk_tier"]] = tier_count.get(r["risk_tier"], 0) + 1
    _save_bar_chart(
        tier_order,
        [tier_count.get(k, 0) for k in tier_order],
        "Risk Tier Distribution (Heuristic)",
        "risk_tier",
        "module count",
        chart_dir / "risk_tier_distribution.png",
    )

    # Markdown report.
    ok = summary["ok"]
    skipped = summary["skipped"]
    failed = summary["failed"]
    removed_and_added = summary["modules_with_removed_and_added"]
    removed_only = summary["modules_with_removed_funcs"] - removed_and_added
    added_only = summary["modules_with_added_funcs"] - removed_and_added
    neither = ok - removed_and_added - removed_only - added_only

    top5_mod = sorted(ok_rows, key=lambda r: _to_int(r["modified_non_global_func_count"]), reverse=True)[:5]
    top5_api_shift = sorted(ok_rows, key=lambda r: _to_int(r["removed_func_count"]) + _to_int(r["added_func_count"]), reverse=True)[:5]

    report_lines = []
    report_lines.append("# Candidate Small Analysis Report")
    report_lines.append("")
    report_lines.append(f"- run_dir: `{run_dir}`")
    report_lines.append(f"- selected_candidates: **{summary['selected_candidates']}**")
    report_lines.append(f"- ok/skipped/failed: **{ok}/{skipped}/{failed}**")
    report_lines.append(f"- modified_loc range: **{summary['min_modified_loc']}..{summary['max_modified_loc']}**")
    report_lines.append("")
    report_lines.append("## Why 2 Skipped")
    report_lines.append("- `drivers/ntb/hw/epf`: only `Kconfig` changed (non-source), so `changed_source_files=0`.")
    report_lines.append("- `drivers/iio/test`: only `Kconfig` changed (non-source), so `changed_source_files=0`.")
    report_lines.append("")
    report_lines.append("## Core Numbers")
    report_lines.append(f"- total_modified_non_global_funcs: **{summary['total_modified_non_global_funcs']}**")
    report_lines.append(f"- total_removed_funcs: **{summary['total_removed_funcs']}**")
    report_lines.append(f"- total_added_funcs: **{summary['total_added_funcs']}**")
    report_lines.append(f"- modules_with_removed_and_added: **{removed_and_added}** ({summary['ratio_modules_with_removed_and_added_over_ok']*100:.2f}% of ok)")
    report_lines.append(f"- removed_only / added_only / both / neither: **{removed_only} / {added_only} / {removed_and_added} / {neither}**")
    report_lines.append("")
    report_lines.append("## Quantiles")
    report_lines.append(f"- modified_loc p50/p90/p95/max: **{summary['modified_loc_quantiles']['p50']} / {summary['modified_loc_quantiles']['p90']} / {summary['modified_loc_quantiles']['p95']} / {summary['modified_loc_quantiles']['max']}**")
    report_lines.append(f"- modified_non_global_func_count p50/p90/p95/max: **{summary['modified_non_global_func_count_quantiles']['p50']} / {summary['modified_non_global_func_count_quantiles']['p90']} / {summary['modified_non_global_func_count_quantiles']['p95']} / {summary['modified_non_global_func_count_quantiles']['max']}**")
    report_lines.append(f"- removed_func_count p95/max: **{summary['removed_func_count_quantiles']['p95']} / {summary['removed_func_count_quantiles']['max']}**")
    report_lines.append(f"- added_func_count p95/max: **{summary['added_func_count_quantiles']['p95']} / {summary['added_func_count_quantiles']['max']}**")
    report_lines.append("")
    report_lines.append("## Top Modules by Modified Function Count")
    for r in top5_mod:
        report_lines.append(
            f"- `{r['module_path']}`: mod_funcs={r['modified_non_global_func_count']}, removed={r['removed_func_count']}, added={r['added_func_count']}"
        )
    report_lines.append("")
    report_lines.append("## Top Modules by API Shift (removed+added)")
    for r in top5_api_shift:
        api_shift = _to_int(r["removed_func_count"]) + _to_int(r["added_func_count"])
        report_lines.append(
            f"- `{r['module_path']}`: api_shift={api_shift} (removed={r['removed_func_count']}, added={r['added_func_count']}), mod_funcs={r['modified_non_global_func_count']}"
        )
    report_lines.append("")
    report_lines.append("## Chart Files")
    report_lines.append(f"- `charts/modified_loc_bucket_bar.png`")
    report_lines.append(f"- `charts/modified_function_count_hist.png`")
    report_lines.append(f"- `charts/removed_vs_added_scatter.png`")
    report_lines.append(f"- `charts/top20_modules_modified_functions.png`")
    report_lines.append(f"- `charts/risk_tier_distribution.png`")
    report_lines.append("")
    report_lines.append("## Extra Data Files")
    report_lines.append("- `candidate_small_analysis_enriched.csv`")
    report_lines.append("- `modules_removed_and_added.csv`")
    report_lines.append("- `top_modules_by_modified_functions.csv`")
    report_lines.append("- `skipped_reason_detail.json`")

    (run_dir / "analysis_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

