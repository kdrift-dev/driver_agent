from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .utils import read_json, read_text


_STATUS_PASS = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_UNKNOWN = "UNKNOWN"
_STATUS_SKIPPED = "SKIPPED"
_STATUS_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


_HUNK_RE = re.compile(r"@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$")
_SYMBOL_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)")


def _status_from_checks(checks: list[dict[str, Any]]) -> str:
    if any(c.get("status") == _STATUS_FAIL for c in checks):
        return _STATUS_FAIL
    concrete = [c for c in checks if c.get("status") not in {_STATUS_UNKNOWN, _STATUS_SKIPPED, _STATUS_NOT_IMPLEMENTED}]
    if concrete and all(c.get("status") == _STATUS_PASS for c in concrete):
        return _STATUS_PASS
    if any(c.get("status") == _STATUS_UNKNOWN for c in checks):
        return _STATUS_UNKNOWN
    if all(c.get("status") == _STATUS_NOT_IMPLEMENTED for c in checks):
        return _STATUS_NOT_IMPLEMENTED
    if all(c.get("status") == _STATUS_SKIPPED for c in checks):
        return _STATUS_SKIPPED
    return _STATUS_UNKNOWN


def _selected_candidate(patch_generation: dict[str, Any]) -> dict[str, Any]:
    cands = patch_generation.get("patch_candidates") or []
    if not cands:
        return {}
    selected_id = patch_generation.get("selected_candidate_id") or cands[0].get("candidate_id")
    for cand in cands:
        if cand.get("candidate_id") == selected_id:
            return cand
    return cands[0]


def _patch_text(run_dir: Path, patch_generation: dict[str, Any], validation: dict[str, Any]) -> str:
    # Validate the exact effective patch that was compiled. If deterministic
    # compile_autofix adjusted the selected LLM patch, its recorded diff is the
    # artifact that downstream validation should inspect.
    autofix_path = ((validation.get("compile_autofix") or {}).get("patch_path") or "")
    if autofix_path:
        p = Path(autofix_path)
        if p.exists():
            return read_text(p)
    cand = _selected_candidate(patch_generation)
    rel = cand.get("fixed_patch_path") or cand.get("raw_patch_path")
    if rel:
        p = run_dir / rel
        if p.exists():
            return read_text(p)
    apply_path = ((validation.get("apply") or {}).get("patch_path") or "")
    if apply_path:
        p = Path(apply_path)
        if p.exists():
            return read_text(p)
    return ""


def _patch_has_effective_changes(patch_text: str) -> bool:
    removed: list[str] = []
    added: list[str] = []
    for line in (patch_text or "").splitlines():
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    if not removed and not added:
        return False
    if len(removed) == len(added) and removed == added:
        return False
    return True


def _effective_patch_required(obligations_payload: dict[str, Any]) -> bool:
    """Return true only for obligations that mechanically require an edit.

    Annotation-only or revalidation-only obligations may be satisfied by the
    old source. They should keep the case in migration scope, but should not
    make a no-op candidate fail by themselves.
    """
    for item in obligations_payload.get("obligations", []) or []:
        if item.get("hard_checks"):
            return True
        if item.get("replacement_candidates") and str(item.get("semantic_strength", "")).lower() == "strong":
            return True
    for item in obligations_payload.get("semantic_obligations", []) or []:
        if str(item.get("strength", "")).lower() == "strong" and item.get("replacement_candidates"):
            return True
    return False


def _symbol_name(raw: str) -> str:
    raw = (raw or "").strip()
    m = _SYMBOL_PREFIX_RE.match(raw)
    return m.group(1) if m else raw.split()[0] if raw else ""


def _candidate_symbols(candidates: list[Any]) -> list[str]:
    out: list[str] = []
    for item in candidates or []:
        if isinstance(item, dict):
            sym = item.get("symbol") or item.get("name") or ""
        else:
            sym = str(item)
        sym = _symbol_name(sym)
        if sym and sym not in out:
            out.append(sym)
    return out


