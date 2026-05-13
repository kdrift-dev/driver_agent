#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.symbol_scanner import _extract_c_like_functions, _extract_rust_functions
from driver_agent_pregen.utils import now_timestamp, slugify, write_json, write_text


LABELS = [
    "SYMBOL_SCAN_MISS",
    "DEFINITION_LOCATION_MISS",
    "KERNEL_DIFF_EVIDENCE_MISS",
    "MIGRATION_SIGNAL_MISS",
    "IMPACT_GRAPH_PROPAGATION_MISS",
    "FILTER_OR_TIER_MISS",
    "CALLER_CALLBACK_EXPANSION_NEEDED",
    "REFERENCE_DIFF_ONLY_CHANGE",
    "UNKNOWN",
]

SEMANTIC_CHANGE_TYPES = {
    "api_deprecation",
    "rename",
    "signature_change",
    "struct_field_change",
    "return_value_change",
    "ownership_change",
    "locking_change",
    "api_migration_signal",
}

ALLOC_FAMILY = {
    "kmalloc",
    "kzalloc",
    "kvmalloc",
    "vzalloc",
    "kmalloc_array",
    "kvmalloc_array",
    "kcalloc",
    "kvcalloc",
}


@dataclass
class MissRecord:
    case_id: str
    module_path: str
    run_dir: str
    missing_function: str
    missing_file: str
    present_in_old_source: bool
    detected_in_driver_functions: bool
    kernel_symbols_detected_count: int
    kernel_symbols_detected_sample: str
    definition_locations_count: int
    symbols_with_locations_count: int
    relevant_kernel_diff_evidence_count: int
    migration_signal_count: int
    replacement_candidates: str
    impact_contains_function: bool
    impact_exclusion_reason: str
    excluded_by_filter_or_tier: bool
    would_expansion_include: bool
    direct_callers_count: int
    direct_callees_count: int
    callback_root_count: int
    root_cause_label: str
    supporting_labels: str
    notes: str


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _parse_json_list(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(x) for x in v]
    except Exception:
        return []
    return []


def _extract_functions_for_file(text: str, rel_file: str) -> set[str]:
    suffix = Path(rel_file).suffix.lower()
    if suffix == ".rs":
        funcs = _extract_rust_functions(text, rel_file)
    else:
        funcs = _extract_c_like_functions(text, rel_file)
    return {f.name for f in funcs}


def _symbol_tier(item: dict[str, Any]) -> str | None:
    if item.get("migration_signal"):
        return "semantic"
    ct = str(item.get("change_type_guess", "unknown"))
    if item.get("definition_changed"):
        if ct == "include_dependency":
            return "context"
        if ct in {"return_value_change", "ownership_change", "locking_change", "api_migration_signal"}:
            return "semantic"
        return "hard"
    if ct in {"return_value_change", "ownership_change", "locking_change", "api_migration_signal"}:
        return "semantic"
    if ct == "include_dependency":
        return "context"
    return None


def _function_symbol_usage(used_symbols: list[dict[str, Any]], fn: str) -> list[dict[str, Any]]:
    out = []
    for s in used_symbols:
        if s.get("enclosing_function") != fn:
            continue
        if s.get("is_driver_local"):
            continue
        out.append(s)
    return out


def _infer_impact_exclusion_reason(
    *,
    impact_graph: dict[str, Any],
    fn: str,
    has_relevant_evidence: bool,
    has_migration_signal: bool,
    has_locations: bool,
) -> tuple[str, bool]:
    # Newer impact graph may have explicit diagnostics.
    for item in impact_graph.get("excluded_seed_functions", []):
        if item.get("function") == fn:
            reason = str(item.get("reason", ""))
            tier = str(item.get("tier", ""))
            if tier or reason:
                return f"excluded_seed:tier={tier};reason={reason}", True

    for item in impact_graph.get("context_functions", []):
        if item.get("function") == fn:
            rs = item.get("context_reasons", [])
            return f"context_only:{','.join(str(x) for x in rs)}", True

    if has_relevant_evidence:
        return "relevant_evidence_present_but_not_propagated", False
    if has_migration_signal:
        return "migration_signal_present_but_filtered", True
    if has_locations:
        return "symbols_located_but_no_diff_trigger", False
    return "no_seed_evidence", False


