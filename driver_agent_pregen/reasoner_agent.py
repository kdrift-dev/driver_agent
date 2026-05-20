from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_post_reasoner_prompt, build_pre_reasoner_prompt
from .utils import read_text, write_json


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

    # Strong semantic obligations should not pass as MATCH without required changes.
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
        reps_low = [r.lower() for r in reps if r]
        for fn in sobl.get("affected_driver_functions", []) or []:
            fn_str = str(fn).strip()
            fr = by_fn.get(fn_str, {})
            req_text = " ".join(_as_list(out.get("required_changes")) + _as_list(fr.get("required_changes"))).lower()
            has_required_replacement = any(r in req_text for r in reps_low)
            if not has_required_replacement:
                if reps:
                    unresolved.append(f"{fn_str}: strong semantic obligation lacks required migration signal ({', '.join(reps[:3])})")
                else:
                    unresolved.append(f"{fn_str}: strong semantic obligation lacks required migration signal")

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

    strong_sem = _strong_semantic_obligations(compatibility_obligations)
    if not strong_sem:
        out["overall_verdict"] = verdict if verdict in {"PASS", "FAIL", "UNKNOWN"} else "UNKNOWN"
        return out

    slice_map = {str(x.get("function", "")).strip(): str(x.get("slice", "")) for x in (patched_function_slices or [])}
    unresolved: list[str] = []
    for sobl in strong_sem:
        reps = [str(c.get("symbol", "")).strip() for c in sobl.get("replacement_candidates", []) if c.get("symbol")]
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
    max_chars: int = 6000,
) -> list[dict[str, Any]]:
    out = []
    for afn in impact_graph.get(list_key, []):
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

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "pre_patch_specs": pre_specs,
        "compatibility_obligations": compatibility_obligations,
        "kernel_definition_diffs": kernel_definition_diffs,
        "impact_graph": impact_graph,
        "callback_graph": callback_graph,
        "affected_function_slices": _collect_slices(extracted_dir, impact_graph, list_key="affected_functions"),
        "context_function_slices": _collect_slices(extracted_dir, impact_graph, list_key="context_functions"),
        "instruction": "Judge old driver compatibility against new kernel obligations and provide textual patch guidance.",
    }

    prompt = build_pre_reasoner_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "pre_code_reasoning",
        "overall_verdict": "UNKNOWN",
        "function_results": [],
        "module_level_findings": ["dry-run or no-provider placeholder"],
        "hard_check_requirements": [],
        "semantic_check_requirements": [],
        "required_changes": [],
        "optional_changes": [],
        "unsupported_assumptions": [],
        "evidence_used": [],
        "confidence": "low",
    }

    out = llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
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

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "compatibility_obligations": compatibility_obligations,
        "impact_graph": impact_graph,
        "patched_function_slices": patched_function_slices,
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

    out = llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
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