def _load_function_index(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    payload = read_json(run_dir / "extracted" / "driver_functions.json", {}) or {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for fn in payload.get("functions", []):
        rel = fn.get("file")
        if not rel:
            continue
        by_file.setdefault(rel, []).append(fn)
    for fns in by_file.values():
        fns.sort(key=lambda x: int(x.get("start_line") or 0))
    return by_file


def _function_at(functions: list[dict[str, Any]], line_no: int) -> str:
    for fn in functions:
        start = int(fn.get("start_line") or 0)
        end = int(fn.get("end_line") or 0)
        if start <= line_no <= end:
            return str(fn.get("name") or "")
    return ""


def _modified_functions_from_patch(run_dir: Path, patch_text: str) -> dict[str, Any]:
    funcs_by_file = _load_function_index(run_dir)
    modified: dict[str, set[str]] = {}
    global_edits: dict[str, list[int]] = {}
    current_file = ""
    old_line = 0
    for raw in (patch_text or "").splitlines():
        m_file = _DIFF_FILE_RE.match(raw)
        if m_file:
            current_file = m_file.group(2)
            continue
        m_hunk = _HUNK_RE.search(raw)
        if m_hunk:
            old_line = int(m_hunk.group(1))
            continue
        if not current_file or raw.startswith(("--- ", "+++ ", "index ")):
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            line_no = max(old_line, 1)
            fn = _function_at(funcs_by_file.get(current_file, []), line_no)
            if fn:
                modified.setdefault(current_file, set()).add(fn)
            else:
                global_edits.setdefault(current_file, []).append(line_no)
            old_line += 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            line_no = max(old_line, 1)
            fn = _function_at(funcs_by_file.get(current_file, []), line_no)
            if fn:
                modified.setdefault(current_file, set()).add(fn)
            else:
                global_edits.setdefault(current_file, []).append(line_no)
        elif raw.startswith(" "):
            old_line += 1
    return {
        "modified_functions": [
            {"file": rel, "function": fn}
            for rel, names in sorted(modified.items())
            for fn in sorted(names)
        ],
        "global_edits": [
            {"file": rel, "line": line}
            for rel, lines in sorted(global_edits.items())
            for line in lines
        ],
    }


def _editable_sets(impact_graph: dict[str, Any]) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
    editable: set[tuple[str, str]] = set()
    context: set[tuple[str, str]] = set()
    affected: set[tuple[str, str]] = set()

    for item in impact_graph.get("editable_functions") or []:
        editable.add((str(item.get("file") or ""), str(item.get("function") or "")))
    for item in impact_graph.get("context_functions") or []:
        context.add((str(item.get("file") or ""), str(item.get("function") or "")))
    for item in impact_graph.get("affected_functions") or []:
        key = (str(item.get("file") or ""), str(item.get("function") or ""))
        affected.add(key)
        tier = str(item.get("tier") or "")
        if tier in {"hard", "semantic"}:
            editable.add(key)
        elif tier == "context":
            context.add(key)
    return editable, context, affected


def _obligation_requires_replacement(obligation: dict[str, Any]) -> bool:
    candidates = obligation.get("replacement_candidates") or []
    if candidates:
        if obligation.get("migration_signal") and str(obligation.get("semantic_strength", "")).lower() == "strong":
            return True
        # Deterministic semantic-key validation should not require one of many
        # weak same-file candidates to appear. That caused false FAILs for
        # migrations where the correct fix is deleting an ops entry or assigning
        # a struct field. Require candidate presence only when kernel evidence
        # gives a single clear replacement.
        if len(candidates) == 1:
            ev = "\n".join(str(x) for x in candidates[0].get("evidence", [])).lower()
            if "same_kernel_definition_file_new_ref" in ev or "kernel_side_helper" in ev:
                hard_checks = "\n".join(str(x) for x in obligation.get("hard_checks", [])).lower()
                if any(x in hard_checks for x in ("replacement", "old symbol should not remain", "should not remain")):
                    return True
    return False


def _obligation_requires_old_absence(obligation: dict[str, Any]) -> bool:
    checks = "\n".join(str(x) for x in obligation.get("hard_checks", []) + obligation.get("semantic_checks", [])).lower()
    if any(x in checks for x in ("old symbol should not remain", "should not remain", "removed field should not be accessed")):
        return True
    seed = str(obligation.get("patch_guidance_seed") or "").lower()
    if "old symbol should not remain" in seed:
        return True
    return False


def _line_changes_with_symbol(patch_text: str, symbol: str, prefix: str) -> list[str]:
    if not symbol:
        return []
    # Very short tokens often come from conservative regex fallback scanning
    # (for example a one-letter false "function" symbol). Treating them as
    # semantic keys creates substring-style false failures even with word
    # boundaries, e.g. symbol "s" in scsi_done(...).
    if len(symbol) < 3:
        return []
    pat = re.compile(rf"\b{re.escape(symbol)}\b")
    out = []
    for line in patch_text.splitlines():
        if not line.startswith(prefix) or line.startswith(("+++", "---")):
            continue
        if pat.search(line[1:]):
            out.append(line)
    return out


def _line_changes_with_field_access(patch_text: str, field: str, prefix: str) -> list[str]:
    if not field or len(field) < 3:
        return []
    pat = re.compile(rf"(?:->|\.)\s*{re.escape(field)}\b")
    out = []
    for line in patch_text.splitlines():
        if not line.startswith(prefix) or line.startswith(("+++", "---")):
            continue
        if pat.search(line[1:]):
            out.append(line)
    return out


def validate_semantic_obligations(run_dir: Path) -> dict[str, Any]:
    """Build conservative deterministic migration-obligation checks for one run.

    This intentionally verifies only mechanically checkable facts. Anything
    requiring semantic equivalence, interprocedural ownership, locking, or real
    hardware behavior is reported as UNKNOWN/NOT_IMPLEMENTED instead of PASS.
    """
    validation = read_json(run_dir / "validation.json", {}) or {}
    patch_generation = read_json(run_dir / "patch_generation.json", {}) or {}
    impact_graph = read_json(run_dir / "extracted" / "impact_graph.json", {}) or {}
    obligations_payload = read_json(run_dir / "extracted" / "compatibility_obligations.json", {}) or {}
    obligations = obligations_payload.get("obligations", []) or []
    patch_text = _patch_text(run_dir, patch_generation, validation)
    cand = _selected_candidate(patch_generation)
    migration_scope_present = bool(
        obligations
        or impact_graph.get("affected_functions")
        or impact_graph.get("editable_functions")
        or obligations_payload.get("semantic_obligations")
    )
    effective_patch_required = _effective_patch_required(obligations_payload)

    checks: list[dict[str, Any]] = []

    selected_id = str(cand.get("candidate_id") or patch_generation.get("selected_candidate_id") or "")
    all_policy_rejections = patch_generation.get("policy_rejections") or []
    # Policy rejections are produced for discarded candidates as well as the
    # selected candidate. Validation must judge the effective patch that was
    # actually compiled, otherwise a rejected no-op LLM candidate can make a
    # later accepted heuristic/autofixed patch look invalid.
    policy_rejections = [
        r
        for r in all_policy_rejections
        if isinstance(r, dict) and str(r.get("candidate_id") or "") == selected_id
    ]
    checks.append(
        {
            "id": "L3-EDIT-SCOPE-POLICY",
            "name": "patch_generation_edit_scope_policy",
            "status": _STATUS_PASS if not policy_rejections else _STATUS_FAIL,
            "evidence": policy_rejections,
            "note": "Pass means patch-generation policy did not reject edits outside editable scope.",
        }
    )

    effective = _patch_has_effective_changes(patch_text)
    noop_status = _STATUS_PASS
    if bool(cand.get("no_op_candidate")) or not effective:
        noop_status = _STATUS_FAIL if effective_patch_required else _STATUS_SKIPPED
    checks.append(
        {
            "id": "L3-NOOP",
            "name": "candidate_has_effective_changes",
            "status": noop_status,
            "evidence": [
                f"candidate_id={cand.get('candidate_id', '')}",
                f"no_op_candidate={bool(cand.get('no_op_candidate'))}",
                f"patch_has_effective_changes={effective}",
                f"migration_scope_present={migration_scope_present}",
                f"effective_patch_required={effective_patch_required}",
            ],
            "note": "No-op is only a failure when an actionable migration obligation requires an effective patch.",
        }
    )

    mod_info = _modified_functions_from_patch(run_dir, patch_text)
    editable, context, affected = _editable_sets(impact_graph)
    outside: list[dict[str, str]] = []
    context_edits: list[dict[str, str]] = []
    for item in mod_info["modified_functions"]:
        key = (item["file"], item["function"])
        if key in context and key not in editable:
            context_edits.append(item)
        if editable and key not in editable:
            outside.append(item)
    checks.append(
        {
            "id": "L3-EDIT-SCOPE-MODIFIED-FUNCTIONS",
            "name": "modified_functions_within_edit_scope",
            "status": _STATUS_PASS if not outside and not context_edits else _STATUS_FAIL,
            "evidence": {
                "modified_functions": mod_info["modified_functions"],
                "global_edits": mod_info["global_edits"],
                "editable_functions": [{"file": f, "function": n} for f, n in sorted(editable)],
                "context_only_edits": context_edits,
                "outside_edit_scope_edits": outside,
            },
            "note": "Uses old-source function ranges and unified-diff hunks; global edits are reported but not treated as function-scope PASS.",
        }
    )

    for idx, obl in enumerate(obligations, 1):
        old_sym = _symbol_name(str(obl.get("affected_kernel_symbol") or obl.get("symbol") or ""))
        replacements = _candidate_symbols(obl.get("replacement_candidates") or [])
        if _obligation_requires_replacement(obl):
            added_hits = []
            for repl in replacements:
                hits = _line_changes_with_symbol(patch_text, repl, "+")
                if hits:
                    added_hits.append({"replacement": repl, "added_lines": hits[:10]})
            checks.append(
                {
                    "id": f"L3-REPLACEMENT-{idx:03d}",
                    "name": "required_replacement_symbol_added",
                    "status": _STATUS_PASS if added_hits else _STATUS_FAIL,
                    "obligation_id": obl.get("id"),
                    "symbol": old_sym,
                    "replacement_candidates": replacements,
                    "evidence": added_hits,
                    "note": "Patch-based semantic-key check; does not prove behavioral equivalence.",
                }
            )
        elif replacements:
            checks.append(
                {
                    "id": f"L3-REPLACEMENT-{idx:03d}",
                    "name": "replacement_symbol_optional_or_unsupported",
                    "status": _STATUS_UNKNOWN,
                    "obligation_id": obl.get("id"),
                    "symbol": old_sym,
                    "replacement_candidates": replacements,
                    "evidence": [],
                    "note": "Replacement candidates exist, but the obligation is not strong enough for deterministic required-edit validation.",
                }
            )

        if _obligation_requires_old_absence(obl):
            if len(old_sym) < 3:
                checks.append(
                    {
                        "id": f"L3-OLD-SYMBOL-ABSENCE-{idx:03d}",
                        "name": "old_symbol_removed_from_changed_lines",
                        "status": _STATUS_UNKNOWN,
                        "obligation_id": obl.get("id"),
                        "symbol": old_sym,
                        "evidence": {"reason": "symbol_too_short_for_reliable_patch_line_check"},
                        "note": "Short fallback-scanned tokens are not reliable semantic keys.",
                    }
                )
                continue
            raw_symbol = str(obl.get("affected_kernel_symbol") or obl.get("symbol") or "")
            if "(field)" in raw_symbol:
                removed_hits = _line_changes_with_field_access(patch_text, old_sym, "-")
                added_old_hits = _line_changes_with_field_access(patch_text, old_sym, "+")
            else:
                removed_hits = _line_changes_with_symbol(patch_text, old_sym, "-")
                added_old_hits = _line_changes_with_symbol(patch_text, old_sym, "+")
            checks.append(
                {
                    "id": f"L3-OLD-SYMBOL-ABSENCE-{idx:03d}",
                    "name": "old_symbol_removed_from_changed_lines",
                    "status": _STATUS_PASS if removed_hits and not added_old_hits else _STATUS_FAIL,
                    "obligation_id": obl.get("id"),
                    "symbol": old_sym,
                    "evidence": {"removed_lines": removed_hits[:10], "added_old_symbol_lines": added_old_hits[:10]},
                    "note": "Patch-line check only; macro indirection and untouched files are not resolved here.",
                }
            )

    checks.extend(
        [
            {
                "id": "L3-ERROR-PATH-PRESERVATION",
                "name": "error_path_preserved",
                "status": _STATUS_NOT_IMPLEMENTED,
                "evidence": [],
                "note": "Requires semantic/control-flow equivalence; first version intentionally does not claim PASS.",
            },
            {
                "id": "L3-OWNERSHIP-PAIRING",
                "name": "ownership_or_alloc_free_pairing_preserved",
                "status": _STATUS_NOT_IMPLEMENTED,
                "evidence": [],
                "note": "Cross-function resource lifetime requires interprocedural analysis; left for future work.",
            },
            {
                "id": "L3-LOCKING-LIFECYCLE",
                "name": "locking_or_lifecycle_contract_preserved",
                "status": _STATUS_NOT_IMPLEMENTED,
                "evidence": [],
                "note": "Locking/lifecycle correctness is not mechanically checked in this conservative validator.",
            },
        ]
    )

    return {
        "stage": "conservative_migration_obligation_validation",
        "status": _status_from_checks(checks) if migration_scope_present else _STATUS_SKIPPED,
        "migration_scope_present": migration_scope_present,
        "effective_patch_required": effective_patch_required,
        "scope": "mechanically-checkable subset only",
        "caveat": "PASS only means these conservative symbol/edit-scope checks passed; it does not prove driver correctness.",
        "modified_function_analysis": mod_info,
        "checks": checks,
    }
