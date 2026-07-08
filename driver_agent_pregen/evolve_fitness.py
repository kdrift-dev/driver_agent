"""Multi-objective fitness mapping for the evolutionary patch search.

Converts the Driver-Agent validation evidence vector into the metrics dict that
OpenEvolve consumes: it maximizes ``combined_score`` and uses the other keys as
MAP-Elites feature dimensions. Kept dependency-free so it is testable without
an LLM or a kernel build.
"""

from __future__ import annotations

from typing import Any

W_APPLIES = 0.30
W_BUILDS = 0.40
W_RUNTIME = 0.20
W_OBLIGATION = 0.10

COMPILE_ERROR_PENALTY = 0.01
COMPILE_ERROR_PENALTY_CAP = 0.20


def _count_failed_compile_runs(compile_block: dict[str, Any]) -> int:
    runs = compile_block.get("runs") or []
    return sum(1 for r in runs if not r.get("ok"))


def _level_status(summary_v2: dict[str, Any] | None, level: int) -> str:
    if not summary_v2:
        return "UNKNOWN"
    for entry in summary_v2.get("levels", []) or []:
        if entry.get("level") == level:
            return str(entry.get("status", "UNKNOWN")).upper()
    return "UNKNOWN"


def _runtime_passed(validation: dict[str, Any] | None) -> bool:
    if not validation:
        return False
    qemu = validation.get("qemu") or {}
    suite = validation.get("suite_execution") or {}
    qemu_pass = bool(qemu.get("success")) and not qemu.get("skipped")
    suite_pass = bool(suite.get("success")) and not suite.get("skipped")
    return qemu_pass or suite_pass


def validation_to_metrics(
    *,
    build: dict[str, Any],
    edit_scope_check: dict[str, Any] | None = None,
    patch_stats: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    summary_v2: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Map the evidence vector into OpenEvolve metrics.

    ``build`` is the dict returned by ``validation.BuildSession.evaluate_patch``.
    ``validation`` and ``summary_v2`` are the optional richer artifacts from the
    authoritative validation pass. A candidate must apply, stay in edit-scope and
    change something to score at all; it then climbs the evidence ladder, with a
    small penalty gradient on compile-error count below the build threshold. A
    failed compile with no runs at all (no compile dirs found) takes the full
    penalty so it ranks below every candidate that reached the compiler.
    """
    apply_block = build.get("apply") or {}
    compile_block = build.get("compile") or {}
    patch_stats = patch_stats or {}

    applied = bool(apply_block.get("ok"))
    no_op = bool(apply_block.get("no_op_candidate"))
    edit_scope_ok = bool((edit_scope_check or {}).get("ok", True))
    builds = bool(compile_block.get("success"))
    failed_runs = _count_failed_compile_runs(compile_block)
    runtime_pass = _runtime_passed(validation)
    obligation_pass = _level_status(summary_v2, 3) == "PASS"

    score = 0.0
    valid_base = applied and edit_scope_ok and not no_op
    if valid_base:
        score += W_APPLIES
        if builds:
            score += W_BUILDS
            if runtime_pass:
                score += W_RUNTIME
            if obligation_pass:
                score += W_OBLIGATION
        elif failed_runs == 0:
            score -= COMPILE_ERROR_PENALTY_CAP
        else:
            score -= min(COMPILE_ERROR_PENALTY * failed_runs, COMPILE_ERROR_PENALTY_CAP)

    score = max(0.0, round(score, 4))

    if not valid_base:
        build_tier = 0
    elif not builds:
        build_tier = 1
    elif not runtime_pass:
        build_tier = 2
    else:
        build_tier = 3

    return {
        "combined_score": score,
        "build_tier": float(build_tier),
        "build_pass": 1.0 if builds else 0.0,
        "runtime_pass": 1.0 if runtime_pass else 0.0,
        "edit_scope_ok": 1.0 if edit_scope_ok else 0.0,
        "obligation_pass": 1.0 if obligation_pass else 0.0,
        "no_op": 1.0 if no_op else 0.0,
        "compile_error_count": float(failed_runs),
        "patch_size": float(patch_stats.get("patch_size", 0)),
        "files_touched": float(patch_stats.get("files_touched", 0)),
    }
