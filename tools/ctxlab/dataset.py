"""Case-pack loading, schema validation and catalog statistics.

Canonical home of the dataset contract. `load_case` accepts both pack shapes:

  - mined_migration_bench packs: kernel_evidence.json {"gold": [...], "distractors": [...]}
  - intree_* packs: kernel_evidence.json {"items": [...]} split into gold/distractors
    via expected_semantic_keys.json required_added_symbols.

tools/run_ppl_signal_experiment.py imports from here; the miner writes to this
schema. Everything loaded here is evaluation-only data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def evidence_text(item: dict[str, Any]) -> str:
    """Render one kernel-evidence item as context text (both pack shapes)."""
    parts = [f"symbol: {item.get('symbol', '?')}"]
    for key in ("definition_diff", "old_definition", "new_definition",
                "old_definition_excerpt", "new_definition_excerpt"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"--- {key} ---\n{val.strip()}")
    for side in ("old_locations", "new_locations"):
        locs = item.get(side) or []
        lines = [f"{loc.get('path')}:{loc.get('line')}: {loc.get('text', '')}"
                 for loc in locs if isinstance(loc, dict)]
        if lines:
            parts.append(f"--- {side} ---\n" + "\n".join(lines))
    return "\n".join(parts)


def _split_intree_items(case_dir: Path, items: list[dict[str, Any]]) -> tuple[list, list]:
    gold_symbols: set[str] = set()
    keys_path = case_dir / "expected_semantic_keys.json"
    if keys_path.exists():
        keys = json.loads(keys_path.read_text())
        for sym in keys.get("required_added_symbols") or []:
            gold_symbols.add(sym)
        for pat in keys.get("expected_removed_patterns") or []:
            gold_symbols.add(pat.rstrip("(").split("(")[0])
    gold = [it for it in items
            if any(g and g in (it.get("symbol") or "") or (it.get("symbol") or "") in g
                   for g in gold_symbols)]
    return gold, [it for it in items if it not in gold]


def load_case(case_dir: Path) -> Optional[dict[str, Any]]:
    """Load one case pack; None if the directory is not a usable pack."""
    case_json = case_dir / "case.json"
    reference = case_dir / "reference.diff"
    evidence_path = case_dir / "kernel_evidence.json"
    if not (case_json.exists() and reference.exists() and evidence_path.exists()):
        return None
    meta = json.loads(case_json.read_text())
    evidence = json.loads(evidence_path.read_text())

    if "gold" in evidence:
        gold_items = evidence.get("gold") or []
        distractor_items = evidence.get("distractors") or []
    else:
        gold_items, distractor_items = _split_intree_items(case_dir, evidence.get("items") or [])

    sources = []
    src_root = case_dir / "old_module_sources"
    if src_root.exists():
        for path in sorted(src_root.rglob("*")):
            if path.is_file() and path.suffix in (".c", ".h"):
                sources.append(path.read_text(errors="replace"))
    if not sources:
        return None
    return {
        "case_id": meta.get("case_id", case_dir.name),
        "meta": meta,
        "reference_diff": reference.read_text(errors="replace"),
        "gold_items": gold_items,
        "distractor_items": distractor_items,
        "gold_text": "\n\n".join(evidence_text(it) for it in gold_items),
        "distractor_text": "\n\n".join(evidence_text(it) for it in distractor_items),
        "old_source": "\n\n".join(sources),
    }


def case_dirs(dataset_dir: Path) -> list[Path]:
    for root in (dataset_dir / "cases", dataset_dir):
        if root.exists():
            dirs = sorted(d for d in root.iterdir()
                          if d.is_dir() and (d / "case.json").exists())
            if dirs:
                return dirs
    return []


def load_cases(dataset_dir: Path, max_cases: int = 0,
               verbose: bool = True) -> list[dict[str, Any]]:
    cases = []
    for d in case_dirs(dataset_dir):
        case = load_case(d)
        if case is None:
            if verbose:
                print(f"[dataset] skip {d.name}: incomplete case pack")
            continue
        cases.append(case)
        if max_cases and len(cases) >= max_cases:
            break
    return cases


# ---------------------------------------------------------------------------
# schema validation (errors as data, for the checks runner and future miners)
# ---------------------------------------------------------------------------

_REQUIRED_META_KEYS = (
    "case_id", "commit", "old_ref", "new_ref", "module_file", "module_path",
    "kernel_symbols", "distractor_symbols", "caller_changed_lines",
    "treewide_scale", "mining", "evaluation_only",
)


def validate_case_dir(case_dir: Path) -> list[str]:
    """Return a list of schema violations for a mined case pack (empty = ok)."""
    errors = []
    for name in ("case.json", "kernel_evidence.json", "reference.diff", "kernel_side.diff"):
        if not (case_dir / name).exists():
            errors.append(f"missing {name}")
    if errors:
        return errors
    try:
        meta = json.loads((case_dir / "case.json").read_text())
    except json.JSONDecodeError as exc:
        return [f"case.json unparsable: {exc}"]
    for key in _REQUIRED_META_KEYS:
        if key not in meta:
            errors.append(f"case.json missing key {key}")
    if meta.get("evaluation_only") is not True:
        errors.append("evaluation_only must be true")
    try:
        evidence = json.loads((case_dir / "kernel_evidence.json").read_text())
    except json.JSONDecodeError as exc:
        return errors + [f"kernel_evidence.json unparsable: {exc}"]
    if "gold" not in evidence or "distractors" not in evidence:
        errors.append("kernel_evidence.json must have gold/distractors")
    else:
        ev_gold = sorted(it.get("symbol", "") for it in evidence["gold"])
        if ev_gold != sorted(meta.get("kernel_symbols") or []):
            errors.append("evidence gold symbols disagree with case.json kernel_symbols")
        for it in evidence["gold"]:
            if not (it.get("definition_diff") or it.get("new_locations")):
                errors.append(f"gold item {it.get('symbol')} has no evidence payload")
    src_root = case_dir / "old_module_sources"
    if not src_root.exists() or not any(src_root.rglob("*.c")):
        errors.append("old_module_sources has no .c file")
    reference = (case_dir / "reference.diff").read_text(errors="replace")
    for sym in meta.get("kernel_symbols") or []:
        if sym not in reference:
            errors.append(f"gold symbol {sym} not present in reference.diff")
    return errors


def catalog_stats(dataset_dir: Path) -> dict[str, Any]:
    """Recompute catalog-style statistics from the packs themselves."""
    dirs = case_dirs(dataset_dir)
    metas = []
    for d in dirs:
        try:
            metas.append(json.loads((d / "case.json").read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    gold_counts = [len(m.get("kernel_symbols") or []) for m in metas]
    changed = [m.get("caller_changed_lines", 0) for m in metas]
    return {
        "cases": len(metas),
        "zero_gold_cases": sum(1 for n in gold_counts if n == 0),
        "gold_count_histogram": {str(n): gold_counts.count(n) for n in sorted(set(gold_counts))},
        "caller_changed_lines_min_median_max": (
            (min(changed), sorted(changed)[len(changed) // 2], max(changed)) if changed else None),
        "treewide_scale_max": max((m.get("treewide_scale", 0) for m in metas), default=0),
    }
