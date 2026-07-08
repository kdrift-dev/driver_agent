"""OpenEvolve ``evaluation_file`` for the evolutionary patch search.

OpenEvolve imports this file by path and calls ``evaluate(path)`` once per
candidate genome. The genome is the in-scope driver source, concatenated with
file delimiters and wrapped in EVOLVE-BLOCK markers (see ``evolve_search.py``).
Each mutant is unpacked, diffed against the pristine old-ref source, normalized,
checked against the edit-scope policy, built with a shared ``BuildSession`` and
mapped to OpenEvolve metrics. Context is passed out-of-band via the
``DRIVER_AGENT_EVAL_CONTEXT`` env var pointing at a JSON file written by the
controller; evaluation runs serially because the session shares two worktrees.
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

_CTX_PATH = os.environ.get("DRIVER_AGENT_EVAL_CONTEXT", "")
_CTX: dict[str, Any] = {}
if _CTX_PATH and Path(_CTX_PATH).exists():
    _CTX = json.loads(Path(_CTX_PATH).read_text(encoding="utf-8"))
_REPO_ROOT = _CTX.get("package_parent")
if _REPO_ROOT and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from driver_agent_pregen.evolve_fitness import validation_to_metrics
from driver_agent_pregen.patch_generation import _edit_scope_policy_check, _function_key
from driver_agent_pregen.patch_line_fixer import fix_patch_line_numbers
from driver_agent_pregen.repo_utils import RepositoryAccessor
from driver_agent_pregen.validation import BuildSession

FILE_DELIM_PREFIX = "// ===== DRIVER-AGENT FILE: "
FILE_DELIM_SUFFIX = " ====="
EVOLVE_MARKERS = ("// EVOLVE-BLOCK-START", "// EVOLVE-BLOCK-END")

_SESSION: BuildSession | None = None
_SESSION_INIT_FAILED: str | None = None


def build_session_from_ctx(ctx: dict[str, Any]) -> BuildSession:
    """Construct an unprepared ``BuildSession`` from an eval-context dict."""
    accessor = RepositoryAccessor(
        repo=Path(ctx["repo"]) if ctx.get("repo") else None,
        old_ref=ctx.get("old_ref"),
        new_ref=ctx.get("new_ref"),
    )
    return BuildSession(
        accessor=accessor,
        run_dir=Path(ctx["run_dir"]),
        module_path=ctx["module_path"],
        old_ref=ctx.get("old_ref"),
        new_ref=ctx.get("new_ref"),
        jobs=int(ctx.get("jobs", 8)),
        kernel_config_target=ctx.get("kernel_config_target", "defconfig"),
        kbuild_target=ctx.get("kbuild_target"),
        target_source_files=ctx.get("target_source_files"),
    )


def _get_session() -> BuildSession | None:
    """Lazily prepare the shared build session once, reusing pre-warmed worktrees."""
    global _SESSION, _SESSION_INIT_FAILED
    if _SESSION is not None or _SESSION_INIT_FAILED is not None:
        return _SESSION

    session = build_session_from_ctx(_CTX)
    prep = session.prepare()
    if not prep.get("ok"):
        _SESSION_INIT_FAILED = str(prep.get("error", "build_session_prepare_failed"))
        return None
    _SESSION = session
    return _SESSION


def _unpack_genome(genome_text: str) -> dict[str, str]:
    """Split a concatenated genome into ``{rel_path: source_without_markers}``."""
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in genome_text.splitlines():
        stripped = line.rstrip("\n")
        if stripped.startswith(FILE_DELIM_PREFIX) and stripped.endswith(FILE_DELIM_SUFFIX):
            rel = stripped[len(FILE_DELIM_PREFIX) : -len(FILE_DELIM_SUFFIX)].strip()
            current = rel
            files[current] = []
            continue
        if stripped.strip() in EVOLVE_MARKERS:
            continue
        if current is None:
            continue
        files[current].append(line)
    return {rel: "\n".join(lines).rstrip("\n") + "\n" for rel, lines in files.items()}


def _unified_diff(rel: str, old_text: str, new_text: str) -> str:
    """git-apply-compatible unified diff for a single file, empty if unchanged."""
    if old_text == new_text:
        return ""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}")
    body = "".join(diff)
    if not body:
        return ""
    return f"diff --git a/{rel} b/{rel}\n{body}"


def genome_to_patch(old_sources: dict[str, str], genome_text: str) -> dict[str, Any]:
    """Derive a normalized unified diff from a genome; shared by scoring and winner conversion."""
    mutant = _unpack_genome(genome_text)
    patch_parts: list[str] = []
    touched: list[str] = []
    for rel, old_text in old_sources.items():
        part = _unified_diff(rel, old_text, mutant.get(rel, old_text))
        if part:
            patch_parts.append(part)
            touched.append(rel)
    raw_patch = "".join(patch_parts)
    ok, fixed_patch, msgs = fix_patch_line_numbers(raw_patch, file_contents=old_sources)
    return {
        "raw_patch": raw_patch,
        "fixed_patch": fixed_patch,
        "line_fix_ok": bool(ok),
        "line_fix_messages": list(msgs or []),
        "touched_files": touched,
    }


def _editable_keys(impact_graph: dict[str, Any]) -> set[tuple[str, str]]:
    """Edit_candidate function keys, pre-authorized for the evolutionary search."""
    keys: set[tuple[str, str]] = set()
    for item in impact_graph.get("edit_candidate_functions", []) or []:
        key = _function_key(item)
        if key:
            keys.add(key)
    return keys


def evaluate(program_path: str) -> Any:
    """OpenEvolve entry point; returns metrics, optionally wrapped with artifacts."""
    run_dir = Path(_CTX["run_dir"])
    extracted_dir = Path(_CTX["extracted_dir"])
    impact_graph = _CTX.get("impact_graph") or {}
    old_sources: dict[str, str] = _CTX.get("old_sources") or {}

    genome_text = Path(program_path).read_text(encoding="utf-8", errors="replace")

    bridge = genome_to_patch(old_sources, genome_text)
    raw_patch = bridge["raw_patch"]
    fixed_patch = bridge["fixed_patch"]

    if not raw_patch.strip():
        return _result(
            validation_to_metrics(
                build={"apply": {"ok": True, "no_op_candidate": True}, "compile": {"success": False, "runs": []}},
                patch_stats={"patch_size": 0, "files_touched": 0},
            ),
            {},
        )

    editable_keys = _editable_keys(impact_graph)
    scope_check = _edit_scope_policy_check(
        patch_text=fixed_patch,
        extracted_dir=extracted_dir,
        impact_graph=impact_graph,
        candidate_rationale_text="",
        extra_editable_keys=editable_keys,
        citation_exempt_keys=editable_keys,
    )

    patch_stats = {
        "patch_size": sum(
            1 for ln in fixed_patch.splitlines() if ln[:1] in ("+", "-") and not ln.startswith(("+++", "---"))
        ),
        "files_touched": len(bridge["touched_files"]),
    }

    artifacts: dict[str, Any] = {}
    if not scope_check.get("ok"):
        metrics = validation_to_metrics(
            build={"apply": {"ok": False, "reason": "edit_scope_policy_violation"}, "compile": {"success": False, "runs": []}},
            edit_scope_check=scope_check,
            patch_stats=patch_stats,
        )
        artifacts["edit_scope_violations"] = json.dumps(scope_check.get("violations", [])[:8])
        return _result(metrics, artifacts)

    session = _get_session()
    if session is None:
        metrics = validation_to_metrics(
            build={"apply": {"ok": False}, "compile": {"success": False, "runs": []}},
            edit_scope_check=scope_check,
            patch_stats=patch_stats,
        )
        return _result(metrics, {"session_error": _SESSION_INIT_FAILED or "no_session"})
    build = session.evaluate_patch(fixed_patch)

    metrics = validation_to_metrics(build=build, edit_scope_check=scope_check, patch_stats=patch_stats)

    compile_block = build.get("compile") or {}
    if not compile_block.get("success"):
        from driver_agent_pregen.validation import _compile_failure_text

        artifacts["compile_stderr"] = _compile_failure_text(compile_block)[-6000:]
    apply_block = build.get("apply") or {}
    if not apply_block.get("ok"):
        artifacts["apply_error"] = str(apply_block.get("stderr") or apply_block.get("reason") or "")[-2000:]

    return _result(metrics, artifacts)


def _result(metrics: dict[str, float], artifacts: dict[str, Any]) -> Any:
    """Return an EvaluationResult when artifacts are present, else plain metrics."""
    if not artifacts:
        return metrics
    try:
        from openevolve.evaluation_result import EvaluationResult

        return EvaluationResult(metrics=metrics, artifacts=artifacts)
    except Exception:
        return metrics
