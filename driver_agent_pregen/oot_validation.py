from __future__ import annotations

import importlib.util
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .reasoner_agent import run_post_reasoner_agent
from .repo_utils import RepositoryAccessor
from .utils import read_text, write_json, write_text


def _load_oot_runner_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "run_oot_qemu_case.py"
    spec = importlib.util.spec_from_file_location("_driver_agent_oot_runner", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load OOT runner helpers from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_STAGES = [
    "worktree_prepare",
    "patch_apply",
    "stage_patched_files",
    "compile_prepare",
    "compile_modules",
    "test_generation",
    "suite_execution",
    "qemu_execution",
    "post_reasoning",
]


def export_oot_source_snapshot(
    *,
    repo_url: str,
    ref: str,
    source_subdir: str,
    dest: Path,
    cache_root: Path,
) -> dict[str, Any]:
    runner = _load_oot_runner_module()
    return runner.export_external_source(
        repo_url=repo_url,
        ref=ref,
        source_subdir=source_subdir,
        dest=dest,
        cache_root=cache_root,
    )


def collect_oot_pre_patch_build_diagnostics(
    *,
    accessor: RepositoryAccessor,
    run_dir: Path,
    external_source_root: Path,
    jobs: int,
    make_extra_args: list[str] | None = None,
) -> dict[str, Any]:
    runner = _load_oot_runner_module()
    if accessor.mode != "git" or not accessor.repo or not accessor.old_ref or not accessor.new_ref:
        return {"enabled": False, "reason": "requires_git_repo_refs"}
    worktrees_dir = (run_dir / "worktrees").resolve()
    old_tree = worktrees_dir / "old"
    new_tree = worktrees_dir / "new"
    wt_old = runner.prepare_worktree(accessor.repo, accessor.old_ref, old_tree, keep_existing=True)
    wt_new = runner.prepare_worktree(accessor.repo, accessor.new_ref, new_tree, keep_existing=True)
    if not wt_old.get("ok") or not wt_new.get("ok"):
        return {"enabled": True, "ok": False, "reason": "worktree_prepare_failed", "old_prepare": wt_old, "new_prepare": wt_new}
    old_prep = runner.prepare_kernel(old_tree, jobs)
    new_prep = runner.prepare_kernel(new_tree, jobs)
    old_build = runner.build_external(old_tree, external_source_root, jobs, make_extra_args or [])
    new_build = runner.build_external(new_tree, external_source_root, jobs, make_extra_args or [])
    return {
        "enabled": True,
        "ok": bool(old_build.get("ok")) and not bool(new_build.get("ok")),
        "old_tree": old_tree.as_posix(),
        "new_tree": new_tree.as_posix(),
        "old_prepare": [_truncate_run(x) for x in old_prep],
        "new_prepare": [_truncate_run(x) for x in new_prep],
        "old_build": _truncate_run(old_build),
        "new_build": _truncate_run(new_build),
        "summary": {
            "old_build_passes": bool(old_build.get("ok")),
            "old_source_fails_on_new_kernel": not bool(new_build.get("ok")),
        },
    }


def filter_impact_graph_by_oot_compile_diagnostics(
    *,
    impact_graph: dict[str, Any],
    diagnostics: dict[str, Any],
    extracted_dir: Path,
    module_path: str,
) -> dict[str, Any]:
    stderr = str(((diagnostics.get("new_build") or {}).get("stderr")) or "")
    stdout = str(((diagnostics.get("new_build") or {}).get("stdout")) or "")
    text = stderr + "\n" + stdout
    if not text.strip():
        return impact_graph

    funcs_payload = {}
    try:
        funcs_payload = __import__("json").loads((extracted_dir / "driver_functions.json").read_text())
    except Exception:
        funcs_payload = {}
    functions = [f for f in funcs_payload.get("functions", []) or [] if isinstance(f, dict)]
    by_name = {str(f.get("name")): f for f in functions if f.get("name")}

    selected: set[str] = set()

    # Direct function-name mentions in compiler diagnostics.
    for name in by_name:
        if re.search(rf"\b{re.escape(name)}\b", text):
            selected.add(name)

    # Map file:line diagnostics back into synthetic module function ranges.
    for m in re.finditer(r"(?P<file>[A-Za-z0-9_./-]+\.c):(?P<line>\d+)(?::\d+)?:", text):
        root_rel = m.group("file")
        line = int(m.group("line"))
        syn_rel = f"{module_path.rstrip('/')}/{root_rel.strip('./')}"
        for fn in functions:
            if str(fn.get("file")) != syn_rel:
                continue
            start = int(fn.get("start_line", 0) or 0)
            end = int(fn.get("end_line", 0) or 0)
            if start <= line <= end:
                selected.add(str(fn.get("name")))

    # Attribute macro diagnostics often mention BUS_ATTR_RO(foo), while the
    # required callback function is foo_show.
    for attr in re.findall(r"BUS_ATTR_RO\s*\(\s*([A-Za-z_]\w*)\s*\)", text):
        show_fn = f"{attr}_show"
        if show_fn in by_name:
            selected.add(show_fn)

    if not selected:
        out = dict(impact_graph)
        out["oot_compile_diagnostic_filter"] = {
            "enabled": True,
            "selected_functions": [],
            "applied": False,
            "reason": "no_driver_functions_mapped_from_compile_diagnostics",
        }
        return out

    def keep_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [x for x in items if str(x.get("function")) in selected]

    out = dict(impact_graph)
    out["edit_required_functions"] = keep_items(list(impact_graph.get("edit_required_functions", []) or []))
    out["edit_candidate_functions"] = keep_items(list(impact_graph.get("edit_candidate_functions", []) or []))
    out["affected_functions"] = out["edit_required_functions"] + out["edit_candidate_functions"]
    out["editable_functions"] = [
        {
            "function": x.get("function"),
            "file": x.get("file"),
            "edit_scope": x.get("edit_scope"),
            "tier": x.get("tier"),
        }
        for x in out["affected_functions"]
    ]
    out["edit_scope_counts"] = {
        "edit_required": len(out["edit_required_functions"]),
        "edit_candidate": len(out["edit_candidate_functions"]),
        "context_only": len(out.get("context_only_functions", []) or []),
    }
    out["oot_compile_diagnostic_filter"] = {
        "enabled": True,
        "selected_functions": sorted(selected),
        "applied": True,
        "reason": "OOT old-source/new-kernel compile diagnostics mapped to driver functions",
    }
    return out


def _init_stage_results() -> list[dict[str, Any]]:
    return [{"stage": s, "status": "skipped", "reason": "not_run"} for s in _STAGES]


def _set_stage(stage_results: list[dict[str, Any]], stage: str, status: str, reason: str, details: dict[str, Any] | None = None) -> None:
    for item in stage_results:
        if item.get("stage") == stage:
            item["status"] = status
            item["reason"] = reason
            if details:
                item["details"] = details
            return


def _write_stage_summary(run_dir: Path, stage_results: list[dict[str, Any]]) -> None:
    lines = []
    for item in stage_results:
        reason = item.get("reason", "")
        suffix = f" - {reason}" if reason else ""
        lines.append(f"{item.get('stage')}: {str(item.get('status', '')).upper()}{suffix}")
    validation_dir = run_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    write_text(validation_dir / "stage_summary.txt", "\n".join(lines) + ("\n" if lines else ""))
    for line in lines:
        print(f"[validation-summary] {line}")


def _select_candidate(patch_generation_result: dict[str, Any]) -> dict[str, Any] | None:
    candidates = patch_generation_result.get("patch_candidates", []) or []
    if not candidates:
        return None
    selected_id = patch_generation_result.get("selected_candidate_id") or candidates[0].get("candidate_id")
    for cand in candidates:
        if cand.get("candidate_id") == selected_id:
            return cand
    return candidates[0]


def _root_relative_patch(patch_text: str, module_path: str) -> str:
    prefix = module_path.strip("/") + "/"
    if not prefix.strip("/"):
        return patch_text
    out = []
    for line in (patch_text or "").splitlines():
        line = line.replace(f"a/{prefix}", "a/")
        line = line.replace(f"b/{prefix}", "b/")
        out.append(line)
    text = "\n".join(out)
    return text + ("\n" if patch_text.endswith("\n") or text else "")


def _strip_module_prefix(path: str, module_path: str) -> str:
    prefix = module_path.strip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


def _truncate_run(run: dict[str, Any], max_chars: int = 24000) -> dict[str, Any]:
    out = dict(run)
    for key in ("stdout", "stderr"):
        text = str(out.get(key, "") or "")
        if len(text) > max_chars:
            out[key] = text[-max_chars:]
    return out


def _extract_slice(text: str, start_line: int, end_line: int, pad: int = 20) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, start_line - pad)
    end = min(len(lines), end_line + pad)
    return "\n".join(f"{n:5d}: {lines[n - 1]}" for n in range(start, end + 1))


