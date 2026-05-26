from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any

from .semantic_obligation_validator import validate_semantic_obligations
from .utils import read_json, slugify, write_json, write_text


PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
SKIPPED = "SKIPPED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


SPEC_CONSISTENCY_CAVEAT = (
    "This checks whether the patch satisfies the generated local specification "
    "and stated obligations. It does not prove that the specification is complete "
    "or that the driver is globally correct."
)


def _stage_map(validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(x.get("stage")): x for x in validation.get("validation_stage_results", []) if isinstance(x, dict)}


def _stage_lookup(stages: dict[str, dict[str, Any]], *names: str) -> dict[str, Any]:
    for name in names:
        if name in stages:
            return stages[name]
    return {}


def _map_stage_status(raw: str) -> str:
    raw = str(raw or "").lower()
    if raw == "passed":
        return PASS
    if raw == "failed":
        return FAIL
    if raw == "skipped":
        return SKIPPED
    return UNKNOWN


def _selected_candidate(patch_generation: dict[str, Any]) -> dict[str, Any]:
    cands = patch_generation.get("patch_candidates") or []
    if not cands:
        return {}
    selected_id = patch_generation.get("selected_candidate_id") or cands[0].get("candidate_id")
    for cand in cands:
        if cand.get("candidate_id") == selected_id:
            return cand
    return cands[0]


def _combine_status(statuses: list[str], *, pass_if_any_pass: bool = False) -> str:
    vals = [s for s in statuses if s]
    if not vals:
        return UNKNOWN
    if any(s == FAIL for s in vals):
        return FAIL
    if pass_if_any_pass and any(s == PASS for s in vals):
        return PASS
    if all(s == PASS for s in vals):
        return PASS
    if all(s == SKIPPED for s in vals):
        return SKIPPED
    if any(s == UNKNOWN for s in vals):
        return UNKNOWN
    if all(s == NOT_IMPLEMENTED for s in vals):
        return NOT_IMPLEMENTED
    return UNKNOWN


def _run_output_text(run: dict[str, Any]) -> str:
    return f"{run.get('stdout', '')}\n{run.get('stderr', '')}".lower()


def _run_is_effectively_skipped(run: dict[str, Any]) -> bool:
    if run.get("skipped"):
        return True
    text = _run_output_text(run)
    skip_markers = (
        "suite-skip:",
        "suite_skip:",
        "hardware not present",
        "hardware unavailable",
        "hardware-dependent tests",
        "no compatible device",
        "module not available in this build",
        "not all kconfig options selected in kunitconfig",
        "unsatisfied dependencies",
        "aren't available on uml",
        "exit status 77",
    )
    return any(marker in text for marker in skip_markers)


def _checks_status(checks: list[dict[str, Any]]) -> str:
    return _combine_status([str(c.get("status") or UNKNOWN) for c in checks])


