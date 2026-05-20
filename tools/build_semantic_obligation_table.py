#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_sym_key(sym_key: str) -> tuple[str, str]:
    m = re.match(r"^(.*)\s+\(([^()]*)\)$", str(sym_key).strip())
    if not m:
        return str(sym_key).strip(), "unknown"
    return m.group(1).strip(), m.group(2).strip()


def _conf_score(level: str) -> int:
    lv = str(level or "").lower()
    if lv == "high":
        return 3
    if lv == "medium":
        return 2
    if lv == "low":
        return 1
    return 0


def _best_conf(a: str, b: str) -> str:
    return a if _conf_score(a) >= _conf_score(b) else b


def _semantic_kind_from_change(change_type: str, migration_signal: bool) -> str:
    ct = str(change_type or "unknown")
    if migration_signal or ct == "api_migration_signal":
        return "source_compatible_semantic_migration"
    if ct in {"return_value_change", "ownership_change", "locking_change"}:
        return ct
    return "none"


def _strength_from_row(*, migration_signal: bool, replacement_candidates: list[dict[str, Any]], confidence: str) -> str:
    if migration_signal and replacement_candidates:
        return "strong"
    if migration_signal:
        return "medium"
    if _conf_score(confidence) >= 3:
        return "strong"
    if _conf_score(confidence) >= 2:
        return "medium"
    return "low"


@dataclass
class CaseCtx:
    run_dir: Path
    case_id: str
    module_path: str
    old_ref: str
    new_ref: str


def _iter_case_contexts(runs_dir: Path, old_ref: str | None, new_ref: str | None) -> list[CaseCtx]:
    out: list[CaseCtx] = []
    for md in runs_dir.rglob("metadata.json"):
        try:
            meta = _read_json(md)
        except Exception:
            continue
        mode = meta.get("mode", {}) or {}
        oref = str(mode.get("old_ref") or meta.get("old_ref") or "")
        nref = str(mode.get("new_ref") or meta.get("new_ref") or "")
        if old_ref and oref != old_ref:
            continue
        if new_ref and nref != new_ref:
            continue
        module_path = str(meta.get("module_path") or "")
        case_id = str(meta.get("case_id") or md.parent.parent.name)
        if not module_path:
            continue
        out.append(CaseCtx(run_dir=md.parent, case_id=case_id, module_path=module_path, old_ref=oref, new_ref=nref))
    return sorted(out, key=lambda x: x.run_dir.as_posix())


def _add_commit_hashes(dst: set[str], commits: list[dict[str, Any]]) -> None:
    for c in commits or []:
        h = str(c.get("hash", "")).strip()
        if h:
            dst.add(h)


def _sample_list(values: set[str], n: int = 20) -> list[str]:
    return sorted(list(values))[:n]


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            item: dict[str, Any] = {}
            for k in fieldnames:
                v = r.get(k)
                if isinstance(v, (list, dict)):
                    item[k] = json.dumps(v, ensure_ascii=False)
                else:
                    item[k] = v
            w.writerow(item)