def _would_expansion_include(
    *,
    fn: str,
    affected_functions: set[str],
    callers: set[str],
    callees: set[str],
    callback_roots: set[str],
    callback_reach: dict[str, set[str]],
) -> bool:
    if not affected_functions:
        return False
    # direct caller of affected seed
    if callers & affected_functions:
        return True
    # direct callee of affected seed
    if callees & affected_functions:
        return True
    # same callback chain
    for root in callback_roots:
        reach = callback_reach.get(root, set())
        if reach & affected_functions:
            return True
    return False


def _label_record(
    *,
    present_in_old_source: bool,
    detected_in_driver_functions: bool,
    kernel_symbols_detected_count: int,
    symbols_with_locations_count: int,
    relevant_kernel_diff_evidence_count: int,
    migration_signal_count: int,
    alloc_like_pattern: bool,
    impact_contains_function: bool,
    excluded_by_filter_or_tier: bool,
    would_expansion_include: bool,
) -> tuple[str, list[str]]:
    supports: list[str] = []
    if not present_in_old_source or not detected_in_driver_functions:
        return "SYMBOL_SCAN_MISS", supports
    if kernel_symbols_detected_count == 0:
        return "SYMBOL_SCAN_MISS", supports
    if symbols_with_locations_count == 0:
        return "DEFINITION_LOCATION_MISS", supports
    if relevant_kernel_diff_evidence_count == 0:
        if alloc_like_pattern and migration_signal_count == 0:
            return "MIGRATION_SIGNAL_MISS", supports
        if would_expansion_include:
            return "CALLER_CALLBACK_EXPANSION_NEEDED", supports
        return "KERNEL_DIFF_EVIDENCE_MISS", supports
    if impact_contains_function:
        return "UNKNOWN", supports
    if excluded_by_filter_or_tier:
        return "FILTER_OR_TIER_MISS", supports
    if would_expansion_include:
        return "CALLER_CALLBACK_EXPANSION_NEEDED", supports
    return "IMPACT_GRAPH_PROPAGATION_MISS", supports


