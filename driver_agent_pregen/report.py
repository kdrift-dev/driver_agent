from __future__ import annotations

from typing import Any, Optional

from .utils import write_json, write_text


def run_final_report_builder(
    *,
    out_dir,
    case_id: str,
    module_path: str,
    old_ref: str | None,
    new_ref: str | None,
    module_source: dict[str, Any],
    kernel_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    callback_graph: dict[str, Any],
    obligations: dict[str, Any],
    pre_specs: dict[str, Any],
    pre_reasoning: dict[str, Any],
    context_packet: dict[str, Any],
    patch_generation: Optional[dict[str, Any]] = None,
    validation: Optional[dict[str, Any]] = None,
    stage_timings: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    changed_defs = [i for i in kernel_diffs.get("items", []) if i.get("definition_changed")]
    affected = impact_graph.get("affected_functions", [])

    report_json = {
        "case_id": case_id,
        "module_path": module_path,
        "old_ref": old_ref,
        "new_ref": new_ref,
        "old_driver_module_source_summary": {
            "source_files_old": len(module_source.get("source_files_old", [])),
            "headers_old": len(module_source.get("headers_old", [])),
            "module_loc_old": module_source.get("module_loc_old", 0),
        },
        "kernel_side_changed_definitions": changed_defs,
        "affected_driver_functions": affected,
        "top_down_context": {
            "callback_roots": callback_graph.get("roots", []),
            "reachable": callback_graph.get("reachable", {}),
        },
        "compatibility_obligations": obligations.get("obligations", []),
        "semantic_obligations": obligations.get("semantic_obligations", []),
        "pre_patch_specs": pre_specs,
        "pre_patch_reasoner_verdict": pre_reasoning,
        "patch_guidance": context_packet.get("patch_generation_guidance", []),
        "patch_generation": patch_generation or {},
        "validation": validation or {},
        "stage_timings": stage_timings or [],
        "known_uncertainty": context_packet.get("open_questions", []),
        "recommended_next_step": "Use patch_generation candidate + validation outputs to iterate until compile/runtime checks pass.",
    }

    md = []
    md.append(f"# Pre-generation Report: {case_id}")
    md.append("")
    md.append(f"- module path: `{module_path}`")
    md.append(f"- old/new ref: `{old_ref}` -> `{new_ref}`")
    md.append(f"- old source files: {len(module_source.get('source_files_old', []))}")
    md.append(f"- old module LOC: {module_source.get('module_loc_old', 0)}")
    md.append("")

    md.append("## Kernel-side changed definitions")
    if changed_defs:
        for item in changed_defs[:120]:
            md.append(
                f"- {item.get('symbol')} ({item.get('kind')}): {item.get('change_type_guess')} "
                f"[confidence={item.get('confidence')}]"
            )
    else:
        md.append("- none detected")
    md.append("")

    md.append("## Affected driver functions")
    if affected:
        for fn in affected:
            syms = ", ".join(fn.get("uses_changed_kernel_symbols", []))
            md.append(f"- {fn.get('function')} ({fn.get('file')}): {syms}")
    else:
        md.append("- none detected")
    md.append("")

    md.append("## Compatibility obligations")
    for obl in obligations.get("obligations", []):
        md.append(f"- {obl.get('id')} {obl.get('affected_kernel_symbol')}: {obl.get('statement')}")
    if not obligations.get("obligations"):
        md.append("- none")
    md.append("")

    md.append("## Semantic obligations")
    sem_obls = obligations.get("semantic_obligations", [])
    if sem_obls:
        for so in sem_obls[:80]:
            md.append(
                f"- {so.get('id')} {so.get('affected_kernel_symbol')} "
                f"[{so.get('kind')}, strength={so.get('strength')}]: {so.get('statement')}"
            )
    else:
        md.append("- none")
    md.append("")

    md.append("## Pre-patch reasoner verdict")
    md.append(f"- overall_verdict: {pre_reasoning.get('overall_verdict', 'UNKNOWN')}")
    for fr in pre_reasoning.get("function_results", [])[:40]:
        md.append(f"- {fr.get('function')}: {fr.get('verdict')} | {fr.get('patch_guidance', '')}")
    md.append("")

    md.append("## Patch generation")
    if patch_generation:
        md.append(f"- parse_status: {patch_generation.get('parse_status', 'unknown')}")
        md.append(f"- candidates: {len(patch_generation.get('patch_candidates', []))}")
        md.append(f"- selected: {patch_generation.get('selected_candidate_id')}")
    else:
        md.append("- not executed")
    md.append("")

    md.append("## Validation")
    if validation:
        md.append(f"- status: {validation.get('status', 'unknown')}")
        comp = validation.get("compile", {})
        md.append(f"- compile_success: {comp.get('success', False)}")
        qemu = validation.get("qemu", {})
        md.append(f"- qemu_enabled: {qemu.get('enabled', False)}")
        md.append(f"- qemu_runs: {qemu.get('detected_qemu_runs', 0)}")
        post = validation.get("post_reasoning", {})
        md.append(f"- post_reasoning_verdict: {post.get('overall_verdict', 'UNKNOWN')}")
    else:
        md.append("- not executed")
    md.append("")

    md.append("## Stage Timings")
    if stage_timings:
        total = 0.0
        for st in stage_timings:
            dur = float(st.get("duration_sec", 0.0) or 0.0)
            total += dur
            md.append(f"- {st.get('name')}: {st.get('status')} ({dur:.3f}s)")
        md.append(f"- total(stage sum): {total:.3f}s")
    else:
        md.append("- unavailable")
    md.append("")

    md.append("## Known uncertainty")
    for q in context_packet.get("open_questions", []):
        md.append(f"- {q}")
    if not context_packet.get("open_questions"):
        md.append("- none")
    md.append("")

    md.append("## Recommended next step")
    md.append("- Iterate patch candidates based on compile/runtime failures until validation reaches status=ok.")

    write_json(reports_dir / "pre_generation_report.json", report_json)
    write_text(reports_dir / "pre_generation_report.md", "\n".join(md) + "\n")
    return report_json
