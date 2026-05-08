from __future__ import annotations

from typing import Any

from .utils import write_json


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
    else:
        semantic.append("re-check usage against new kernel definition")
        seed = "inspect changed contract and align call site behavior"

    return hard, semantic, seed


def _confidence_from_diff(item: dict) -> str:
    if item.get("confidence") == "high":
        return "high"
    if item.get("definition_changed"):
        return "medium"
    return "low"


def run_obligation_builder(
    extracted_dir,
    kernel_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
) -> dict[str, Any]:
    affected_map = {}
    for afn in impact_graph.get("affected_functions", []):
        fn = afn.get("function")
        for sym in afn.get("uses_changed_kernel_symbols", []):
            affected_map.setdefault(sym, set()).add(fn)

    obligations = []
    idx = 1
    for item in kernel_diffs.get("items", []):
        symbol = item.get("symbol")
        kind = item.get("kind", "unknown")
        sym_key = f"{symbol} ({kind})"

        missing_new = any("missing in new_ref" in q for q in item.get("open_questions", []))
        definition_changed = bool(item.get("definition_changed"))
        change_type = item.get("change_type_guess", "unknown")
        affected_fns = sorted(affected_map.get(sym_key, set()))

        # Keep obligations focused on concrete migration deltas.
        if not definition_changed and not missing_new:
            continue
        if change_type == "unknown" and not missing_new:
            continue
        if not affected_fns:
            # Skip noisy symbols not linked to any affected function slice.
            continue

        hard, semantic, seed = _checks_for_change(change_type, missing_new)

        evidence = [
            {
                "type": "definition_diff",
                "value": item.get("definition_diff", "")[:4000],
            }
        ]
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
            }
        )
        idx += 1

    payload = {"obligations": obligations}
    write_json(extracted_dir / "compatibility_obligations.json", payload)
    return payload
