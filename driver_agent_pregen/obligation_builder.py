from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import read_json, write_json


_SKIP_SYMBOLS = {
    "__alignof__",
    "__alignof",
    "__builtin_offsetof",
    "__builtin_types_compatible_p",
    "sizeof",
}


def _checks_for_change(change_type: str, old_missing_new: bool) -> tuple[list[str], list[str], str]:
    hard: list[str] = []
    semantic: list[str] = []
    seed = ""

    if old_missing_new:
        hard.append("old symbol should not remain in patched driver")
        semantic.append("replacement must preserve return/error/resource behavior")
        seed = "replace removed symbol; preserve behavior and cleanup paths"
        return hard, semantic, seed

    if change_type in {"api_deprecation", "rename"}:
        hard.append("deprecated/renamed symbol should not remain")
        semantic.append("replacement call must preserve return and error behavior")
        seed = "update symbol usage to replacement and re-check semantic equivalence"
    elif change_type == "signature_change":
        hard.append("call sites must match new argument list")
        semantic.append("inserted/removed arguments must preserve old behavior")
        seed = "update call signature and validate argument semantics"
    elif change_type == "struct_field_change":
        hard.append("removed/renamed field must not be accessed")
        semantic.append("replacement field/helper should preserve meaning")
        seed = "migrate field access to supported helper/field"
    elif change_type == "return_value_change":
        semantic.append("success/error branch handling must follow new convention")
        seed = "adjust branch logic around changed return semantics"
    elif change_type == "ownership_change":
        semantic.append("no leak and no double-free; get/put or alloc/free pairing preserved")
        seed = "reconcile ownership/lifetime transitions on all paths"
    elif change_type == "locking_change":
        semantic.append("caller context must satisfy lock/unlock requirement")
        seed = "ensure lock state and context constraints at call site"
    elif change_type == "include_dependency":
        hard.append("required header should be directly included when needed")
        seed = "fix include dependency and verify build visibility"
    elif change_type == "api_migration_signal":
        semantic.append("evaluate in-tree migration pattern and adopt replacement API if semantics match")
        seed = "inspect migration signal and update callsite if replacement is required"
    else:
        semantic.append("re-check usage against new kernel definition")
        seed = "inspect changed contract and align call site behavior"

    return hard, semantic, seed


def _confidence_from_diff(item: dict) -> str:
    if item.get("migration_signal"):
        return "medium"
    if item.get("confidence") == "high":
        return "high"
    if item.get("definition_changed"):
        return "medium"
    return "low"


def _semantic_obligation_kind(item: dict) -> str | None:
    if item.get("migration_signal"):
        return "source_compatible_semantic_migration"
    ct = item.get("change_type_guess", "unknown")
    if ct in {"return_value_change", "ownership_change", "locking_change"}:
        return ct
    return None


def _semantic_strength(item: dict) -> str:
    if item.get("kernel_side_helper_signal") and item.get("replacement_candidates"):
        # Kernel-side helper discovery is useful guidance, but it is not direct
        # proof that the old source-compatible callsite is wrong. Keep it below
        # "strong" unless a semantic table or hard API evidence reinforces it.
        return "medium"
    if item.get("migration_signal") and item.get("replacement_candidates"):
        return "strong"
    if item.get("migration_signal"):
        return "medium"
    if item.get("confidence") == "high":
        return "strong"
    return "medium"


def _build_semantic_statement(item: dict, sym_key: str) -> str:
    if item.get("migration_signal"):
        reps = [c.get("symbol") for c in item.get("replacement_candidates", []) if c.get("symbol")]
        if reps:
            return (
                f"{sym_key} appears source-compatible but has migration evidence; "
                f"evaluate replacement usage ({', '.join(reps[:3])}) for semantic alignment."
            )
        return f"{sym_key} has migration signal; verify old usage semantics and migrate if required by evidence."

    ct = item.get("change_type_guess", "unknown")
    if ct == "return_value_change":
        return f"{sym_key} return-value handling should align with new semantic convention."
    if ct == "ownership_change":
        return f"{sym_key} ownership/lifetime handling must preserve resource pairing on all paths."
    if ct == "locking_change":
        return f"{sym_key} call-context locking requirements should be re-validated."
    return f"{sym_key} requires semantic-level compatibility validation."


def _strength_score(level: str) -> int:
    lv = str(level or "").lower()
    if lv == "strong":
        return 3
    if lv == "medium":
        return 2
    if lv == "low":
        return 1
    return 0


