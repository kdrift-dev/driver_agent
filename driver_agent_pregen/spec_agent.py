from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_pre_spec_prompt
from .utils import read_text


def _load_slices(
    extracted_dir: Path,
    impact_graph: dict[str, Any],
    *,
    list_key: str = "affected_functions",
    max_chars_per_slice: int = 3500,
    max_items: int | None = None,
    symbol_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    slices = []
    for afn in impact_graph.get(list_key, []):
        if max_items is not None and len(slices) >= max_items:
            break
        if symbol_keys is not None:
            uses = {str(x) for x in afn.get("uses_changed_kernel_symbols", []) or []}
            if not uses.intersection(symbol_keys):
                continue
        rel = afn.get("local_slice_path")
        if not rel:
            continue
        p = extracted_dir / rel
        if not p.exists():
            continue
        txt = read_text(p)
        if len(txt) > max_chars_per_slice:
            txt = txt[:max_chars_per_slice] + "\n...[truncated]"
        slices.append(
            {
                "function": afn.get("function"),
                "file": afn.get("file"),
                "slice_path": rel,
                "slice": txt,
            }
        )
    return slices


def _impact_symbol_keys(impact_graph: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in impact_graph.get("affected_functions", []) or []:
        for sym in item.get("uses_changed_kernel_symbols", []) or []:
            if sym:
                keys.add(str(sym))
    return keys


def _required_symbol_keys(compatibility_obligations: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for x in compatibility_obligations.get("obligations", []) or []:
        if x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong":
            sym = str(x.get("affected_kernel_symbol", "")).strip()
            if sym:
                keys.add(sym)
    for x in compatibility_obligations.get("semantic_obligations", []) or []:
        if str(x.get("strength", "")).lower() == "strong":
            sym = str(x.get("affected_kernel_symbol", "")).strip()
            if sym:
                keys.add(sym)
    return keys


def _slim_kernel_diffs_for_impact(
    kernel_definition_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    *,
    symbol_keys: set[str] | None = None,
) -> dict[str, Any]:
    keys = symbol_keys if symbol_keys is not None else _impact_symbol_keys(impact_graph)
    if not keys:
        return {**kernel_definition_diffs, "items": []}
    items = []
    for item in kernel_definition_diffs.get("items", []) or []:
        key = f"{item.get('symbol')} ({item.get('kind')})"
        if key in keys:
            # Keep full evidence on disk; LLM only needs compact excerpts.
            slim = {
                "symbol": item.get("symbol"),
                "kind": item.get("kind"),
                "definition_changed": item.get("definition_changed"),
                "change_type_guess": item.get("change_type_guess"),
                "confidence": item.get("confidence"),
                "replacement_candidates": item.get("replacement_candidates", [])[:3],
                "migration_signal": item.get("migration_signal", False),
                "used_by_driver": (item.get("used_by_driver", []) or [])[:2],
                "direct_include_requirements": item.get("direct_include_requirements", [])[:3],
                "open_questions": item.get("open_questions", [])[:3],
            }
            for field, max_chars in (
                ("old_definition", 250),
                ("new_definition", 250),
                ("definition_diff", 350),
            ):
                text = str(item.get(field, "") or "")
                slim[field] = text[:max_chars] + ("\n...[truncated]" if len(text) > max_chars else "")
            items.append(slim)
    return {**kernel_definition_diffs, "items": items}


def _compact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in evidence[:3]:
        value = ev.get("value")
        if isinstance(value, str):
            value = value[:250] + ("\n...[truncated]" if len(value) > 250 else "")
        elif isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if k in {"file", "required_include", "provider", "line", "symbol", "path"}
            }
        out.append({"type": ev.get("type"), "value": value})
    return out


def _compact_obligation(x: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": x.get("id"),
        "affected_kernel_symbol": x.get("affected_kernel_symbol"),
        "affected_driver_functions": x.get("affected_driver_functions", []),
        "statement": x.get("statement"),
        "kind": x.get("kind"),
        "strength": x.get("strength"),
        "recommended_action": x.get("recommended_action"),
        "hard_checks": x.get("hard_checks", []),
        "semantic_checks": x.get("semantic_checks", []),
        "confidence": x.get("confidence"),
        "semantic_strength": x.get("semantic_strength"),
        "migration_signal": x.get("migration_signal", False),
        "replacement_candidates": x.get("replacement_candidates", [])[:3],
        "patch_guidance_seed": x.get("patch_guidance_seed"),
        "open_questions": x.get("open_questions", [])[:4],
        "evidence": _compact_evidence(x.get("evidence", []) or []),
    }


def _slim_obligations_for_impact(
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    *,
    symbol_keys: set[str] | None = None,
) -> dict[str, Any]:
    keys = symbol_keys if symbol_keys is not None else _impact_symbol_keys(impact_graph)
    if not keys:
        return {**compatibility_obligations, "obligations": [], "semantic_obligations": []}
    out = dict(compatibility_obligations)
    out["obligations"] = [
        _compact_obligation(x)
        for x in compatibility_obligations.get("obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
        and (x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong")
    ]
    out["semantic_obligations"] = [
        _compact_obligation(x)
        for x in compatibility_obligations.get("semantic_obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
        and str(x.get("strength", "")).lower() == "strong"
    ]
    dropped_semantic = [
        x.get("id")
        for x in compatibility_obligations.get("semantic_obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
        and str(x.get("strength", "")).lower() != "strong"
    ]
    out["semantic_obligations_omitted_from_llm"] = {
        "count": len(dropped_semantic)
        + sum(
            1
            for x in compatibility_obligations.get("obligations", []) or []
            if str(x.get("affected_kernel_symbol", "")) in keys
            and not (x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong")
        ),
        "reason": "medium/low semantic guidance is not a required edit and is omitted from LLM payload",
        "ids": dropped_semantic[:20],
    }
    out["llm_payload_filter"] = "impact_symbols_only; full obligations remain in extracted/compatibility_obligations.json"
    return out


def _slim_impact_graph_for_llm(impact_graph: dict[str, Any], *, symbol_keys: set[str] | None = None) -> dict[str, Any]:
    def keep(item: dict[str, Any]) -> bool:
        if symbol_keys is None:
            return True
        uses = {str(x) for x in item.get("uses_changed_kernel_symbols", []) or []}
        return bool(uses.intersection(symbol_keys))

    def slim_fn(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "function": item.get("function"),
            "file": item.get("file"),
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "tier": item.get("tier"),
            "edit_scope": item.get("edit_scope"),
            "uses_changed_kernel_symbols": item.get("uses_changed_kernel_symbols", []),
            "ordinary_callers": item.get("ordinary_callers", [])[:8],
            "ordinary_callees": item.get("ordinary_callees", [])[:8],
            "callback_roots": item.get("callback_roots", [])[:4],
            "trigger_reasons": item.get("trigger_reasons", [])[:8],
        }

    return {
        "module_path": impact_graph.get("module_path"),
        "affected_tier_policy": impact_graph.get("affected_tier_policy", {}),
        "tier_counts": impact_graph.get("tier_counts", {}),
        "affected_functions": [],
        "edit_required_functions": [slim_fn(x) for x in (impact_graph.get("edit_required_functions", []) or []) if keep(x)][:20],
        "edit_candidate_functions": [slim_fn(x) for x in (impact_graph.get("edit_candidate_functions", []) or []) if keep(x)][:12],
        "context_only_functions": [slim_fn(x) for x in (impact_graph.get("context_only_functions", []) or []) if keep(x)][:8],
        "context_functions": [],
        "edges_summary": {
            "count": len(impact_graph.get("edges", []) or []),
            "sample": (impact_graph.get("edges", []) or [])[:12],
        },
        "llm_payload_filter": "compact graph; affected/context duplicates omitted; full graph remains in extracted/impact_graph.json",
    }


def _requires_effective_migration_edit(compatibility_obligations: dict[str, Any]) -> bool:
    for item in compatibility_obligations.get("obligations", []) or []:
        if item.get("hard_checks"):
            return True
        if item.get("replacement_candidates") and str(item.get("semantic_strength", "")).lower() == "strong":
            return True
    for item in compatibility_obligations.get("semantic_obligations", []) or []:
        if str(item.get("strength", "")).lower() == "strong" and item.get("replacement_candidates"):
            return True
    return False


def run_pre_spec_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    kernel_definition_diffs: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "01_pre_spec"
    stage_dir.mkdir(parents=True, exist_ok=True)

    no_required_edit = not _requires_effective_migration_edit(compatibility_obligations)
    if no_required_edit:
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "kernel_definition_diffs": {"items": [], "llm_payload_filter": "omitted because no required migration edit exists"},
            "compatibility_obligations": {
                "obligation_count": len(compatibility_obligations.get("obligations", []) or []),
                "semantic_obligation_count": len(compatibility_obligations.get("semantic_obligations", []) or []),
                "required_obligation_count": 0,
                "llm_payload_filter": "full obligations remain in extracted/compatibility_obligations.json",
            },
            "impact_graph": {
                "tier_counts": impact_graph.get("tier_counts", {}),
                "llm_payload_filter": "omitted because no required migration edit exists",
            },
            "edit_required_function_slices": [],
            "edit_candidate_function_slices": [],
            "context_only_function_slices": [],
            "affected_function_slices": [],
            "context_function_slices": [],
            "slice_payload_filter": "omitted because no required migration edit exists",
            "instruction": "No hard or strong semantic migration edit exists; skip pre-spec generation.",
        }
    else:
        required_keys = _required_symbol_keys(compatibility_obligations)
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "kernel_definition_diffs": _slim_kernel_diffs_for_impact(
                kernel_definition_diffs, impact_graph, symbol_keys=required_keys
            ),
            "compatibility_obligations": _slim_obligations_for_impact(
                compatibility_obligations, impact_graph, symbol_keys=required_keys
            ),
            "impact_graph": _slim_impact_graph_for_llm(impact_graph, symbol_keys=required_keys),
            "edit_required_function_slices": _load_slices(
                extracted_dir,
                impact_graph,
                list_key="edit_required_functions",
                max_items=12,
                symbol_keys=required_keys,
            ),
            "edit_candidate_function_slices": _load_slices(
                extracted_dir,
                impact_graph,
                list_key="edit_candidate_functions",
                max_items=6,
                symbol_keys=required_keys,
            ),
            "context_only_function_slices": _load_slices(
                extracted_dir,
                impact_graph,
                list_key="context_only_functions",
                max_chars_per_slice=1200,
                max_items=4,
                symbol_keys=required_keys,
            ),
            "affected_function_slices": [],
            "context_function_slices": [],
            "slice_payload_filter": "deduplicated; use edit_required/edit_candidate/context_only slices above",
            "instruction": "Generate migration-specific pre/post conditions grounded in kernel compatibility obligations and caller/callback context.",
        }

    prompt = build_pre_spec_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "pre_spec_generation",
        "function_specs": [],
        "module_spec": {
            "summary": "dry-run or no-provider placeholder",
            "required_module_level_properties": [],
            "required_changes": [],
            "optional_changes": [],
            "unsupported_assumptions": [],
            "evidence_used": [],
            "confidence": "low",
        },
    }

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=bool(dry_run or no_required_edit),
        default_output=default_output,
    )