def _collect_oot_patched_slices(source_patched: Path, module_path: str, impact_graph: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for afn in impact_graph.get("affected_functions", []) or []:
        rel = str(afn.get("file", "") or "")
        if not rel:
            continue
        root_rel = _strip_module_prefix(rel, module_path)
        p = source_patched / root_rel
        if not p.exists():
            continue
        start = int(afn.get("start_line", 1) or 1)
        end = int(afn.get("end_line", start) or start)
        out.append(
            {
                "function": afn.get("function"),
                "file": rel,
                "slice": _extract_slice(read_text(p), start, end),
            }
        )
    return out


def run_oot_integrated_validation_agent(
    *,
    accessor: RepositoryAccessor,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    run_dir: Path,
    case_id: str,
    module_path: str,
    external_source_root: Path,
    module_ko: str,
    module_name: str,
    patch_generation_result: dict[str, Any],
    context_packet: dict[str, Any],
    impact_graph: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    dry_run: bool,
    jobs: int,
    enable_qemu: bool,
    qemu_timeout_sec: int,
    qemu_check_script: str | None,
    qemu_device_node: str,
    qemu_write_data: str,
    qemu_expect_data: str,
    make_extra_args: list[str] | None = None,
    keep_worktrees: bool = True,
) -> dict[str, Any]:
    runner = _load_oot_runner_module()
    stage_results = _init_stage_results()
    validation_dir = run_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)

    out: dict[str, Any] = {
        "case_id": case_id,
        "stage": "oot_integrated_patch_validation",
        "status": "unknown",
        "validation_mode": "out_of_tree_integrated_pipeline",
        "apply": {},
        "stage_patched_files": {},
        "compile": {},
        "test_generation": {
            "case_id": case_id,
            "stage": "system_test_generation",
            "parse_status": "skipped_for_oot_custom_qemu",
            "test_cases": [],
            "suite_commands": [],
            "notes": ["OOT integrated validation uses explicit qemu_check_script instead of LLM-generated suite commands."],
        },
        "suite_execution": {"success": True, "runs": [], "skipped": True, "reason": "oot_custom_qemu_path"},
        "qemu": {},
        "post_reasoning": {},
        "validation_stage_results": stage_results,
        "open_questions": [],
    }

    def finalize(status: str) -> dict[str, Any]:
        out["status"] = status
        out["validation_stage_results"] = stage_results
        _write_stage_summary(run_dir, stage_results)
        write_json(run_dir / "validation.json", out)
        return out

    cand = _select_candidate(patch_generation_result)
    if not cand:
        _set_stage(stage_results, "worktree_prepare", "skipped", "no_patch_candidate_available")
        out["open_questions"].append("no_patch_candidate_available")
        return finalize("skipped")

    patch_rel = cand.get("fixed_patch_path")
    if not patch_rel:
        _set_stage(stage_results, "worktree_prepare", "skipped", "selected_candidate_missing_fixed_patch_path")
        out["open_questions"].append("selected_candidate_missing_fixed_patch_path")
        return finalize("skipped")

    patch_path = run_dir / patch_rel
    if not patch_path.exists():
        _set_stage(stage_results, "worktree_prepare", "skipped", f"fixed_patch_missing:{patch_rel}")
        out["open_questions"].append(f"fixed_patch_missing:{patch_rel}")
        return finalize("skipped")

    if accessor.mode != "git" or not accessor.repo or not accessor.old_ref or not accessor.new_ref:
        _set_stage(stage_results, "worktree_prepare", "skipped", "oot_validation_requires_git_repo_refs")
        out["open_questions"].append("oot_validation_requires_git_repo_refs")
        return finalize("skipped")

    t0 = time.time()
    worktrees_dir = (run_dir / "worktrees").resolve()
    old_tree = worktrees_dir / "old"
    new_tree = worktrees_dir / "new"
    wt_old = runner.prepare_worktree(accessor.repo, accessor.old_ref, old_tree, keep_existing=True)
    wt_new = runner.prepare_worktree(accessor.repo, accessor.new_ref, new_tree, keep_existing=True)
    out["worktrees"] = {
        "old": old_tree.as_posix(),
        "new": new_tree.as_posix(),
        "old_prepare": _truncate_run(wt_old),
        "new_prepare": _truncate_run(wt_new),
    }
    if not wt_old.get("ok") or not wt_new.get("ok"):
        _set_stage(stage_results, "worktree_prepare", "failed", "failed_to_prepare_worktrees")
        return finalize("failed")
    _set_stage(stage_results, "worktree_prepare", "passed", "old/new kernel worktrees prepared")

    source_old = validation_dir / "oot_source_old"
    source_patched = validation_dir / "oot_source_patched"
    if source_old.exists():
        shutil.rmtree(source_old)
    if source_patched.exists():
        shutil.rmtree(source_patched)
    shutil.copytree(external_source_root, source_old)
    shutil.copytree(external_source_root, source_patched)

    root_patch = _root_relative_patch(read_text(patch_path), module_path)
    root_patch_path = validation_dir / "oot_root_relative_patch.diff"
    write_text(root_patch_path, root_patch)
    patch_apply = runner.apply_generated_patch(validation_dir, source_patched, root_patch, iteration=None)
    out["apply"] = {
        "patch_path": patch_path.as_posix(),
        "oot_root_relative_patch_path": root_patch_path.as_posix(),
        "result": _truncate_run(patch_apply),
        "touched_files": cand.get("touched_files", []),
    }
    if not patch_apply.get("ok"):
        _set_stage(stage_results, "patch_apply", "failed", patch_apply.get("reason", "patch_apply_failed"))
        return finalize("failed")
    _set_stage(stage_results, "patch_apply", "passed", "patch applied to OOT source root")

    out["stage_patched_files"] = {
        "copied_files": cand.get("touched_files", []),
        "missing_files": [],
        "count": len(cand.get("touched_files", [])),
        "note": "OOT validation applies patch in an external source root; files are not staged into kernel tree.",
    }
    _set_stage(stage_results, "stage_patched_files", "passed", "OOT source root patched")

    old_prep = runner.prepare_kernel(old_tree, jobs)
    new_prep = runner.prepare_kernel(new_tree, jobs)
    prep_ok = all(step.get("ok") for step in old_prep + new_prep)
    out["compile"]["prepare_steps"] = {
        "old": [_truncate_run(x) for x in old_prep],
        "new": [_truncate_run(x) for x in new_prep],
    }
    if prep_ok:
        _set_stage(stage_results, "compile_prepare", "passed", "old/new defconfig/olddefconfig/modules_prepare succeeded")
    else:
        _set_stage(stage_results, "compile_prepare", "failed", "kernel_prepare_failed")
        return finalize("failed")

    old_build = runner.build_external(old_tree, source_old, jobs, make_extra_args or [])
    new_pre_build = runner.build_external(new_tree, source_old, jobs, make_extra_args or [])
    post_build = runner.build_external(new_tree, source_patched, jobs, make_extra_args or [])
    out["compile"].update(
        {
            "success": bool(post_build.get("ok")),
            "compiled_dirs": [source_patched.as_posix()],
            "runs": [_truncate_run(post_build)],
            "error": "" if post_build.get("ok") else "oot_module_compile_failed",
            "oot_builds": {
                "old_driver_on_old_kernel": _truncate_run(old_build),
                "old_driver_on_new_kernel_pre_patch": _truncate_run(new_pre_build),
                "patched_driver_on_new_kernel": _truncate_run(post_build),
            },
            "oot_expected_transition": {
                "old_build_passes": bool(old_build.get("ok")),
                "old_source_fails_on_new_kernel": not bool(new_pre_build.get("ok")),
                "patched_build_passes_new_kernel": bool(post_build.get("ok")),
            },
        }
    )
    if not post_build.get("ok"):
        _set_stage(stage_results, "compile_modules", "failed", "patched OOT module failed to compile")
    else:
        _set_stage(stage_results, "compile_modules", "passed", f"compiled OOT module: {module_ko}")

    _set_stage(stage_results, "test_generation", "skipped", "OOT integrated validation uses explicit qemu_check_script")
    _set_stage(stage_results, "suite_execution", "skipped", "OOT integrated validation uses explicit qemu_check_script")

    qemu_run = None
    if enable_qemu and post_build.get("ok"):
        ko = source_patched / module_ko
        qemu_run = runner.run_qemu_smoke(
            run_dir,
            new_tree,
            ko,
            jobs,
            qemu_timeout_sec,
            module_name=module_name,
            device_node=qemu_device_node,
            write_data=qemu_write_data,
            expect_data=qemu_expect_data,
            qemu_check_script=Path(qemu_check_script) if qemu_check_script else None,
        )
        qemu_detail = qemu_run.get("qemu") if isinstance(qemu_run, dict) else None
        qemu_runs = [_truncate_run(qemu_detail)] if isinstance(qemu_detail, dict) else [_truncate_run(qemu_run)]
        out["qemu"] = {
            "enabled": True,
            "detected_qemu_runs": 1,
            "runs": qemu_runs,
            "success": bool(qemu_run.get("ok")),
            "reason": qemu_run.get("reason"),
            "note": "OOT custom QEMU smoke check",
        }
        if qemu_run.get("ok"):
            _set_stage(stage_results, "qemu_execution", "passed", "runs=1")
        else:
            _set_stage(stage_results, "qemu_execution", "failed", qemu_run.get("reason", "qemu_run_failed"))
    elif enable_qemu:
        out["qemu"] = {
            "enabled": True,
            "detected_qemu_runs": 0,
            "runs": [],
            "success": False,
            "reason": "compile_failed_or_module_missing",
        }
        _set_stage(stage_results, "qemu_execution", "skipped", "compile failed or module missing")
    else:
        out["qemu"] = {
            "enabled": False,
            "detected_qemu_runs": 0,
            "runs": [],
            "success": False,
            "reason": "qemu disabled by flag",
        }
        _set_stage(stage_results, "qemu_execution", "skipped", "qemu disabled by flag")

    patched_slices = _collect_oot_patched_slices(source_patched, module_path, impact_graph)
    post_reasoning = run_post_reasoner_agent(
        llm_client=llm_client,
        llm_calls_dir=llm_calls_dir,
        case_id=case_id,
        module_path=module_path,
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
        patched_function_slices=patched_slices,
        compile_result=out["compile"],
        test_generation_result=out["test_generation"],
        dry_run=dry_run,
    )
    out["post_reasoning"] = post_reasoning
    parse_ok = str(post_reasoning.get("parse_status", "")) == "ok"
    verdict = str(post_reasoning.get("overall_verdict", "UNKNOWN")).upper()
    if parse_ok and verdict == "PASS":
        _set_stage(stage_results, "post_reasoning", "passed", "overall_verdict=PASS")
    elif parse_ok:
        _set_stage(stage_results, "post_reasoning", "failed", f"overall_verdict={verdict}")
    else:
        _set_stage(stage_results, "post_reasoning", "failed", post_reasoning.get("error") or f"parse_status={post_reasoning.get('parse_status')}")

    ok = bool(
        old_build.get("ok")
        and not new_pre_build.get("ok")
        and post_build.get("ok")
        and (not enable_qemu or (qemu_run and qemu_run.get("ok")))
        and parse_ok
        and verdict == "PASS"
    )
    out["timings"] = {"total_sec": round(time.time() - t0, 3)}
    if not old_build.get("ok"):
        out["open_questions"].append("old_oot_source_did_not_build_on_old_kernel")
    if new_pre_build.get("ok"):
        out["open_questions"].append("old_oot_source_already_builds_on_new_kernel")
    if not post_build.get("ok"):
        out["open_questions"].append("patched_oot_source_did_not_build_on_new_kernel")
    if enable_qemu and not (qemu_run and qemu_run.get("ok")):
        out["open_questions"].append("oot_qemu_smoke_not_success")
    if not parse_ok or verdict != "PASS":
        out["open_questions"].append("post_reasoning_not_pass")

    if not keep_worktrees:
        runner.run(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(old_tree)], timeout=300)
        runner.run(["git", "-C", str(accessor.repo), "worktree", "remove", "-f", str(new_tree)], timeout=300)

    return finalize("ok" if ok else "failed")