def _load_semantic_obligation_table(table_path: str | None) -> dict[str, Any]:
    if not table_path:
        return {}
    p = Path(table_path).expanduser().resolve()
    if not p.exists():
        return {}
    data = read_json(p, default={}) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _lookup_table_rows(
    *,
    table: dict[str, Any],
    old_ref: str | None,
    new_ref: str | None,
    symbol: str,
    kind: str,
) -> list[dict[str, Any]]:
    rows = list(table.get("kernel_evidence_rows", []) or table.get("kernel_symbol_rows", []) or [])
    if not rows:
        return []

    # Fast path via optional prebuilt index.
    idx = table.get("indexes", {}).get("by_ref_symbol", {})
    key = f"{old_ref or ''}|{new_ref or ''}|{symbol}|{kind}"
    id_to_row = {str(r.get("row_id")): r for r in rows if isinstance(r, dict) and r.get("row_id")}
    if isinstance(idx, dict) and key in idx:
        out = []
        for rid in idx.get(key, []) or []:
            row = id_to_row.get(str(rid))
            if row:
                out.append(row)
        if out:
            return out

    # Fallback scan.
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")) != symbol:
            continue
        if str(row.get("kind", "unknown")) != kind:
            continue
        if old_ref and str(row.get("old_ref", "")) != old_ref:
            continue
        if new_ref and str(row.get("new_ref", "")) != new_ref:
            continue
        out.append(row)
    return out


def _derive_from_table_hits(
    *,
    table_hits: list[dict[str, Any]],
    current_strength: str,
    current_replacement_candidates: list[dict[str, Any]],
) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not table_hits:
        return False, current_strength, current_replacement_candidates, []

    derived_migration = False
    best_strength = current_strength
    repl_map: dict[str, dict[str, Any]] = {}
    hit_evidence: list[dict[str, Any]] = []

    for c in current_replacement_candidates or []:
        sym = str(c.get("symbol", "")).strip()
        if sym:
            repl_map[sym] = c

    for hit in table_hits:
        sem_kind = str(hit.get("semantic_kind", "")).strip()
        if sem_kind == "source_compatible_semantic_migration":
            derived_migration = True
        hit_strength = str(hit.get("strength", "")).lower()
        if _strength_score(hit_strength) > _strength_score(best_strength):
            best_strength = hit_strength

        for c in hit.get("replacement_candidates", []) or []:
            sym = str(c.get("symbol", "")).strip()
            if not sym:
                continue
            if sym in repl_map:
                continue
            repl_map[sym] = {
                "symbol": sym,
                "score": c.get("score", 1),
                "evidence": c.get("evidence", ["semantic_obligation_table"]),
            }

        hit_evidence.append(
            {
                "type": "obligation_table_hit",
                "row_id": hit.get("row_id"),
                "semantic_kind": hit.get("semantic_kind"),
                "strength": hit.get("strength"),
                "source_case_count": hit.get("source_case_count"),
                "source_module_count": hit.get("source_module_count"),
                "sample_modules": hit.get("sample_modules", [])[:5],
            }
        )

    return derived_migration, best_strength, list(repl_map.values()), hit_evidence