def build_table(runs_dir: Path, old_ref: str | None, new_ref: str | None) -> dict[str, Any]:
    cases = _iter_case_contexts(runs_dir, old_ref, new_ref)

    # 1) Kernel evidence rows aggregated by (ref pair, symbol, kind).
    kernel_agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    # 2) Driver impact pattern rows aggregated by (ref pair, module, function, symbol, kind).
    impact_agg: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}

    for c in cases:
        extracted = c.run_dir / "extracted"
        kd_path = extracted / "kernel_definition_diffs.json"
        ig_path = extracted / "impact_graph.json"
        if not kd_path.exists() or not ig_path.exists():
            continue

        kernel_diffs = _read_json(kd_path)
        impact = _read_json(ig_path)

        # Kernel evidence aggregation.
        for item in kernel_diffs.get("items", []) or []:
            symbol = str(item.get("symbol", "")).strip()
            kind = str(item.get("kind", "unknown")).strip() or "unknown"
            if not symbol:
                continue

            definition_changed = bool(item.get("definition_changed", False))
            migration_signal = bool(item.get("migration_signal", False))
            change_type = str(item.get("change_type_guess", "unknown"))
            replacement_candidates = list(item.get("replacement_candidates", []) or [])
            confidence = str(item.get("confidence", "low"))

            # Keep only rows with useful evidence.
            if not definition_changed and not migration_signal and change_type == "unknown":
                continue

            key = (c.old_ref, c.new_ref, symbol, kind)
            row = kernel_agg.get(key)
            if not row:
                row = {
                    "old_ref": c.old_ref,
                    "new_ref": c.new_ref,
                    "symbol": symbol,
                    "kind": kind,
                    "definition_changed": definition_changed,
                    "migration_signal": migration_signal,
                    "change_type_votes": Counter([change_type]),
                    "replacement_candidates": replacement_candidates,
                    "confidence": confidence,
                    "source_cases": set(),
                    "source_modules": set(),
                    "commit_hashes": set(),
                    "open_question_samples": set(),
                }
                kernel_agg[key] = row
            else:
                row["definition_changed"] = bool(row["definition_changed"]) or definition_changed
                row["migration_signal"] = bool(row["migration_signal"]) or migration_signal
                row["change_type_votes"][change_type] += 1
                row["confidence"] = _best_conf(str(row.get("confidence", "low")), confidence)
                existing = {str(x.get("symbol", "")): x for x in row.get("replacement_candidates", []) if x.get("symbol")}
                for r in replacement_candidates:
                    rs = str(r.get("symbol", "")).strip()
                    if rs and rs not in existing:
                        existing[rs] = r
                row["replacement_candidates"] = list(existing.values())

            row["source_cases"].add(c.case_id)
            row["source_modules"].add(c.module_path)
            _add_commit_hashes(row["commit_hashes"], item.get("commits", []) or [])
            for q in item.get("open_questions", []) or []:
                qs = str(q).strip()
                if qs:
                    row["open_question_samples"].add(qs)

        # Impact pattern aggregation from affected functions.
        for af in impact.get("affected_functions", []) or []:
            fn = str(af.get("function", "")).strip()
            file_path = str(af.get("file", "")).strip()
            tier = str(af.get("tier", "context")).strip() or "context"
            if not fn:
                continue

            uses = list(af.get("uses_changed_kernel_symbols", []) or [])
            cb_roots = list(af.get("callback_roots", []) or [])
            trig_reasons = list(af.get("trigger_reasons", []) or [])
            cb_kinds = sorted(
                {
                    str(x.get("root_kind", "unknown"))
                    for x in cb_roots
                    if isinstance(x, dict)
                }
            )
            trigger_reason_texts = sorted(
                {
                    ",".join(sorted([str(r) for r in (x.get("reasons", []) or [])]))
                    for x in trig_reasons
                    if isinstance(x, dict)
                }
            )

            if not uses:
                key = (c.old_ref, c.new_ref, c.module_path, fn, "<none>", "unknown")
                row = impact_agg.get(key)
                if not row:
                    row = {
                        "old_ref": c.old_ref,
                        "new_ref": c.new_ref,
                        "module_path": c.module_path,
                        "function": fn,
                        "file": file_path,
                        "symbol": "<none>",
                        "kind": "unknown",
                        "max_tier": tier,
                        "callback_root_kinds": set(cb_kinds),
                        "trigger_reason_samples": set(trigger_reason_texts),
                        "source_cases": set(),
                        "source_run_dirs": set(),
                    }
                    impact_agg[key] = row
                else:
                    if row.get("max_tier") != "hard" and tier == "hard":
                        row["max_tier"] = "hard"
                    elif row.get("max_tier") == "context" and tier == "semantic":
                        row["max_tier"] = "semantic"
                    row["callback_root_kinds"].update(cb_kinds)
                    row["trigger_reason_samples"].update(trigger_reason_texts)
                row["source_cases"].add(c.case_id)
                row["source_run_dirs"].add(c.run_dir.as_posix())
                continue

            for sym_key in uses:
                sym, kind = _parse_sym_key(str(sym_key))
                key = (c.old_ref, c.new_ref, c.module_path, fn, sym, kind)
                row = impact_agg.get(key)
                if not row:
                    row = {
                        "old_ref": c.old_ref,
                        "new_ref": c.new_ref,
                        "module_path": c.module_path,
                        "function": fn,
                        "file": file_path,
                        "symbol": sym,
                        "kind": kind,
                        "max_tier": tier,
                        "callback_root_kinds": set(cb_kinds),
                        "trigger_reason_samples": set(trigger_reason_texts),
                        "source_cases": set(),
                        "source_run_dirs": set(),
                    }
                    impact_agg[key] = row
                else:
                    if row.get("max_tier") != "hard" and tier == "hard":
                        row["max_tier"] = "hard"
                    elif row.get("max_tier") == "context" and tier == "semantic":
                        row["max_tier"] = "semantic"
                    row["callback_root_kinds"].update(cb_kinds)
                    row["trigger_reason_samples"].update(trigger_reason_texts)
                row["source_cases"].add(c.case_id)
                row["source_run_dirs"].add(c.run_dir.as_posix())

    kernel_rows: list[dict[str, Any]] = []
    for i, row in enumerate(sorted(kernel_agg.values(), key=lambda x: (x["old_ref"], x["new_ref"], x["symbol"], x["kind"])), start=1):
        votes: Counter[str] = row.get("change_type_votes", Counter())
        top_change = "unknown"
        if votes:
            top_change = votes.most_common(1)[0][0]
        replacement_candidates = list(row.get("replacement_candidates", []) or [])
        migration_signal = bool(row.get("migration_signal", False))
        confidence = str(row.get("confidence", "low"))
        semantic_kind = _semantic_kind_from_change(top_change, migration_signal)
        strength = _strength_from_row(
            migration_signal=migration_signal,
            replacement_candidates=replacement_candidates,
            confidence=confidence,
        )

        kernel_rows.append(
            {
                "row_id": f"KER-{i:06d}",
                "old_ref": row["old_ref"],
                "new_ref": row["new_ref"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "definition_changed": bool(row.get("definition_changed", False)),
                "migration_signal": migration_signal,
                "change_type_guess": top_change,
                "change_type_vote_count": dict(votes),
                "semantic_kind": semantic_kind,
                "strength": strength,
                "confidence": confidence,
                "replacement_candidates": replacement_candidates,
                "source_case_count": len(row["source_cases"]),
                "source_module_count": len(row["source_modules"]),
                "sample_cases": _sample_list(row["source_cases"]),
                "sample_modules": _sample_list(row["source_modules"]),
                "evidence_commits": _sample_list(row["commit_hashes"]),
                "open_question_samples": _sample_list(row["open_question_samples"]),
            }
        )

    impact_rows: list[dict[str, Any]] = []
    for i, row in enumerate(sorted(impact_agg.values(), key=lambda x: (x["old_ref"], x["new_ref"], x["module_path"], x["function"], x["symbol"], x["kind"])), start=1):
        impact_rows.append(
            {
                "row_id": f"IMP-{i:06d}",
                "old_ref": row["old_ref"],
                "new_ref": row["new_ref"],
                "module_path": row["module_path"],
                "function": row["function"],
                "file": row.get("file", ""),
                "symbol": row["symbol"],
                "kind": row["kind"],
                "max_tier": row.get("max_tier", "context"),
                "callback_root_kinds": sorted(list(row.get("callback_root_kinds", set()))),
                "trigger_reason_samples": sorted(list(row.get("trigger_reason_samples", set())))[:20],
                "source_case_count": len(row["source_cases"]),
                "sample_cases": _sample_list(row["source_cases"]),
                "sample_run_dirs": _sample_list(row["source_run_dirs"]),
            }
        )

    by_ref_symbol: dict[str, list[str]] = defaultdict(list)
    for r in kernel_rows:
        key = f"{r['old_ref']}|{r['new_ref']}|{r['symbol']}|{r['kind']}"
        by_ref_symbol[key].append(r["row_id"])

    return {
        "meta": {
            "runs_dir": runs_dir.as_posix(),
            "old_ref_filter": old_ref or "",
            "new_ref_filter": new_ref or "",
            "case_count": len(cases),
            "kernel_evidence_row_count": len(kernel_rows),
            "driver_impact_pattern_row_count": len(impact_rows),
        },
        "kernel_evidence_rows": kernel_rows,
        "driver_impact_pattern_rows": impact_rows,
        # Backward compatibility aliases for existing consumers.
        "kernel_symbol_rows": kernel_rows,
        "driver_function_rows": impact_rows,
        "indexes": {
            "by_ref_symbol": dict(by_ref_symbol),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build kernel evidence + driver impact pattern tables from existing run artifacts")
    ap.add_argument("--runs-dir", required=True, help="root directory containing run artifacts")
    ap.add_argument("--out-dir", required=True, help="output directory for table files")
    ap.add_argument("--old-ref", default=None, help="optional old-ref filter")
    ap.add_argument("--new-ref", default=None, help="optional new-ref filter")
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    table = build_table(runs_dir=runs_dir, old_ref=args.old_ref, new_ref=args.new_ref)
    json_path = out_dir / "semantic_obligation_table.json"
    json_path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    _write_csv(
        out_dir / "kernel_evidence_table.csv",
        table.get("kernel_evidence_rows", []),
        [
            "row_id",
            "old_ref",
            "new_ref",
            "symbol",
            "kind",
            "definition_changed",
            "migration_signal",
            "change_type_guess",
            "change_type_vote_count",
            "semantic_kind",
            "strength",
            "confidence",
            "replacement_candidates",
            "source_case_count",
            "source_module_count",
            "sample_modules",
            "sample_cases",
            "evidence_commits",
            "open_question_samples",
        ],
    )
    _write_csv(
        out_dir / "driver_impact_pattern_table.csv",
        table.get("driver_impact_pattern_rows", []),
        [
            "row_id",
            "old_ref",
            "new_ref",
            "module_path",
            "function",
            "file",
            "symbol",
            "kind",
            "max_tier",
            "callback_root_kinds",
            "trigger_reason_samples",
            "source_case_count",
            "sample_cases",
            "sample_run_dirs",
        ],
    )

    # Backward compatibility CSV names.
    _write_csv(
        out_dir / "kernel_semantic_obligation_table.csv",
        table.get("kernel_evidence_rows", []),
        [
            "row_id",
            "old_ref",
            "new_ref",
            "symbol",
            "kind",
            "semantic_kind",
            "strength",
            "confidence",
            "change_type_guess",
            "definition_changed",
            "migration_signal",
            "source_case_count",
            "source_module_count",
            "replacement_candidates",
            "sample_modules",
            "sample_cases",
            "evidence_commits",
        ],
    )
    _write_csv(
        out_dir / "driver_semantic_obligation_table.csv",
        table.get("driver_impact_pattern_rows", []),
        [
            "row_id",
            "old_ref",
            "new_ref",
            "module_path",
            "function",
            "symbol",
            "kind",
            "max_tier",
            "source_case_count",
            "sample_cases",
            "sample_run_dirs",
        ],
    )

    print(
        json.dumps(
            {
                "out_dir": out_dir.as_posix(),
                "json": json_path.as_posix(),
                "kernel_evidence_rows": len(table.get("kernel_evidence_rows", [])),
                "driver_impact_pattern_rows": len(table.get("driver_impact_pattern_rows", [])),
                "cases": table.get("meta", {}).get("case_count", 0),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
