from __future__ import annotations

from typing import Any

from .utils import write_json, write_text


def _build_patch_guidance(pre_reasoning: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in pre_reasoning.get("function_results", []):
        fn = item.get("function")
        guide = item.get("patch_guidance")
        if not fn:
            continue
        out.append(
            {
                "function": fn,
                "guidance": guide or "",
                "evidence": [
                    item.get("code_evidence", ""),
                    item.get("reasoning", ""),
                ],
            }
        )
    return out


def _kernel_change_summary(kernel_diffs: dict[str, Any]) -> str:
    changed = 0
    removed = 0
    type_counts = {}
    for item in kernel_diffs.get("items", []):
        if item.get("definition_changed"):
            changed += 1
            t = item.get("change_type_guess", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        if any("missing in new_ref" in q for q in item.get("open_questions", [])):
            removed += 1
    parts = [f"changed_symbols={changed}", f"missing_in_new={removed}"]
    if type_counts:
        parts.append("type_counts=" + ", ".join(f"{k}:{v}" for k, v in sorted(type_counts.items())))
    return "; ".join(parts)


def run_context_packet_builder(
    *,
    out_dir,
    case_id: str,
    module_path: str,
    old_ref: str | None,
    new_ref: str | None,
    kernel_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    obligations: dict[str, Any],
    pre_specs: dict[str, Any],
    pre_reasoning: dict[str, Any],
) -> dict[str, Any]:
    hard_plan = []
    sem_plan = []
    for obl in obligations.get("obligations", []):
        for hc in obl.get("hard_checks", []):
            hard_plan.append(f"{obl.get('id')}: {hc}")
        for sc in obl.get("semantic_checks", []):
            sem_plan.append(f"{obl.get('id')}: {sc}")

    packet = {
        "case_id": case_id,
        "module_path": module_path,
        "old_ref": old_ref,
        "new_ref": new_ref,
        "kernel_change_summary": _kernel_change_summary(kernel_diffs),
        "affected_functions": impact_graph.get("affected_functions", []),
        "context_functions": impact_graph.get("context_functions", []),
        "editable_functions": impact_graph.get("editable_functions", []),
        "affected_tier_policy": impact_graph.get("affected_tier_policy", {}),
        "compatibility_obligations": obligations.get("obligations", []),
        "pre_specs": pre_specs,
        "pre_reasoning": pre_reasoning,
        "patch_generation_guidance": _build_patch_guidance(pre_reasoning),
        "hard_validation_plan": hard_plan,
        "semantic_validation_plan": sem_plan,
        "open_questions": collect_open_questions(kernel_diffs, obligations, pre_specs, pre_reasoning),
    }

    write_json(out_dir / "context_packet.json", packet)

    md = [
        f"# Context Packet: {case_id}",
        "",
        f"- module_path: `{module_path}`",
        f"- old_ref: `{old_ref}`",
        f"- new_ref: `{new_ref}`",
        f"- kernel_change_summary: {packet['kernel_change_summary']}",
        f"- affected_functions: {len(packet['affected_functions'])}",
        f"- context_functions: {len(packet['context_functions'])}",
        f"- editable_functions: {len(packet['editable_functions'])}",
        f"- obligations: {len(packet['compatibility_obligations'])}",
        f"- overall_verdict: {pre_reasoning.get('overall_verdict', 'UNKNOWN')}",
        "",
        "## Patch Guidance",
    ]
    for g in packet["patch_generation_guidance"]:
        md.append(f"- {g['function']}: {g['guidance']}")
    if not packet["patch_generation_guidance"]:
        md.append("- (none)")

    md.append("")
    md.append("## Open Questions")
    for q in packet["open_questions"]:
        md.append(f"- {q}")
    if not packet["open_questions"]:
        md.append("- (none)")

    write_text(out_dir / "context_packet.md", "\n".join(md) + "\n")
    return packet


def collect_open_questions(*objects: dict[str, Any]) -> list[str]:
    out = []
    for obj in objects:
        if isinstance(obj, dict):
            _walk_collect(obj, out)
    # de-dup
    seen = set()
    dedup = []
    for q in out:
        if q in seen:
            continue
        seen.add(q)
        dedup.append(q)
    return dedup


def _walk_collect(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "open_questions" and isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip())
            else:
                _walk_collect(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_collect(v, out)