def run_obligation_builder(
    extracted_dir,
    kernel_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    *,
    old_ref: str | None = None,
    new_ref: str | None = None,
    module_path: str | None = None,
    semantic_obligation_table: str | None = None,
) -> dict[str, Any]:
    if kernel_diffs.get("affected_func_signal_mode") == "kernel_only":
        semantic_obligation_table = None

    affected_map = {}
    for afn in impact_graph.get("affected_functions", []):
        fn = afn.get("function")
        for sym in afn.get("uses_changed_kernel_symbols", []):
            affected_map.setdefault(sym, set()).add(fn)

    obligations = []
    semantic_obligations = []
    table = _load_semantic_obligation_table(semantic_obligation_table)
    table_hits_summary: list[dict[str, Any]] = []
    idx = 1
    sidx = 1
    for item in kernel_diffs.get("items", []):
        symbol = item.get("symbol")
        kind = item.get("kind", "unknown")
        if str(symbol) in _SKIP_SYMBOLS:
            continue
        sym_key = f"{symbol} ({kind})"

        missing_new = any("missing in new_ref" in q for q in item.get("open_questions", []))
        definition_changed = bool(item.get("definition_changed"))
        migration_signal = bool(item.get("migration_signal"))
        change_type = item.get("change_type_guess", "unknown")
        affected_fns = sorted(affected_map.get(sym_key, set()))
        replacement_candidates = list(item.get("replacement_candidates", []) or [])
        table_hits = _lookup_table_rows(
            table=table,
            old_ref=old_ref,
            new_ref=new_ref,
            symbol=str(symbol),
            kind=str(kind),
        )
        table_migration, table_strength, replacement_candidates, table_evidence = _derive_from_table_hits(
            table_hits=table_hits,
            current_strength=_semantic_strength(item),
            current_replacement_candidates=replacement_candidates,
        )
        effective_migration_signal = bool(migration_signal or table_migration)
        effective_change_type = change_type
        if effective_change_type == "unknown" and effective_migration_signal:
            effective_change_type = "api_migration_signal"
        if table_hits:
            table_hits_summary.append(
                {
                    "symbol": str(symbol),
                    "kind": str(kind),
                    "rows": [h.get("row_id") for h in table_hits if h.get("row_id")],
                    "module_path": module_path,
                }
            )

        # Field/struct regex fallbacks can occasionally attach a nearby kernel
        # diff hunk to an unlocated driver token. If neither old nor new
        # locations exist and there is no include or migration signal, this is
        # not enough evidence for a hard driver obligation.
        unlocated_type_or_field = (
            kind in {"field", "struct"}
            and not (item.get("old_locations") or [])
            and not (item.get("new_locations") or [])
            and not str(item.get("old_definition", "") or "").strip()
            and not str(item.get("new_definition", "") or "").strip()
            and not item.get("direct_include_requirements")
            and not missing_new
            and not effective_migration_signal
        )
        if unlocated_type_or_field:
            continue

        # Keep obligations focused on concrete migration deltas. Include
        # direct-include obligations even when the provider definition itself
        # did not change; these capture source files relying on transitive
        # kernel headers.
        if (
            not definition_changed
            and not missing_new
            and not effective_migration_signal
            and effective_change_type != "include_dependency"
        ):
            continue
        if effective_change_type == "unknown" and not missing_new and not effective_migration_signal:
            continue
        if (
            kind == "field"
            and effective_change_type == "struct_field_change"
            and not missing_new
            and ((item.get("new_locations") or []) or str(item.get("new_definition", "") or "").strip())
        ):
            old_definition = str(item.get("old_definition", "") or "")
            new_definition = str(item.get("new_definition", "") or "")
            if str(symbol) and (f"(*{symbol})" in old_definition or f"(*{symbol})" in new_definition):
                pass
            else:
                # The field is still visible in the new kernel. A surrounding
                # struct diff is useful context, but not a hard migration
                # obligation.
                continue
        if not affected_fns:
            # Skip noisy symbols not linked to any affected function slice.
            continue

        hard, semantic, seed = _checks_for_change(effective_change_type, missing_new)

        evidence = [
            {
                "type": "definition_diff",
                "value": item.get("definition_diff", "")[:4000],
            }
        ]
        for req in item.get("direct_include_requirements", [])[:8]:
            evidence.append({"type": "direct_include_requirement", "value": req})
        evidence.extend(table_evidence)
        for c in item.get("commits", [])[:4]:
            evidence.append(
                {
                    "type": "commit",
                    "hash": c.get("hash"),
                    "subject": c.get("subject"),
                    "message": c.get("message", "")[:2000],
                    "changed_files": c.get("changed_files", [])[:20],
                }
            )

        obligations.append(
            {
                "id": f"OBL-{idx:03d}",
                "affected_kernel_symbol": sym_key,
                "affected_driver_functions": affected_fns,
                "statement": f"Driver usage of {sym_key} must stay compatible with kernel-side evolution between refs.",
                "hard_checks": hard,
                "semantic_checks": semantic,
                "evidence": evidence,
                "confidence": _confidence_from_diff(item),
                "open_questions": item.get("open_questions", []),
                "patch_guidance_seed": seed,
                "migration_signal": effective_migration_signal,
                "semantic_strength": table_strength,
                "replacement_candidates": replacement_candidates,
                "table_hit_rows": [h.get("row_id") for h in table_hits if h.get("row_id")],
            }
        )
        idx += 1

        sem_kind = _semantic_obligation_kind(item)
        if not sem_kind and effective_migration_signal:
            sem_kind = "source_compatible_semantic_migration"
        if sem_kind and affected_fns:
            sem_evidence = []
            if effective_migration_signal:
                sem_evidence.append(
                    {
                        "type": "migration_signal",
                        "replacement_candidates": replacement_candidates,
                        "open_questions": item.get("open_questions", []),
                    }
                )
            sem_evidence.extend(table_evidence)
            for c in item.get("commits", [])[:3]:
                sem_evidence.append(
                    {
                        "type": "commit",
                        "hash": c.get("hash"),
                        "subject": c.get("subject"),
                    }
                )
            semantic_obligations.append(
                {
                    "id": f"SOBL-{sidx:03d}",
                    "kind": sem_kind,
                    "strength": table_strength,
                    "affected_kernel_symbol": sym_key,
                    "affected_driver_functions": affected_fns,
                    "statement": _build_semantic_statement(item, sym_key),
                    "recommended_action": "treat as semantic-level required change when strong evidence is present",
                    "evidence": sem_evidence,
                    "replacement_candidates": replacement_candidates,
                    "confidence": _confidence_from_diff(item),
                    "table_hit_rows": [h.get("row_id") for h in table_hits if h.get("row_id")],
                }
            )
            sidx += 1

    payload = {
        "obligations": obligations,
        "semantic_obligations": semantic_obligations,
        "semantic_obligation_summary": {
            "count": len(semantic_obligations),
            "strong_count": sum(1 for x in semantic_obligations if x.get("strength") == "strong"),
            "kinds": sorted({x.get("kind") for x in semantic_obligations if x.get("kind")}),
        },
        "semantic_obligation_table": {
            "path": semantic_obligation_table or "",
            "loaded": bool(table),
            "matched_symbol_count": len(table_hits_summary),
            "matches": table_hits_summary[:200],
        },
    }
    write_json(extracted_dir / "compatibility_obligations.json", payload)
    return payload
