from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_pre_spec_prompt
from .utils import read_text, write_json, write_text


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
                "replacement_candidates": _compact_replacement_candidates(
                    item.get("replacement_candidates", []) or []
                ),
                "migration_signal": item.get("migration_signal", False),
                "used_by_driver": (item.get("used_by_driver", []) or [])[:1],
                "direct_include_requirements": item.get("direct_include_requirements", [])[:1],
                "open_questions": item.get("open_questions", [])[:1],
            }
            for field, max_chars in (
                ("old_definition", 50),
                ("new_definition", 50),
                ("definition_diff", 80),
            ):
                text = str(item.get(field, "") or "")
                slim[field] = text[:max_chars] + ("\n...[truncated]" if len(text) > max_chars else "")
            items.append(slim)
    out = {**kernel_definition_diffs, "items": items[:4]}
    if len(items) > 4:
        out["llm_payload_filter"] = f"truncated_to_4_required_symbols_from_{len(items)}; full evidence on disk"
    return out


def _compact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in evidence[:2]:
        value = ev.get("value")
        if isinstance(value, str):
            value = value[:100] + ("\n...[truncated]" if len(value) > 100 else "")
        elif isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if k in {"file", "required_include", "provider", "line", "symbol", "path"}
            }
        out.append({"type": ev.get("type"), "value": value})
    return out


def _compact_replacement_candidates(candidates: list[dict[str, Any]], max_items: int = 1) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in (candidates or [])[:max_items]:
        rec = {
            "symbol": cand.get("symbol"),
            "score": cand.get("score"),
            "evidence": [str(x)[:100] for x in (cand.get("evidence", []) or [])[:2]],
            "paths": [str(x) for x in (cand.get("paths", []) or [])[:2]],
        }
        loc = cand.get("new_location") or {}
        if loc:
            rec["new_location"] = {
                "path": loc.get("path"),
                "line": loc.get("line"),
                "match": str(loc.get("match", "") or "")[:100],
            }
        if cand.get("return_type_guess"):
            rec["return_type_guess"] = str(cand.get("return_type_guess"))[:80]
        out.append(rec)
    return out