def _case_report(case_id: str, module_path: str, records: list[MissRecord]) -> str:
    lines: list[str] = []
    lines.append(f"# {case_id} affected-function miss analysis")
    lines.append("")
    lines.append(f"- module_path: `{module_path}`")
    lines.append(f"- missing_functions: {len(records)}")
    lines.append("")
    for i, r in enumerate(records, start=1):
        lines.append(f"## Missing Function {i}: `{r.missing_file}:{r.missing_function}`")
        lines.append("")
        lines.append("1. Was the missing function present in old driver source?")
        lines.append(f"- {r.present_in_old_source}")
        lines.append("2. Was it detected in driver_functions.json?")
        lines.append(f"- {r.detected_in_driver_functions}")
        lines.append("3. Were kernel symbols detected in driver_used_symbols.json?")
        lines.append(f"- count={r.kernel_symbols_detected_count}; sample={r.kernel_symbols_detected_sample}")
        lines.append("4. Did those symbols appear in kernel_definition_locations.json?")
        lines.append(f"- symbols_with_locations={r.symbols_with_locations_count} / located_items={r.definition_locations_count}")
        lines.append("5. Did kernel_definition_diffs.json contain relevant evidence?")
        lines.append(f"- relevant_evidence_count={r.relevant_kernel_diff_evidence_count}")
        lines.append("6. Was there migration/replacement signal?")
        lines.append(f"- migration_signal_count={r.migration_signal_count}; replacements={r.replacement_candidates}")
        lines.append("7. Did impact_graph_builder exclude it? If yes, why?")
        lines.append(f"- impact_contains_function={r.impact_contains_function}; reason={r.impact_exclusion_reason}")
        lines.append("8. Was it excluded by confidence/tier/filter logic?")
        lines.append(f"- {r.excluded_by_filter_or_tier}")
        lines.append("9. Would caller/callee/callback expansion include it?")
        lines.append(f"- {r.would_expansion_include} (callers={r.direct_callers_count}, callees={r.direct_callees_count}, callback_roots={r.callback_root_count})")
        lines.append("")
        lines.append(f"- root_cause_label: `{r.root_cause_label}`")
        if r.notes:
            lines.append(f"- notes: {r.notes}")
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Trace AFFECTED_FUNCTION_MISS root causes from baseline spec-gap outputs")
    p.add_argument("--baseline-dir", required=True, help="spec-gap baseline directory containing spec_gap_cases.csv")
    p.add_argument("--out-dir", default="", help="output directory (default: workflow/runs/affected_miss_analysis_<ts>)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline_dir = Path(args.baseline_dir).resolve()
    if not baseline_dir.exists():
        raise FileNotFoundError(f"baseline dir not found: {baseline_dir}")
    cases_csv = baseline_dir / "spec_gap_cases.csv"
    if not cases_csv.exists():
        raise FileNotFoundError(f"spec_gap_cases.csv not found under: {baseline_dir}")

    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
    else:
        out_dir = (WORKFLOW_ROOT / "runs" / f"affected_miss_analysis_{now_timestamp()}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    case_reports_dir = out_dir / "case_reports"
    case_reports_dir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(cases_csv.open("r", encoding="utf-8")))
    miss_rows = [r for r in rows if r.get("spec_gap_label") == "AFFECTED_FUNCTION_MISS"]

    all_records: list[MissRecord] = []
    by_case: dict[str, list[MissRecord]] = defaultdict(list)

    for row in miss_rows:
        case_id = row["case_id"]
        module_path = row["module_path"]
        run_dir = Path(row["run_dir"])
        extracted = run_dir / "extracted"

        driver_functions = _load_json(extracted / "driver_functions.json", {}).get("functions", [])
        used_symbols = _load_json(extracted / "driver_used_symbols.json", {}).get("symbols", [])
        locations_items = _load_json(extracted / "kernel_definition_locations.json", {}).get("items", [])
        diffs_items = _load_json(extracted / "kernel_definition_diffs.json", {}).get("items", [])
        impact_graph = _load_json(extracted / "impact_graph.json", {})
        call_graph = _load_json(extracted / "call_graph.json", {})
        callbacks = _load_json(extracted / "driver_callbacks.json", {}).get("callbacks", [])
        callback_graph = _load_json(extracted / "callback_graph.json", {})

        fn_index = {(f.get("file"), f.get("name")): f for f in driver_functions if isinstance(f, dict)}
        loc_by_sym = {(it.get("symbol"), it.get("kind")): it for it in locations_items if isinstance(it, dict)}
        diff_by_sym = {(it.get("symbol"), it.get("kind")): it for it in diffs_items if isinstance(it, dict)}

        affected_fn_names = {x.get("function") for x in impact_graph.get("affected_functions", []) if isinstance(x, dict)}

        callers_map: dict[str, set[str]] = defaultdict(set)
        callees_map: dict[str, set[str]] = defaultdict(set)
        for e in call_graph.get("edges", []):
            caller = e.get("caller")
            callee = e.get("callee")
            if caller and callee:
                callers_map[callee].add(caller)
                callees_map[caller].add(callee)

        callback_roots_by_fn: dict[str, set[str]] = defaultdict(set)
        for cb in callbacks:
            rf = cb.get("root_function")
            if rf:
                callback_roots_by_fn[rf].add(rf)
        reach_raw = callback_graph.get("reachable", {})
        callback_reach: dict[str, set[str]] = {}
        for root, nodes in (reach_raw.items() if isinstance(reach_raw, dict) else []):
            if isinstance(nodes, list):
                callback_reach[str(root)] = set(str(x) for x in nodes)

        missing_list = _parse_json_list(row.get("missing_modified_functions", ""))
        for miss in missing_list:
            if ":" not in miss:
                continue
            miss_file, miss_fn = miss.rsplit(":", 1)
            miss_file = miss_file.strip()
            miss_fn = miss_fn.strip()
            if not miss_file or not miss_fn:
                continue

            source_file = extracted / "old_module_sources" / miss_file
            present_in_old_source = False
            if source_file.exists() and source_file.is_file():
                text = source_file.read_text(encoding="utf-8", errors="replace")
                try:
                    present_in_old_source = miss_fn in _extract_functions_for_file(text, miss_file)
                except Exception:
                    present_in_old_source = miss_fn in text

            detected_in_driver_functions = (miss_file, miss_fn) in fn_index

            fn_syms = _function_symbol_usage(used_symbols, miss_fn)
            kernel_symbols_detected_count = len(fn_syms)
            sample = sorted({f"{s.get('name')}({s.get('kind')})" for s in fn_syms})[:12]

            sym_keys = {(s.get("name"), s.get("kind")) for s in fn_syms}
            loc_matches = [loc_by_sym[k] for k in sym_keys if k in loc_by_sym]
            symbols_with_locations_count = 0
            for it in loc_matches:
                if it.get("old_locations") or it.get("new_locations"):
                    symbols_with_locations_count += 1

            diff_matches = [diff_by_sym[k] for k in sym_keys if k in diff_by_sym]
            relevant_evidence = []
            migration_count = 0
            replacements: list[str] = []
            for it in diff_matches:
                tier = _symbol_tier(it)
                if tier in {"hard", "semantic"}:
                    relevant_evidence.append(it)
                if it.get("migration_signal"):
                    migration_count += 1
                    for c in it.get("replacement_candidates", []):
                        sym = c.get("symbol")
                        if sym and sym not in replacements:
                            replacements.append(sym)

            alloc_like_pattern = any(str(s.get("name", "")).lower() in ALLOC_FAMILY for s in fn_syms)
            impact_contains_function = miss_fn in affected_fn_names

            exclusion_reason, excluded_by_filter_or_tier = _infer_impact_exclusion_reason(
                impact_graph=impact_graph,
                fn=miss_fn,
                has_relevant_evidence=bool(relevant_evidence),
                has_migration_signal=migration_count > 0,
                has_locations=symbols_with_locations_count > 0,
            )

            callers = callers_map.get(miss_fn, set())
            callees = callees_map.get(miss_fn, set())
            cb_roots = callback_roots_by_fn.get(miss_fn, set())
            if not cb_roots:
                for root, nodes in callback_reach.items():
                    if miss_fn in nodes:
                        cb_roots.add(root)
            would_exp = _would_expansion_include(
                fn=miss_fn,
                affected_functions={x for x in affected_fn_names if isinstance(x, str)},
                callers=callers,
                callees=callees,
                callback_roots=cb_roots,
                callback_reach=callback_reach,
            )

            label, supports = _label_record(
                present_in_old_source=present_in_old_source,
                detected_in_driver_functions=detected_in_driver_functions,
                kernel_symbols_detected_count=kernel_symbols_detected_count,
                symbols_with_locations_count=symbols_with_locations_count,
                relevant_kernel_diff_evidence_count=len(relevant_evidence),
                migration_signal_count=migration_count,
                alloc_like_pattern=alloc_like_pattern,
                impact_contains_function=impact_contains_function,
                excluded_by_filter_or_tier=excluded_by_filter_or_tier,
                would_expansion_include=would_exp,
            )

            notes = []
            if alloc_like_pattern:
                notes.append("allocator-family call pattern detected in missing function")
            if not relevant_evidence:
                notes.append("no hard/semantic evidence reached impact graph seed stage")

            rec = MissRecord(
                case_id=case_id,
                module_path=module_path,
                run_dir=run_dir.as_posix(),
                missing_function=miss_fn,
                missing_file=miss_file,
                present_in_old_source=present_in_old_source,
                detected_in_driver_functions=detected_in_driver_functions,
                kernel_symbols_detected_count=kernel_symbols_detected_count,
                kernel_symbols_detected_sample=json.dumps(sample, ensure_ascii=False),
                definition_locations_count=len(loc_matches),
                symbols_with_locations_count=symbols_with_locations_count,
                relevant_kernel_diff_evidence_count=len(relevant_evidence),
                migration_signal_count=migration_count,
                replacement_candidates=json.dumps(replacements, ensure_ascii=False),
                impact_contains_function=impact_contains_function,
                impact_exclusion_reason=exclusion_reason,
                excluded_by_filter_or_tier=excluded_by_filter_or_tier,
                would_expansion_include=would_exp,
                direct_callers_count=len(callers),
                direct_callees_count=len(callees),
                callback_root_count=len(cb_roots),
                root_cause_label=label,
                supporting_labels=json.dumps(supports, ensure_ascii=False),
                notes="; ".join(notes),
            )
            all_records.append(rec)
            by_case[case_id].append(rec)

    csv_path = out_dir / "affected_miss_root_cause.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "case_id",
                "module_path",
                "run_dir",
                "missing_file",
                "missing_function",
                "present_in_old_source",
                "detected_in_driver_functions",
                "kernel_symbols_detected_count",
                "kernel_symbols_detected_sample",
                "definition_locations_count",
                "symbols_with_locations_count",
                "relevant_kernel_diff_evidence_count",
                "migration_signal_count",
                "replacement_candidates",
                "impact_contains_function",
                "impact_exclusion_reason",
                "excluded_by_filter_or_tier",
                "would_expansion_include",
                "direct_callers_count",
                "direct_callees_count",
                "callback_root_count",
                "root_cause_label",
                "supporting_labels",
                "notes",
            ],
        )
        writer.writeheader()
        for r in all_records:
            writer.writerow(r.__dict__)

    for case_id, recs in sorted(by_case.items()):
        module_path = recs[0].module_path if recs else ""
        text = _case_report(case_id, module_path, recs)
        write_text(case_reports_dir / f"{slugify(case_id)}_affected_miss.md", text)

    label_counts = Counter(r.root_cause_label for r in all_records)
    summary = {
        "baseline_dir": baseline_dir.as_posix(),
        "miss_cases": len(by_case),
        "missing_function_records": len(all_records),
        "label_counts": {k: int(label_counts.get(k, 0)) for k in LABELS},
        "cases": sorted(by_case.keys()),
    }
    write_json(out_dir / "affected_miss_root_cause_summary.json", summary)

    md: list[str] = []
    md.append("# Affected-Function Miss Root Cause Report")
    md.append("")
    md.append(f"- baseline_dir: `{baseline_dir}`")
    md.append(f"- miss_cases: {summary['miss_cases']}")
    md.append(f"- missing_function_records: {summary['missing_function_records']}")
    md.append("")
    md.append("## Label Counts")
    for label in LABELS:
        md.append(f"- {label}: {summary['label_counts'].get(label, 0)}")
    md.append("")
    md.append("## Files")
    md.append(f"- `{csv_path}`")
    md.append(f"- `{out_dir / 'affected_miss_root_cause_summary.json'}`")
    md.append(f"- `{case_reports_dir}`")
    write_text(out_dir / "affected_miss_report.md", "\n".join(md) + "\n")

    print(json.dumps({"out_dir": out_dir.as_posix(), "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
