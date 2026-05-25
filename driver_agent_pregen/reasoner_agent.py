from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_post_reasoner_prompt, build_pre_reasoner_prompt
from .utils import read_text, write_json, write_text


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            if x is None:
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    s = str(value).strip()
    return [s] if s else []


def _append_unique(target: list[str], item: str) -> None:
    if item and item not in target:
        target.append(item)


def _normalize_common_fields(payload: dict[str, Any]) -> dict[str, Any]:
    payload["module_level_findings"] = _as_list(payload.get("module_level_findings"))
    payload["required_changes"] = _as_list(payload.get("required_changes"))
    payload["optional_changes"] = _as_list(payload.get("optional_changes"))
    payload["unsupported_assumptions"] = _as_list(payload.get("unsupported_assumptions"))
    payload["evidence_used"] = _as_list(payload.get("evidence_used"))
    payload["function_results"] = list(payload.get("function_results") or [])
    for fr in payload["function_results"]:
        if not isinstance(fr, dict):
            continue
        fr["required_changes"] = _as_list(fr.get("required_changes"))
        fr["optional_changes"] = _as_list(fr.get("optional_changes"))
        fr["unsupported_assumptions"] = _as_list(fr.get("unsupported_assumptions"))
        fr["evidence_used"] = _as_list(fr.get("evidence_used"))
    return payload


