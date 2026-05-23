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
    max_chars_per_slice: int = 6000,
) -> list[dict[str, Any]]:
    slices = []
    for afn in impact_graph.get(list_key, []):
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


def _slim_kernel_diffs_for_impact(kernel_definition_diffs: dict[str, Any], impact_graph: dict[str, Any]) -> dict[str, Any]:
    keys = _impact_symbol_keys(impact_graph)
    if not keys:
        return {**kernel_definition_diffs, "items": []}
    items = []
    for item in kernel_definition_diffs.get("items", []) or []:
        key = f"{item.get('symbol')} ({item.get('kind')})"
        if key in keys:
            slim = dict(item)
            # Keep full evidence on disk; LLM only needs compact excerpts.
            for field in ("old_definition", "new_definition", "definition_diff"):
                text = str(slim.get(field, "") or "")
                if len(text) > 5000:
                    slim[field] = text[:5000] + "\n...[truncated]"
            items.append(slim)
    return {**kernel_definition_diffs, "items": items}


def _slim_obligations_for_impact(compatibility_obligations: dict[str, Any], impact_graph: dict[str, Any]) -> dict[str, Any]:
    keys = _impact_symbol_keys(impact_graph)
    if not keys:
        return {**compatibility_obligations, "obligations": [], "semantic_obligations": []}
    out = dict(compatibility_obligations)
    out["obligations"] = [
        x
        for x in compatibility_obligations.get("obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
    ]
    out["semantic_obligations"] = [
        x
        for x in compatibility_obligations.get("semantic_obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
    ]
    out["llm_payload_filter"] = "impact_symbols_only; full obligations remain in extracted/compatibility_obligations.json"
    return out


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

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "kernel_definition_diffs": _slim_kernel_diffs_for_impact(kernel_definition_diffs, impact_graph),
        "compatibility_obligations": _slim_obligations_for_impact(compatibility_obligations, impact_graph),
        "impact_graph": impact_graph,
        "edit_required_function_slices": _load_slices(extracted_dir, impact_graph, list_key="edit_required_functions"),
        "edit_candidate_function_slices": _load_slices(extracted_dir, impact_graph, list_key="edit_candidate_functions"),
        "context_only_function_slices": _load_slices(extracted_dir, impact_graph, list_key="context_only_functions"),
        "affected_function_slices": _load_slices(extracted_dir, impact_graph, list_key="affected_functions"),
        "context_function_slices": _load_slices(extracted_dir, impact_graph, list_key="context_functions"),
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
        dry_run=dry_run,
        default_output=default_output,
    )