def _real_suite_status(validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    suite = validation.get("suite_execution") or {}
    runs = suite.get("runs") or []
    if not runs:
        return SKIPPED, {"reason": stages.get("suite_execution", {}).get("reason") or "no suite runs"}
    skipped = [r for r in runs if _run_is_effectively_skipped(r)]
    real_runs = [r for r in runs if not _run_is_effectively_skipped(r)]
    failed = [r for r in real_runs if not r.get("ok")]
    if failed:
        return FAIL, {"runs": len(runs), "real_runs": len(real_runs), "skipped_runs": len(skipped), "first_failure": failed[0].get("reason") or failed[0].get("stderr") or failed[0].get("stdout")}
    if real_runs:
        return PASS, {"runs": len(runs), "real_runs": len(real_runs), "skipped_runs": len(skipped)}
    return SKIPPED, {"runs": len(runs), "real_runs": 0, "skipped_runs": len(skipped), "reason": "; ".join(sorted({str(r.get('reason') or 'skipped') for r in skipped}))}


def _real_qemu_status(validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    qemu = validation.get("qemu") or {}
    if not qemu.get("enabled"):
        return SKIPPED, {"reason": "qemu disabled"}
    runs = qemu.get("runs") or []
    if not runs:
        stage = stages.get("qemu_execution", {})
        raw_status = _map_stage_status(stage.get("status"))
        # No run is not evidence of runtime success. Keep failures visible but do not call it PASS.
        if raw_status == FAIL:
            return FAIL, {"reason": stage.get("reason") or "qemu enabled but no run detected"}
        return SKIPPED, {"reason": stage.get("reason") or "no qemu run"}
    skipped = [r for r in runs if _run_is_effectively_skipped(r)]
    real_runs = [r for r in runs if not _run_is_effectively_skipped(r)]
    failed = [r for r in real_runs if not r.get("ok")]
    if failed:
        return FAIL, {"runs": len(runs), "real_runs": len(real_runs), "skipped_runs": len(skipped), "first_failure": failed[0].get("reason") or failed[0].get("stderr") or failed[0].get("stdout")}
    if real_runs:
        return PASS, {"runs": len(runs), "real_runs": len(real_runs), "skipped_runs": len(skipped)}
    return SKIPPED, {"runs": len(runs), "real_runs": 0, "skipped_runs": len(skipped), "reason": "; ".join(sorted({str(r.get('reason') or 'skipped') for r in skipped}))}


def _level0_patch_sanity(validation: dict[str, Any], patch_generation: dict[str, Any], semantic_validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cand = _selected_candidate(patch_generation)
    no_op = bool(cand.get("no_op_candidate"))
    migration_scope_present = bool(semantic_validation.get("migration_scope_present"))
    effective_patch_required = bool(semantic_validation.get("effective_patch_required"))
    no_op_status = FAIL if (no_op and effective_patch_required) else PASS
    checks = [
        {
            "id": "L0-PATCH-APPLY",
            "name": "patch_applies_to_old_ref",
            "status": _map_stage_status(stages.get("patch_apply", {}).get("status")),
            "evidence": stages.get("patch_apply", {}),
        },
        {
            "id": "L0-NOOP",
            "name": "candidate_has_effective_changes",
            "status": no_op_status,
            "evidence": {
                "candidate_id": cand.get("candidate_id"),
                "no_op_candidate": no_op,
                "migration_scope_present": migration_scope_present,
                "effective_patch_required": effective_patch_required,
            },
            "note": "No-op is a patch-sanity failure only when an actionable migration obligation requires an effective patch.",
        },
        {
            "id": "L0-STAGE-PATCHED-FILES",
            "name": "patched_files_staged_into_new_tree",
            "status": _map_stage_status(stages.get("stage_patched_files", {}).get("status")),
            "evidence": validation.get("stage_patched_files", {}),
        },
    ]
    for c in semantic_validation.get("checks", []):
        if c.get("id") in {"L3-EDIT-SCOPE-POLICY", "L3-EDIT-SCOPE-MODIFIED-FUNCTIONS"}:
            cc = dict(c)
            cc["id"] = cc["id"].replace("L3-", "L0-")
            checks.append(cc)
    return {
        "level": 0,
        "name": "patch_sanity",
        "status": _checks_status(checks),
        "proves": "The selected patch can be applied/staged and does not obviously violate edit-scope policy.",
        "does_not_prove": "Runtime behavior, complete semantic correctness, or hardware correctness.",
        "checks": checks,
    }


def _level1_build(validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    checks = [
        {
            "id": "L1-COMPILE-PREPARE",
            "name": "kernel_build_prepare",
            "status": _map_stage_status(stages.get("compile_prepare", {}).get("status")),
            "evidence": stages.get("compile_prepare", {}),
        },
        {
            "id": "L1-COMPILE-MODULES",
            "name": "target_module_compile",
            "status": PASS if (validation.get("compile") or {}).get("success") else _map_stage_status(stages.get("compile_modules", {}).get("status")),
            "evidence": {
                "compiled_dirs": (validation.get("compile") or {}).get("compiled_dirs", []),
                "error": (validation.get("compile") or {}).get("error", ""),
                "stage": stages.get("compile_modules", {}),
            },
        },
    ]
    return {
        "level": 1,
        "name": "build_correctness",
        "status": _checks_status(checks),
        "proves": "The patched target module builds in the attempted configuration.",
        "does_not_prove": "Semantic correctness, full config/architecture coverage, or hardware behavior.",
        "checks": checks,
    }


def _level2_runtime(validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    suite_status, suite_ev = _real_suite_status(validation, stages)
    qemu_status, qemu_ev = _real_qemu_status(validation, stages)
    test_generation_status = _map_stage_status(stages.get("test_generation", {}).get("status"))
    if test_generation_status == FAIL:
        runtime_status = FAIL
    elif suite_status == FAIL or qemu_status == FAIL:
        runtime_status = FAIL
    elif PASS in {suite_status, qemu_status}:
        runtime_status = PASS
    elif suite_status == SKIPPED and qemu_status == SKIPPED:
        runtime_status = SKIPPED
    else:
        runtime_status = UNKNOWN
    checks = [
        {
            "id": "L2-TEST-GENERATION",
            "name": "system_test_generation",
            "status": test_generation_status,
            "evidence": {"stage": stages.get("test_generation", {}), "parse_status": (validation.get("test_generation") or {}).get("parse_status")},
        },
        {"id": "L2-SUITE-EXECUTION", "name": "suite_execution_real_run", "status": suite_status, "evidence": suite_ev},
        {"id": "L2-QEMU-EXECUTION", "name": "qemu_or_kunit_execution_real_run", "status": qemu_status, "evidence": qemu_ev},
    ]
    return {
        "level": 2,
        "name": "runtime_hooks",
        "status": runtime_status,
        "proves": "Only that generated runtime hooks executed successfully when real suite/QEMU runs occurred.",
        "does_not_prove": "Hardware-dependent probe/IRQ/PM paths unless the test actually exercises that hardware path.",
        "checks": checks,
    }


def _level3_migration(semantic_validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "level": 3,
        "name": "migration_obligation_validation",
        "status": semantic_validation.get("status", UNKNOWN),
        "proves": "Conservative symbol/edit-scope/semantic-key checks passed when status is PASS.",
        "does_not_prove": "Behavioral equivalence, complete error-path preservation, interprocedural ownership, locking, lifecycle, or hardware semantics.",
        "checks": semantic_validation.get("checks", []),
        "caveat": semantic_validation.get("caveat", ""),
    }


def _level4_spec(validation: dict[str, Any], stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pr = validation.get("post_reasoning") or {}
    parse_status = str(pr.get("parse_status", ""))
    verdict = str(pr.get("overall_verdict", "UNKNOWN")).upper()
    if parse_status != "ok":
        status = FAIL
    elif verdict == "PASS":
        status = PASS
    elif verdict in {"UNKNOWN", "MATCH", "MISMATCH"}:
        # Historical outputs sometimes used MATCH/MISMATCH; only PASS is treated as pass.
        status = UNKNOWN if verdict == "UNKNOWN" else FAIL
    else:
        status = FAIL
    return {
        "level": 4,
        "name": "spec_consistency",
        "status": status,
        "proves": "The patch is consistent with the generated local specification and stated obligations when status is PASS.",
        "does_not_prove": "The generated specification is complete or that the driver is globally correct.",
        "caveat": SPEC_CONSISTENCY_CAVEAT,
        "checks": [
            {
                "id": "L4-POST-REASONING",
                "name": "post_reasoning_as_spec_consistency",
                "status": status,
                "evidence": {
                    "stage": _stage_lookup(stages, "spec_consistency", "post_reasoning"),
                    "parse_status": parse_status,
                    "overall_verdict": verdict,
                    "confidence": pr.get("confidence", ""),
                    "unsupported_assumptions": pr.get("unsupported_assumptions", []),
                    "evidence_used": pr.get("evidence_used", []),
                },
                "caveat": SPEC_CONSISTENCY_CAVEAT,
            }
        ],
    }


def _future_level(level: int, name: str, note: str) -> dict[str, Any]:
    return {
        "level": level,
        "name": name,
        "status": NOT_IMPLEMENTED,
        "proves": "Nothing in the current implementation.",
        "does_not_prove": "This evidence layer is reserved for future stronger validation.",
        "checks": [{"id": f"L{level}-{name.upper().replace('-', '_')}", "name": name, "status": NOT_IMPLEMENTED, "note": note}],
    }


def _overall_evidence_status(levels: list[dict[str, Any]]) -> str:
    return _overall_validation_result(levels)


def _overall_validation_result(levels: list[dict[str, Any]]) -> str:
    critical = [levels[0].get("status"), levels[1].get("status"), levels[3].get("status")]
    if any(s == FAIL for s in critical):
        return "FAILED_VALIDATION"
    if not all(s == PASS for s in critical):
        return "INCOMPLETE_VALIDATION"

    runtime_status = levels[2].get("status")
    if runtime_status == FAIL:
        return "PASS_CORE_RUNTIME_FAILED"
    if runtime_status in {SKIPPED, UNKNOWN, NOT_IMPLEMENTED}:
        return "PASS_CORE_RUNTIME_NOT_RUN"
    if runtime_status == PASS:
        return "PASS_ALL_VALIDATION"
    return "INCOMPLETE_VALIDATION"


def build_validation_summary_v2(run_dir: str | Path, *, write_outputs: bool = True) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    validation = read_json(run_dir / "validation.json", {}) or {}
    patch_generation = read_json(run_dir / "patch_generation.json", {}) or {}
    metadata = read_json(run_dir / "metadata.json", {}) or {}
    context_packet = read_json(run_dir / "context_packet.json", {}) or {}
    stages = _stage_map(validation)
    semantic_validation = validate_semantic_obligations(run_dir)

    levels = [
        _level0_patch_sanity(validation, patch_generation, semantic_validation, stages),
        _level1_build(validation, stages),
        _level2_runtime(validation, stages),
        _level3_migration(semantic_validation),
        _level4_spec(validation, stages),
        _future_level(5, "static_kernel_checks", "Sparse/Smatch/Coccinelle/checkpatch integration is not implemented in v2."),
        _future_level(6, "cbmc_local_obligation_validation", "CBMC harness generation for affected slices is future work; output must remain UNKNOWN/NOT_IMPLEMENTED for now."),
        _future_level(7, "evaluation_only_reference_or_manual_verdict", "Reference diff/manual verdict are evaluation-only and are not agent-input correctness evidence."),
    ]

    summary = {
        "schema_version": "validation_summary_v2.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "run_dir": run_dir.as_posix(),
        "case_id": validation.get("case_id") or metadata.get("case_id") or context_packet.get("case_id"),
        "module_path": metadata.get("module_path") or context_packet.get("module_path"),
        "input_policy": {
            "agent_input": "old target module source plus old/new kernel-side definitions/diffs only",
            "evaluation_only": "reference/new target driver diff and manual verdicts, if used, must stay outside agent input",
            "spec_consistency_caveat": SPEC_CONSISTENCY_CAVEAT,
        },
        "overall_validation_result": _overall_validation_result(levels),
        "overall_evidence_status": _overall_evidence_status(levels),
        "not_a_correctness_proof": True,
        "levels": levels,
        "raw_validation_status": validation.get("status", ""),
        "open_questions": validation.get("open_questions", []),
    }
    if write_outputs:
        write_json(run_dir / "validation_summary_v2.json", summary)
        write_text(run_dir / "validation_summary_v2.md", render_validation_summary_markdown(summary))
    return summary


def render_validation_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Validation Summary v2: {summary.get('case_id') or ''}",
        "",
        f"- Module: `{summary.get('module_path') or ''}`",
        f"- Overall validation result: `{summary.get('overall_validation_result') or summary.get('overall_evidence_status')}`",
        f"- Not a correctness proof: `{summary.get('not_a_correctness_proof')}`",
        "",
        "## Caveat",
        "",
        SPEC_CONSISTENCY_CAVEAT,
        "",
        "## Evidence Levels",
        "",
    ]
    for level in summary.get("levels", []):
        lines.extend(
            [
                f"### Level {level.get('level')}: {level.get('name')}",
                "",
                f"- Status: `{level.get('status')}`",
                f"- Proves: {level.get('proves', '')}",
                f"- Does not prove: {level.get('does_not_prove', '')}",
                "",
                "Checks:",
            ]
        )
        for check in level.get("checks", []):
            note = check.get("note") or check.get("caveat") or ""
            suffix = f" — {note}" if note else ""
            lines.append(f"- `{check.get('id')}` `{check.get('status')}` {check.get('name')}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _find_run_dirs(root: Path) -> list[Path]:
    return sorted({p.parent for p in root.rglob("validation.json")})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build Driver-Agent validation_summary_v2 evidence vectors")
    p.add_argument("--run-dir", action="append", default=[], help="single completed run directory; may be repeated")
    p.add_argument("--runs-dir", default=None, help="directory containing completed runs; recursively finds validation.json")
    p.add_argument("--out-dir", default=None, help="optional external output directory for summaries, useful for read-only/root-owned runs")
    p.add_argument("--no-write", action="store_true", help="do not write validation_summary_v2 files")
    args = p.parse_args(argv)

    run_dirs = [Path(x).resolve() for x in args.run_dir]
    if args.runs_dir:
        run_dirs.extend(_find_run_dirs(Path(args.runs_dir).resolve()))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for rd in run_dirs:
        key = rd.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(rd)
    if not unique:
        p.error("provide --run-dir or --runs-dir")

    failed = 0
    for rd in unique:
        if not (rd / "validation.json").exists():
            print(f"[validation-summary-v2] skipped missing validation.json: {rd}")
            failed += 1
            continue
        write_in_place = (not args.no_write) and not args.out_dir
        summary = build_validation_summary_v2(rd, write_outputs=write_in_place)
        if args.out_dir and not args.no_write:
            out_root = Path(args.out_dir).resolve()
            case = slugify(str(summary.get("case_id") or rd.name))
            dest = out_root / case
            write_json(dest / "validation_summary_v2.json", summary)
            write_text(dest / "validation_summary_v2.md", render_validation_summary_markdown(summary))
        print(f"[validation-summary-v2] {summary.get('overall_validation_result') or summary.get('overall_evidence_status')} {rd}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