def _strong_semantic_obligations(compatibility_obligations: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in compatibility_obligations.get("semantic_obligations", []):
        if str(item.get("strength", "")).lower() == "strong":
            out.append(item)
    return out


def _has_any_evidence(
    *,
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
) -> bool:
    affected = impact_graph.get("affected_functions", []) or []
    obligations = compatibility_obligations.get("obligations", []) or []
    sem_obligations = compatibility_obligations.get("semantic_obligations", []) or []
    return bool(affected or obligations or sem_obligations)


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


def _deterministic_pre_reasoning_fallback(
    *,
    case_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    """Conservative fallback used when LLM reasoning output is truncated/unparseable."""
    impact = input_payload.get("impact_graph", {}) or {}
    compat = input_payload.get("compatibility_obligations", {}) or {}
    obligations = list(compat.get("obligations", []) or []) + list(compat.get("semantic_obligations", []) or [])

    def norm_symbol(value: Any) -> str:
        return str(value or "").split(" (", 1)[0].strip()

    def obligations_for(symbols: list[Any]) -> list[dict[str, Any]]:
        names = {norm_symbol(x) for x in symbols or []}
        return [x for x in obligations if norm_symbol(x.get("affected_kernel_symbol", "")) in names][:4]

    function_results: list[dict[str, Any]] = []
    required_changes: list[str] = []
    evidence_used: list[str] = []
    for scope_key in ("edit_required_functions", "edit_candidate_functions"):
        for fn in impact.get(scope_key, []) or []:
            obs = obligations_for(fn.get("uses_changed_kernel_symbols", []) or [])
            if not obs:
                obs = [
                    {
                        "affected_kernel_symbol": str(sym),
                        "hard_checks": ["verify old usage against full extracted/compatibility_obligations.json"],
                        "patch_guidance_seed": "Use full extracted evidence for this impacted symbol.",
                    }
                    for sym in (fn.get("uses_changed_kernel_symbols", []) or [])[:3]
                ]
            fn_changes: list[str] = []
            fn_optional: list[str] = []
            for ob in obs:
                sym = str(ob.get("affected_kernel_symbol", ""))
                reps = [str(c.get("symbol", "")) for c in (ob.get("replacement_candidates", []) or []) if c.get("symbol")]
                if ob.get("hard_checks"):
                    msg = f"{fn.get('function')}: resolve hard obligation for {sym}"
                    if reps:
                        msg += f" using candidate {reps[0]}"
                    fn_changes.append(msg)
                elif str(ob.get("strength", ob.get("semantic_strength", ""))).lower() == "strong":
                    msg = f"{fn.get('function')}: resolve strong semantic obligation for {sym}"
                    if reps:
                        msg += f" using candidate {reps[0]}"
                    fn_changes.append(msg)
                else:
                    fn_optional.append(f"{fn.get('function')}: optional/unknown change for {sym}")
                evidence_used.append(f"{sym}: {ob.get('id', 'obligation')}")
            required_changes.extend(fn_changes)
            function_results.append(
                {
                    "function": fn.get("function"),
                    "verdict": "MISMATCH" if fn_changes else "UNKNOWN",
                    "spec_claim": "Old code must satisfy directly evidenced kernel compatibility obligations.",
                    "actual_behavior": "Old function still uses symbols listed in impact_graph.",
                    "code_evidence": "; ".join(str(x) for x in (fn.get("uses_changed_kernel_symbols", []) or [])[:4]),
                    "trigger_condition": "Build or migration mismatch when old usage is incompatible with new kernel definition.",
                    "reasoning": "Deterministic fallback: required only for hard or strong semantic obligations present in input.",
                    "patch_guidance": "; ".join(fn_changes[:4]) or "Do not edit unless stronger evidence appears.",
                    "required_changes": fn_changes[:5],
                    "optional_changes": fn_optional[:5],
                    "unsupported_assumptions": [
                        "Fallback reasoning is conservative and does not prove full behavior."
                    ],
                    "evidence_used": evidence_used[-4:],
                    "confidence": "medium" if fn_changes else "low",
                }
            )
            if len(function_results) >= 8:
                break
        if len(function_results) >= 8:
            break

    return {
        "case_id": case_id,
        "stage": "pre_code_reasoning",
        "overall_verdict": "MISMATCH" if required_changes else "UNKNOWN",
        "function_results": function_results,
        "module_level_findings": [
            "deterministic fallback reasoning used when LLM output is unavailable or unparsable"
        ],
        "hard_check_requirements": required_changes[:8],
        "semantic_check_requirements": [],
        "required_changes": required_changes[:12],
        "optional_changes": [],
        "unsupported_assumptions": [
            "Spec/reasoning consistency is not a correctness proof."
        ],
        "evidence_used": evidence_used[:12],
        "confidence": "medium" if required_changes else "low",
    }


def _enforce_pre_reasoning_policy(
    *,
    output: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
) -> dict[str, Any]:
    out = _normalize_common_fields(dict(output))
    verdict = str(out.get("overall_verdict", "UNKNOWN")).upper()

    # Fail-closed on empty evidence: do not allow vacuous MATCH.
    if not _has_any_evidence(compatibility_obligations=compatibility_obligations, impact_graph=impact_graph):
        out["overall_verdict"] = "UNKNOWN"
        _append_unique(out["module_level_findings"], "insufficient_evidence: empty affected/obligation set")
        _append_unique(out["unsupported_assumptions"], "cannot assert compatibility without affected functions or obligations")
        out["confidence"] = "low"
        return out

    # Strong semantic obligations that carry an explicit replacement candidate
    # should not pass as MATCH without that required migration signal. Other
    # strong obligations, such as annotation-only locking revalidation, may be
    # satisfied by existing code and must not force an edit by themselves.
    strong_sem = _strong_semantic_obligations(compatibility_obligations)
    if not strong_sem:
        out["overall_verdict"] = verdict if verdict in {"MATCH", "MISMATCH", "UNKNOWN"} else "UNKNOWN"
        return out

    by_fn: dict[str, dict[str, Any]] = {}
    for fr in out.get("function_results", []):
        if not isinstance(fr, dict):
            continue
        fn = str(fr.get("function", "")).strip()
        if fn:
            by_fn[fn] = fr

    unresolved: list[str] = []
    for sobl in strong_sem:
        reps = [str(c.get("symbol", "")).strip() for c in sobl.get("replacement_candidates", []) if c.get("symbol")]
        if not reps:
            continue
        reps_low = [r.lower() for r in reps if r]
        for fn in sobl.get("affected_driver_functions", []) or []:
            fn_str = str(fn).strip()
            fr = by_fn.get(fn_str, {})
            req_text = " ".join(_as_list(out.get("required_changes")) + _as_list(fr.get("required_changes"))).lower()
            has_required_replacement = any(r in req_text for r in reps_low)
            if not has_required_replacement:
                unresolved.append(f"{fn_str}: strong semantic obligation lacks required migration signal ({', '.join(reps[:3])})")

    if unresolved and verdict == "MATCH":
        out["overall_verdict"] = "MISMATCH"
        for msg in unresolved:
            _append_unique(out["required_changes"], msg)
        _append_unique(
            out["module_level_findings"],
            "strong semantic obligations present; MATCH downgraded to MISMATCH until required migration changes are explicit",
        )
    else:
        out["overall_verdict"] = verdict if verdict in {"MATCH", "MISMATCH", "UNKNOWN"} else "UNKNOWN"
    return out


def _enforce_post_reasoning_policy(
    *,
    output: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    patched_function_slices: list[dict[str, Any]],
) -> dict[str, Any]:
    out = _normalize_common_fields(dict(output))
    verdict = str(out.get("overall_verdict", "UNKNOWN")).upper()

    if not _has_any_evidence(compatibility_obligations=compatibility_obligations, impact_graph=impact_graph):
        out["overall_verdict"] = "UNKNOWN"
        _append_unique(out["module_level_findings"], "insufficient_evidence: empty affected/obligation set")
        _append_unique(out["unsupported_assumptions"], "cannot claim post-patch PASS without obligation evidence")
        out["confidence"] = "low"
        return out

    include_block_map: dict[str, str] = {}
    for item in patched_function_slices or []:
        if item.get("source_item_kind") == "patched_file_include_block":
            include_block_map[str(item.get("file", "")).strip()] = str(item.get("slice", "") or "")

    missing_direct_includes: list[str] = []
    satisfied_direct_includes: list[tuple[str, str]] = []
    for obl in compatibility_obligations.get("obligations", []) or []:
        for ev in obl.get("evidence", []) or []:
            if ev.get("type") != "direct_include_requirement":
                continue
            value = ev.get("value") or {}
            rel = str(value.get("file") or "").strip()
            required = str(value.get("required_include") or "").strip()
            if not rel or not required:
                continue
            include_block = include_block_map.get(rel, "")
            if required in include_block:
                satisfied_direct_includes.append((rel, required))
            else:
                missing_direct_includes.append(f"{rel}: missing direct include {required}")

    if missing_direct_includes and verdict == "PASS":
        out["overall_verdict"] = "UNKNOWN"
        for msg in missing_direct_includes:
            _append_unique(out["required_changes"], msg)
        _append_unique(
            out["module_level_findings"],
            "direct include obligations unresolved in patched include blocks; PASS downgraded to UNKNOWN",
        )
        out["confidence"] = "low"
        return out

    if satisfied_direct_includes:
        required_headers = {header for _, header in satisfied_direct_includes}
        required_files = {path for path, _ in satisfied_direct_includes}

        def _is_stale_include_note(text: Any) -> bool:
            low = str(text or "").lower()
            return (
                any(h.lower() in low for h in required_headers)
                and any(Path(f).name.lower() in low or f.lower() in low for f in required_files)
                and ("add" in low or "include" in low or "assumed" in low)
            )

        out["optional_changes"] = [x for x in out.get("optional_changes", []) if not _is_stale_include_note(x)]
        out["unsupported_assumptions"] = [x for x in out.get("unsupported_assumptions", []) if not _is_stale_include_note(x)]

    strong_sem = _strong_semantic_obligations(compatibility_obligations)
    if not strong_sem:
        out["overall_verdict"] = verdict if verdict in {"PASS", "FAIL", "UNKNOWN"} else "UNKNOWN"
        return out

    slice_map = {str(x.get("function", "")).strip(): str(x.get("slice", "")) for x in (patched_function_slices or [])}
    unresolved: list[str] = []
    for sobl in strong_sem:
        reps = [str(c.get("symbol", "")).strip() for c in sobl.get("replacement_candidates", []) if c.get("symbol")]
        if not reps:
            continue
        for fn in sobl.get("affected_driver_functions", []) or []:
            fn_str = str(fn).strip()
            sl = slice_map.get(fn_str, "")
            if not sl:
                unresolved.append(f"{fn_str}: no patched slice available for strong semantic obligation check")
                continue
            if reps:
                has_rep = any(f"{r}(" in sl for r in reps)
                if not has_rep:
                    unresolved.append(f"{fn_str}: no replacement call observed in patched slice ({', '.join(reps[:3])})")

    if unresolved and verdict == "PASS":
        out["overall_verdict"] = "UNKNOWN"
        for msg in unresolved:
            _append_unique(out["required_changes"], msg)
        _append_unique(
            out["module_level_findings"],
            "strong semantic obligations unresolved in patched slices; PASS downgraded to UNKNOWN",
        )
    else:
        out["overall_verdict"] = verdict if verdict in {"PASS", "FAIL", "UNKNOWN"} else "UNKNOWN"
    return out


def _collect_slices(
    extracted_dir: Path,
    impact_graph: dict[str, Any],
    *,
    list_key: str = "affected_functions",
    max_chars: int = 3500,
    max_items: int | None = None,
    symbol_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for afn in impact_graph.get(list_key, []):
        if max_items is not None and len(out) >= max_items:
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
        if len(txt) > max_chars:
            txt = txt[:max_chars] + "\n...[truncated]"
        out.append({"function": afn.get("function"), "file": afn.get("file"), "slice": txt})
    return out


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
        if key not in keys:
            continue
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


def _compact_pre_specs(pre_specs: dict[str, Any]) -> dict[str, Any]:
    specs: list[dict[str, Any]] = []
    for item in pre_specs.get("function_specs", []) or []:
        specs.append(
            {
                "function": item.get("function"),
                "file": item.get("file"),
                "precondition": str(item.get("precondition", "") or "")[:700],
                "postcondition": str(item.get("postcondition", "") or "")[:900],
                "compatibility_obligation": str(item.get("compatibility_obligation", "") or "")[:700],
                "caller_lifecycle_expectation": str(item.get("caller_lifecycle_expectation", "") or "")[:500],
                "relevant_kernel_contract": str(item.get("relevant_kernel_contract", "") or "")[:700],
                "open_questions": (item.get("open_questions", []) or [])[:4],
            }
        )
    return {
        "case_id": pre_specs.get("case_id"),
        "stage": pre_specs.get("stage"),
        "function_specs": specs,
        "module_spec": pre_specs.get("module_spec", {}),
        "llm_payload_filter": "compact specs; full pre-spec output remains in llm_calls/01_pre_spec/parsed_output.json",
    }


def _compact_callback_graph(callback_graph: dict[str, Any]) -> dict[str, Any]:
    roots = callback_graph.get("callback_roots", []) or callback_graph.get("roots", []) or []
    edges = callback_graph.get("edges", []) or []
    return {
        "root_count": len(roots),
        "edge_count": len(edges),
        "callback_roots": roots[:20],
        "edges_sample": edges[:40],
        "llm_payload_filter": "compact callback graph; full callback graph remains in extracted/callback_graph.json",
    }


def _compact_patched_slices(patched_function_slices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in patched_function_slices or []:
        if len(out) >= 30:
            break
        sl = str(item.get("slice", "") or "")
        max_chars = 1800 if item.get("source_item_kind") == "patched_file_include_block" else 3000
        out.append(
            {
                "function": item.get("function"),
                "file": item.get("file"),
                "source_item_kind": item.get("source_item_kind"),
                "slice": sl[:max_chars] + ("\n...[truncated]" if len(sl) > max_chars else ""),
            }
        )
    return out


def run_pre_reasoner_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    pre_specs: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    kernel_definition_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    callback_graph: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "02_pre_reasoner"
    stage_dir.mkdir(parents=True, exist_ok=True)

    no_required_edit = not _requires_effective_migration_edit(compatibility_obligations)
    if no_required_edit:
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "pre_patch_specs": {"function_specs": [], "llm_payload_filter": "omitted because no required migration edit exists"},
            "compatibility_obligations": {
                "obligation_count": len(compatibility_obligations.get("obligations", []) or []),
                "semantic_obligation_count": len(compatibility_obligations.get("semantic_obligations", []) or []),
                "required_obligation_count": 0,
                "llm_payload_filter": "full obligations remain in extracted/compatibility_obligations.json",
            },
            "kernel_definition_diffs": {"items": [], "llm_payload_filter": "omitted because no required migration edit exists"},
            "impact_graph": {
                "tier_counts": impact_graph.get("tier_counts", {}),
                "llm_payload_filter": "omitted because no required migration edit exists",
            },
            "callback_graph": {"llm_payload_filter": "omitted because no required migration edit exists"},
            "edit_required_function_slices": [],
            "edit_candidate_function_slices": [],
            "context_only_function_slices": [],
            "affected_function_slices": [],
            "context_function_slices": [],
            "slice_payload_filter": "omitted because no required migration edit exists",
            "instruction": "No hard or strong semantic migration edit exists; skip pre-reasoning.",
        }
    else:
        required_keys = _required_symbol_keys(compatibility_obligations)
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "pre_patch_specs": _compact_pre_specs(pre_specs),
            "compatibility_obligations": _slim_obligations_for_impact(
                compatibility_obligations, impact_graph, symbol_keys=required_keys
            ),
            "kernel_definition_diffs": _slim_kernel_diffs_for_impact(
                kernel_definition_diffs, impact_graph, symbol_keys=required_keys
            ),
            "impact_graph": _slim_impact_graph_for_llm(impact_graph, symbol_keys=required_keys),
            "callback_graph": _compact_callback_graph(callback_graph),
            "edit_required_function_slices": _collect_slices(
                extracted_dir,
                impact_graph,
                list_key="edit_required_functions",
                max_chars=800,
                max_items=2,
                symbol_keys=required_keys,
            ),
            "edit_candidate_function_slices": _collect_slices(
                extracted_dir,
                impact_graph,
                list_key="edit_candidate_functions",
                max_chars=700,
                max_items=1,
                symbol_keys=required_keys,
            ),
            "context_only_function_slices": _collect_slices(
                extracted_dir,
                impact_graph,
                list_key="context_only_functions",
                max_chars=600,
                max_items=1,
                symbol_keys=required_keys,
            ),
            "affected_function_slices": [],
            "context_function_slices": [],
            "slice_payload_filter": "deduplicated; use edit_required/edit_candidate/context_only slices above",
            "instruction": "Judge old driver compatibility against new kernel obligations and provide textual patch guidance.",
        }

    prompt = build_pre_reasoner_prompt(input_payload)
    default_output = _deterministic_pre_reasoning_fallback(case_id=case_id, input_payload=input_payload)

    if (not dry_run) and (not no_required_edit) and len(prompt) > 45000:
        write_text(stage_dir / "prompt.md", prompt)
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "raw_output.txt", "")
        default_output["parse_status"] = "deterministic_fallback_large_payload"
        default_output["fallback_reason"] = f"pre_reasoner_prompt_chars={len(prompt)} exceeds 45000"
        out = _enforce_pre_reasoning_policy(
            output=default_output,
            compatibility_obligations=compatibility_obligations,
            impact_graph=impact_graph,
        )
        write_json(stage_dir / "parsed_output.json", out)
        return out

    out = llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=bool(dry_run or no_required_edit),
        default_output=default_output,
        max_tokens=6000,
    )
    out = _enforce_pre_reasoning_policy(
        output=out,
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
    )
    out.setdefault("parse_status", "ok")
    write_json(stage_dir / "parsed_output.json", out)
    return out