def _compact_obligation(x: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": x.get("id"),
        "affected_kernel_symbol": x.get("affected_kernel_symbol"),
        "affected_driver_functions": (x.get("affected_driver_functions", []) or [])[:4],
        "statement": str(x.get("statement", "") or "")[:160],
        "kind": x.get("kind"),
        "strength": x.get("strength"),
        "recommended_action": str(x.get("recommended_action", "") or "")[:120],
        "hard_checks": (x.get("hard_checks", []) or [])[:2],
        "semantic_checks": (x.get("semantic_checks", []) or [])[:2],
        "confidence": x.get("confidence"),
        "semantic_strength": x.get("semantic_strength"),
        "migration_signal": x.get("migration_signal", False),
        "replacement_candidates": _compact_replacement_candidates(x.get("replacement_candidates", []) or []),
        "patch_guidance_seed": str(x.get("patch_guidance_seed", "") or "")[:120],
        "open_questions": (x.get("open_questions", []) or [])[:1],
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
    obligations = [
        _compact_obligation(x)
        for x in compatibility_obligations.get("obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
        and (x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong")
    ]
    out["obligations"] = obligations[:16]
    semantic_obligations = [
        _compact_obligation(x)
        for x in compatibility_obligations.get("semantic_obligations", []) or []
        if str(x.get("affected_kernel_symbol", "")) in keys
        and str(x.get("strength", "")).lower() == "strong"
    ]
    out["semantic_obligations"] = semantic_obligations[:8]
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
    if len(obligations) > 16 or len(semantic_obligations) > 8:
        out["llm_payload_filter"] += f"; truncated obligations {len(obligations)}->{len(out['obligations'])}, semantic {len(semantic_obligations)}->{len(out['semantic_obligations'])}"
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
            "ordinary_callers": item.get("ordinary_callers", [])[:2],
            "ordinary_callees": item.get("ordinary_callees", [])[:2],
            "callback_roots": item.get("callback_roots", [])[:1],
            "trigger_reasons": item.get("trigger_reasons", [])[:2],
        }

    return {
        "module_path": impact_graph.get("module_path"),
        "affected_tier_policy": impact_graph.get("affected_tier_policy", {}),
        "tier_counts": impact_graph.get("tier_counts", {}),
        "affected_functions": [],
        "edit_required_functions": [slim_fn(x) for x in (impact_graph.get("edit_required_functions", []) or []) if keep(x)][:4],
        "edit_candidate_functions": [slim_fn(x) for x in (impact_graph.get("edit_candidate_functions", []) or []) if keep(x)][:2],
        "context_only_functions": [slim_fn(x) for x in (impact_graph.get("context_only_functions", []) or []) if keep(x)][:1],
        "context_functions": [],
        "edges_summary": {
            "count": len(impact_graph.get("edges", []) or []),
            "sample": (impact_graph.get("edges", []) or [])[:2],
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


def _deterministic_pre_spec_fallback(
    *,
    case_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    """Compact non-LLM fallback so downstream patching is not blocked by JSON truncation."""
    impact = input_payload.get("impact_graph", {}) or {}
    compat = input_payload.get("compatibility_obligations", {}) or {}
    obligations = list(compat.get("obligations", []) or []) + list(compat.get("semantic_obligations", []) or [])

    def norm_symbol(value: Any) -> str:
        return str(value or "").split(" (", 1)[0].strip()

    def obligations_for(symbols: list[Any]) -> list[dict[str, Any]]:
        names = {norm_symbol(x) for x in symbols or []}
        return [x for x in obligations if norm_symbol(x.get("affected_kernel_symbol", "")) in names][:4]

    specs: list[dict[str, Any]] = []
    for scope_key in ("edit_required_functions", "edit_candidate_functions"):
        for fn in impact.get(scope_key, []) or []:
            obs = obligations_for(fn.get("uses_changed_kernel_symbols", []) or [])
            if not obs:
                obs = [
                    {
                        "affected_kernel_symbol": str(sym),
                        "statement": "Symbol is present in impact_graph but detailed obligation was omitted from compact LLM payload.",
                        "hard_checks": ["verify old usage against full extracted/compatibility_obligations.json"],
                        "patch_guidance_seed": "Use full extracted evidence for this impacted symbol.",
                    }
                    for sym in (fn.get("uses_changed_kernel_symbols", []) or [])[:3]
                ]
            req_changes: list[str] = []
            opt_changes: list[str] = []
            evidence: list[str] = []
            open_questions: list[str] = []
            for ob in obs:
                sym = str(ob.get("affected_kernel_symbol", ""))
                if ob.get("hard_checks"):
                    req_changes.append(f"Update usage of {sym} according to new kernel contract.")
                elif str(ob.get("strength", ob.get("semantic_strength", ""))).lower() == "strong":
                    req_changes.append(f"Evaluate required semantic migration for {sym}.")
                else:
                    opt_changes.append(f"Treat {sym} as optional/unknown unless stronger evidence appears.")
                if ob.get("patch_guidance_seed"):
                    evidence.append(str(ob.get("patch_guidance_seed"))[:180])
                if ob.get("open_questions"):
                    open_questions.extend(str(x)[:160] for x in (ob.get("open_questions", []) or [])[:1])
            specs.append(
                {
                    "function": fn.get("function"),
                    "file": fn.get("file"),
                    "precondition": "Function is reached through its recorded driver lifecycle/caller context.",
                    "postcondition": "Preserve existing return, ownership, cleanup, and locking behavior while satisfying required kernel obligations.",
                    "compatibility_obligation": "; ".join(
                        str(x.get("affected_kernel_symbol", "")) for x in obs
                    )[:240],
                    "caller_lifecycle_expectation": "Use impact_graph callback/caller context as non-editable context.",
                    "relevant_kernel_contract": "; ".join(
                        str(x.get("statement", ""))[:140] for x in obs
                    )[:360],
                    "evidence": evidence[:5],
                    "open_questions": open_questions[:5],
                    "required_changes": req_changes[:5],
                    "optional_changes": opt_changes[:5],
                    "unsupported_assumptions": [
                        "Generated by deterministic fallback because LLM output may be unavailable or unparsable."
                    ],
                    "evidence_used": [
                        "compatibility_obligations",
                        "impact_graph.edit_required_functions/edit_candidate_functions",
                    ],
                    "confidence": "medium" if scope_key == "edit_required_functions" else "low",
                }
            )
            if len(specs) >= 8:
                break
        if len(specs) >= 8:
            break

    return {
        "case_id": case_id,
        "stage": "pre_spec_generation",
        "function_specs": specs,
        "module_spec": {
            "summary": f"deterministic fallback spec for {len(specs)} migration-relevant functions",
            "required_module_level_properties": [
                "All hard compatibility obligations must be resolved in editable functions.",
                "Context-only functions are reasoning context and not default edit targets.",
            ],
            "required_changes": [
                change
                for spec in specs
                for change in (spec.get("required_changes", []) or [])
            ][:8],
            "optional_changes": [
                change
                for spec in specs
                for change in (spec.get("optional_changes", []) or [])
            ][:8],
            "unsupported_assumptions": [
                "Fallback spec is conservative and should not be treated as proof of correctness."
            ],
            "evidence_used": ["compatibility_obligations", "impact_graph"],
            "confidence": "medium" if specs else "low",
        },
    }


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
                max_chars_per_slice=800,
                max_items=2,
                symbol_keys=required_keys,
            ),
            "edit_candidate_function_slices": _load_slices(
                extracted_dir,
                impact_graph,
                list_key="edit_candidate_functions",
                max_chars_per_slice=700,
                max_items=1,
                symbol_keys=required_keys,
            ),
            "context_only_function_slices": _load_slices(
                extracted_dir,
                impact_graph,
                list_key="context_only_functions",
                max_chars_per_slice=600,
                max_items=1,
                symbol_keys=required_keys,
            ),
            "affected_function_slices": [],
            "context_function_slices": [],
            "slice_payload_filter": "deduplicated; use edit_required/edit_candidate/context_only slices above",
            "instruction": "Generate migration-specific pre/post conditions grounded in kernel compatibility obligations and caller/callback context.",
        }

    prompt = build_pre_spec_prompt(input_payload)
    default_output = _deterministic_pre_spec_fallback(case_id=case_id, input_payload=input_payload)

    if (not dry_run) and (not no_required_edit) and len(prompt) > 35000:
        write_text(stage_dir / "prompt.md", prompt)
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "raw_output.txt", "")
        default_output["parse_status"] = "deterministic_fallback_large_payload"
        default_output["fallback_reason"] = f"pre_spec_prompt_chars={len(prompt)} exceeds 35000"
        write_json(stage_dir / "parsed_output.json", default_output)
        return default_output

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=bool(dry_run or no_required_edit),
        default_output=default_output,
        max_tokens=6000,
    )