def run_post_reasoner_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    case_id: str,
    module_path: str,
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    patched_function_slices: list[dict[str, Any]],
    compile_result: dict[str, Any],
    test_generation_result: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "04_post_reasoner"
    stage_dir.mkdir(parents=True, exist_ok=True)

    no_required_edit = not _requires_effective_migration_edit(compatibility_obligations)
    if no_required_edit:
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
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
            "patched_function_slices": [],
            "compile_result": {
                "success": compile_result.get("success"),
                "compiled_dirs": compile_result.get("compiled_dirs", []),
                "error": compile_result.get("error", ""),
            },
            "generated_system_tests": {
                "parse_status": test_generation_result.get("parse_status"),
                "test_case_count": len(test_generation_result.get("test_cases", []) or []),
            },
            "instruction": "No hard or strong semantic migration edit exists; skip post-reasoning LLM.",
        }
    else:
        required_keys = _required_symbol_keys(compatibility_obligations)
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "compatibility_obligations": _slim_obligations_for_impact(
                compatibility_obligations, impact_graph, symbol_keys=required_keys
            ),
            "impact_graph": _slim_impact_graph_for_llm(impact_graph, symbol_keys=required_keys),
            "patched_function_slices": _compact_patched_slices(patched_function_slices),
            "compile_result": compile_result,
            "generated_system_tests": test_generation_result,
            "instruction": "Evaluate whether the patched module satisfies obligations and list residual risks.",
        }

    prompt = build_post_reasoner_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "post_patch_reasoning",
        "overall_verdict": "UNKNOWN",
        "function_results": [],
        "module_level_findings": ["dry-run or no-provider placeholder"],
        "remaining_open_questions": [],
        "required_changes": [],
        "optional_changes": [],
        "unsupported_assumptions": [],
        "evidence_used": [],
        "confidence": "low",
    }

    if not bool(compile_result.get("success")):
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "prompt.md", prompt)
        write_text(stage_dir / "raw_output.txt", "")
        out = {
            "case_id": case_id,
            "stage": "post_patch_reasoning",
            "overall_verdict": "FAIL",
            "function_results": [],
            "module_level_findings": [
                "deterministic_spec_consistency_short_circuit: compile failed before post reasoning"
            ],
            "remaining_open_questions": ["compile_not_success"],
            "required_changes": [],
            "optional_changes": [],
            "unsupported_assumptions": [
                "Cannot claim obligation satisfaction when target module does not compile."
            ],
            "evidence_used": ["compile_result.success=false"],
            "confidence": "high",
            "parse_status": "deterministic_compile_failed",
            "skipped_llm_reason": "compile_failed",
        }
        write_json(stage_dir / "parsed_output.json", out)
        return out

    if (not dry_run) and (not no_required_edit) and len(prompt) > 70000:
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "prompt.md", prompt)
        write_text(stage_dir / "raw_output.txt", "")
        out = {
            "case_id": case_id,
            "stage": "post_patch_reasoning",
            "overall_verdict": "UNKNOWN",
            "function_results": [],
            "module_level_findings": [
                "deterministic_spec_consistency_short_circuit: post-reasoning payload too large for reliable LLM JSON output"
            ],
            "remaining_open_questions": ["spec_consistency_not_run_large_payload"],
            "required_changes": [],
            "optional_changes": [],
            "unsupported_assumptions": [
                "Spec consistency was skipped; compile and deterministic validation remain the available evidence."
            ],
            "evidence_used": ["compile_result.success=true"],
            "confidence": "low",
            "parse_status": "ok",
            "skipped_llm_reason": f"post_reasoner_prompt_chars={len(prompt)} exceeds 70000",
        }
        write_json(stage_dir / "parsed_output.json", out)
        return out

    if no_required_edit:
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "prompt.md", prompt)
        write_text(stage_dir / "raw_output.txt", "")
        compile_ok = bool(compile_result.get("success"))
        out = {
            "case_id": case_id,
            "stage": "post_patch_reasoning",
            "overall_verdict": "PASS" if compile_ok else "FAIL",
            "function_results": [],
            "module_level_findings": [
                "deterministic_spec_consistency_short_circuit: no hard or strong semantic migration edit is required"
            ],
            "remaining_open_questions": [],
            "required_changes": [],
            "optional_changes": [],
            "unsupported_assumptions": [],
            "evidence_used": ["compile_result.success", "no hard or strong semantic compatibility obligation"],
            "confidence": "medium" if compile_ok else "low",
            "parse_status": "ok",
            "skipped_llm_reason": "no_required_migration_edit",
        }
    else:
        out = llm_client.call_json(
            stage_dir=stage_dir,
            prompt_text=prompt,
            input_payload=input_payload,
            dry_run=dry_run,
            default_output=default_output,
            max_tokens=4000,
        )
    out = _enforce_post_reasoning_policy(
        output=out,
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
        patched_function_slices=patched_function_slices,
    )
    out.setdefault("parse_status", "ok")
    write_json(stage_dir / "parsed_output.json", out)
    return out
