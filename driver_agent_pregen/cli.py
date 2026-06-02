from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .context_packet import run_context_packet_builder
from .definition_locator import run_definition_locator
from .impact_graph_builder import run_impact_graph_builder
from .kernel_diff_builder import run_kernel_diff_builder
from .llm_client import LLMClient, LLMConfig
from .module_source_loader import run_external_module_source_loader, run_module_source_loader
from .obligation_builder import run_obligation_builder
from .oot_validation import (
    collect_oot_pre_patch_build_diagnostics,
    export_oot_source_snapshot,
    filter_scan_payload_by_oot_compile_diagnostics,
    filter_impact_graph_by_oot_compile_diagnostics,
    run_oot_integrated_validation_agent,
)
from .patch_generation import run_patch_generation_agent
from .reasoner_agent import run_pre_reasoner_agent
from .report import run_final_report_builder
from .repo_utils import RepositoryAccessor
from .spec_agent import run_pre_spec_agent
from .symbol_scanner import run_symbol_scanner
from .utils import ensure_dir, now_timestamp, read_text, slugify, write_json, write_text
from .validation import run_validation_agent
from .validation_summary import build_validation_summary_v2


def _requires_effective_migration_edit(obligations: dict[str, Any]) -> bool:
    for item in obligations.get("obligations", []) or []:
        if item.get("hard_checks"):
            return True
        if item.get("replacement_candidates") and str(item.get("semantic_strength", "")).lower() == "strong":
            return True
    for item in obligations.get("semantic_obligations", []) or []:
        if str(item.get("strength", "")).lower() == "strong" and item.get("replacement_candidates"):
            return True
    return False


def build_parser() -> argparse.ArgumentParser:
    default_config = str(Path(__file__).resolve().parents[1] / "config.ini")
    p = argparse.ArgumentParser(description="Driver migration pre-generation pipeline")
    p.add_argument("--repo", type=str, default=None, help="path to git repo (preferred)")
    p.add_argument("--old-ref", type=str, default=None)
    p.add_argument("--new-ref", type=str, default=None)
    p.add_argument("--old-tree", type=str, default=None, help="fallback old source tree")
    p.add_argument("--new-tree", type=str, default=None, help="fallback new source tree")
    p.add_argument("--module-path", type=str, required=False)
    p.add_argument(
        "--kbuild-target",
        type=str,
        default=None,
        help=(
            "optional in-tree Kbuild module target under --module-path, e.g. iio_dummy.o. "
            "When set, source loading and validation are scoped to that target instead of the whole directory."
        ),
    )
    p.add_argument("--out-dir", type=str, required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--dry-run", action="store_true", help="skip real LLM calls, keep prompt/input outputs")
    p.add_argument("--enable-llm", action="store_true", help="enable real LLM calls (default off)")
    p.add_argument("--llm-provider", type=str, default="openai")
    p.add_argument("--llm-model", type=str, default="gpt-4.1")
    p.add_argument("--llm-api-key", type=str, default=None, help="override API key directly")
    p.add_argument("--llm-base-url", type=str, default=None, help="override LLM API base URL")
    p.add_argument("--config-ini", type=str, default=default_config, help="config file path for provider keys")
    p.add_argument("--llm-timeout-sec", type=int, default=300, help="LLM request timeout in seconds")
    p.add_argument(
        "--llm-max-tokens",
        type=int,
        default=10000,
        help="maximum output tokens per LLM call; keep bounded to avoid oversized JSON/spec outputs",
    )
    p.add_argument("--llm-max-retries", type=int, default=2, help="LLM request max retries")
    p.add_argument("--llm-retry-backoff-sec", type=int, default=3, help="LLM retry backoff seconds")
    p.add_argument(
        "--include-kernel-commit-info",
        action="store_true",
        help=(
            "include sanitized kernel commit subjects/messages/diff excerpts in intermediate evidence "
            "(default: disabled; paper-facing workflow uses definitions/diffs only)"
        ),
    )

    p.add_argument("--patch-candidates", type=int, default=1, help="number of patch candidates to request from LLM")
    p.add_argument(
        "--max-patch-iterations",
        type=int,
        default=1,
        help=(
            "max in-tree LLM patch/validation repair iterations. "
            "Each repair emits a complete replacement patch against the original old source."
        ),
    )
    p.add_argument(
        "--patch-prompt-strength",
        type=str,
        choices=["normal", "strict"],
        default="strict",
        help="patch-generation prompt strictness (strict forbids comment-only edits and requires evidence-grounded code edits)",
    )
    p.add_argument(
        "--patch-source-mode",
        type=str,
        choices=["full_module", "focused"],
        default="focused",
        help=(
            "source payload mode for patch generation: "
            "focused (default: edit-scope function slices plus read-only context) or "
            "full_module (all old module sources; fallback/debug mode)"
        ),
    )
    p.set_defaults(patch_source_with_lineno=True)
    p.add_argument(
        "--patch-source-with-lineno",
        dest="patch_source_with_lineno",
        action="store_true",
        help="include line-number prefixes (e.g., L123:) in patch-generation source snippets",
    )
    p.add_argument(
        "--patch-source-no-lineno",
        dest="patch_source_with_lineno",
        action="store_false",
        help="disable line-number prefixes in patch-generation source snippets",
    )
    p.add_argument(
        "--patch-generation-mode",
        type=str,
        choices=["agent", "baseline_source_only", "baseline_kernel_evidence"],
        default="agent",
        help=(
            "patch generation payload mode: agent uses Driver-Agent obligations/spec/reasoning; "
            "baseline_source_only uses only old target module source; "
            "baseline_kernel_evidence adds compact kernel-side evidence but no affected graph/spec/reasoning"
        ),
    )
    p.add_argument(
        "--affected-func-signal-mode",
        type=str,
        choices=["kernel_only", "kernel_plus_other_drivers"],
        default="kernel_only",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--validation-patch-file",
        type=str,
        default=None,
        help="optional unified diff patch to override LLM patch candidates during validation",
    )
    p.set_defaults(enable_qemu=True)
    p.add_argument(
        "--enable-qemu",
        dest="enable_qemu",
        action="store_true",
        help="enable qemu/kunit runtime command execution if present in generated suite commands (default: enabled)",
    )
    p.add_argument(
        "--disable-qemu",
        dest="enable_qemu",
        action="store_false",
        help="disable qemu/kunit runtime command execution",
    )
    p.add_argument("--qemu-timeout-sec", type=int, default=3600, help="timeout seconds for suite/qemu commands")
    p.add_argument(
        "--kernel-config-target",
        type=str,
        choices=["defconfig", "allmodconfig", "allyesconfig"],
        default="defconfig",
        help=(
            "kernel config target for validation compile. Use allmodconfig for P0/hard-break "
            "experiments to avoid false passes from disabled drivers."
        ),
    )
    p.add_argument(
        "--semantic-obligation-table",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--skip-system-test-generation",
        action="store_true",
        help="skip LLM system-test generation and suite/qemu execution; keep patch apply/compile/post-reasoning",
    )
    p.add_argument(
        "--enable-host-suite",
        action="store_true",
        help=(
            "execute LLM-generated suite commands directly on the host. "
            "Default is disabled because generated driver tests may run modprobe/mount/suspend."
        ),
    )
    p.add_argument("--cleanup-worktrees", action="store_true", help="remove temporary worktrees after validation")

    oot = p.add_argument_group("out-of-tree driver path")
    oot.add_argument(
        "--oot-pipeline-mode",
        type=str,
        choices=["parallel", "integrated"],
        default="parallel",
        help=(
            "parallel runs the lightweight OOT patch/build/QEMU path; integrated "
            "routes OOT source through symbol/diff/impact/spec/reasoning before OOT validation"
        ),
    )
    oot.add_argument("--oot-repo-url", type=str, default=None, help="real OOT git repository URL; enables the OOT parallel path")
    oot.add_argument("--oot-repo-ref", type=str, default=None, help="commit/ref to export from the OOT repository")
    oot.add_argument("--oot-source-subdir", type=str, default=None, help="source subdirectory inside the OOT repository")
    oot.add_argument("--oot-module-ko", type=str, default="oot_class_demo.ko", help="built .ko filename for QEMU smoke")
    oot.add_argument("--oot-module-name", type=str, default="oot_class_demo", help="module name for rmmod in QEMU smoke")
    oot.add_argument("--oot-device-node", type=str, default="/dev/oot_class_demo", help="device node expected after insmod")
    oot.add_argument("--oot-qemu-write-data", type=str, default="qemu-ok", help="data written by OOT QEMU smoke")
    oot.add_argument("--oot-qemu-expect-data", type=str, default="qemu-ok", help="data expected by OOT QEMU smoke")
    oot.add_argument("--oot-qemu-check-script", type=str, default=None, help="custom OOT QEMU init check script")
    oot.add_argument(
        "--oot-qemu-test-mode",
        type=str,
        choices=["auto", "custom", "llm", "default"],
        default="auto",
        help=(
            "OOT integrated QEMU script selection: custom uses --oot-qemu-check-script, "
            "llm generates an in-guest script, default uses deterministic load/interface smoke, "
            "auto prefers custom, then LLM when enabled, then default"
        ),
    )
    oot.add_argument(
        "--oot-qemu-test-level",
        type=str,
        choices=["load_unload", "interface_smoke", "semantic_scenario"],
        default="interface_smoke",
        help="requested depth for generated OOT in-QEMU validation script",
    )
    oot.add_argument("--oot-max-patch-iterations", type=int, default=1, help="max OOT LLM patch/build repair iterations")
    oot.add_argument(
        "--oot-make-extra-arg",
        action="append",
        default=[],
        help="extra make argument for OOT module builds, e.g. KCFLAGS=-Wno-error=type-limits",
    )
    oot.add_argument(
        "--oot-kernel-config",
        action="append",
        default=[],
        help=(
            "kernel config option required for OOT build/QEMU validation, "
            "e.g. MEDIA_SUPPORT=m or CONFIG_VIDEO_DEV=m"
        ),
    )
    oot.add_argument(
        "--oot-kernel-module-dir",
        action="append",
        default=[],
        help=(
            "in-tree kernel module directory to build/copy into the OOT QEMU initramfs, "
            "e.g. drivers/media/v4l2-core"
        ),
    )
    oot.add_argument(
        "--oot-kernel-module",
        action="append",
        default=[],
        help=(
            "specific in-tree .ko target to build/copy into the OOT QEMU initramfs, "
            "e.g. drivers/media/v4l2-core/videodev.ko"
        ),
    )
    return p


def _run_oot_parallel_path(args: argparse.Namespace) -> tuple[Path, bool]:
    if not args.oot_repo_ref or not args.oot_source_subdir:
        raise SystemExit("--oot-repo-url requires --oot-repo-ref and --oot-source-subdir")
    if not args.repo or not args.old_ref or not args.new_ref:
        raise SystemExit("OOT path requires --repo, --old-ref, and --new-ref")

    script = Path(__file__).resolve().parents[1] / "tools" / "run_oot_qemu_case.py"
    cmd = [
        sys.executable,
        str(script),
        "--repo",
        args.repo,
        "--old-ref",
        args.old_ref,
        "--new-ref",
        args.new_ref,
        "--external-repo-url",
        args.oot_repo_url,
        "--external-repo-ref",
        args.oot_repo_ref,
        "--external-source-subdir",
        args.oot_source_subdir,
        "--module-ko",
        args.oot_module_ko,
        "--module-name",
        args.oot_module_name,
        "--device-node",
        args.oot_device_node,
        "--qemu-write-data",
        args.oot_qemu_write_data,
        "--qemu-expect-data",
        args.oot_qemu_expect_data,
        "--out-dir",
        args.out_dir,
        "--jobs",
        str(args.jobs),
        "--llm-provider",
        args.llm_provider,
        "--llm-model",
        args.llm_model,
        "--config-ini",
        args.config_ini,
        "--llm-timeout-sec",
        str(args.llm_timeout_sec),
        "--llm-max-tokens",
        str(args.llm_max_tokens),
        "--llm-max-retries",
        str(args.llm_max_retries),
        "--max-patch-iterations",
        str(args.oot_max_patch_iterations),
    ]
    if args.enable_llm:
        cmd.append("--enable-llm")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.enable_qemu:
        cmd.extend(["--enable-qemu", "--qemu-timeout-sec", str(args.qemu_timeout_sec)])
    if args.oot_qemu_check_script:
        cmd.extend(["--qemu-check-script", args.oot_qemu_check_script])
    for extra in args.oot_make_extra_arg or []:
        cmd.extend(["--make-extra-arg", extra])
    for opt in args.oot_kernel_config or []:
        cmd.extend(["--kernel-config", opt])
    for rel in args.oot_kernel_module_dir or []:
        cmd.extend(["--kernel-module-dir", rel])
    for target in args.oot_kernel_module or []:
        cmd.extend(["--kernel-module", target])

    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(proc.stdout, end="")

    run_dir = _extract_oot_completed_run_dir(proc.stdout)
    if run_dir is None:
        case_slug = args.oot_repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
        run_dir = (Path(args.out_dir) / f"{case_slug}__{args.old_ref}__{args.new_ref}").resolve()
    return run_dir, proc.returncode == 0


def _extract_oot_completed_run_dir(output: str) -> Path | None:
    for line in output.splitlines():
        if line.startswith("[oot-case] completed: "):
            return Path(line.split(": ", 1)[1].strip()).resolve()
    return None


def _extract_touched_files_from_patch(patch_text: str) -> list[str]:
    out: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[len("+++ b/") :].strip()
        if rel == "/dev/null":
            continue
        if rel and rel not in out:
            out.append(rel)
    return out


def _compact_text(text: str, max_chars: int = 16000) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n...[truncated]"


def _validation_stage(validation: dict[str, Any], name: str) -> dict[str, Any]:
    for item in validation.get("validation_stage_results", []) or []:
        if item.get("stage") == name or item.get("name") == name:
            return item
    return {}


def _validation_repair_reason(validation: dict[str, Any]) -> str | None:
    apply_stage = _validation_stage(validation, "patch_apply")
    if str(apply_stage.get("status", "")).lower() == "failed":
        return "patch_apply_failed"
    compile_stage = _validation_stage(validation, "compile_modules")
    if str(compile_stage.get("status", "")).lower() == "failed":
        return "compile_failed"
    compile_prepare = _validation_stage(validation, "compile_prepare")
    if str(compile_prepare.get("status", "")).lower() == "failed":
        return "kernel_prepare_failed"
    if not bool((validation.get("compile") or {}).get("success")):
        return "compile_not_success"
    return None


def _validation_feedback_for_repair(validation: dict[str, Any], iteration: int, reason: str) -> dict[str, Any]:
    compile_payload = validation.get("compile") or {}
    failed_runs: list[dict[str, Any]] = []
    for run in compile_payload.get("runs", []) or []:
        if run.get("ok"):
            continue
        failed_runs.append(
            {
                "cmd": run.get("cmd", ""),
                "returncode": run.get("returncode"),
                "stderr": _compact_text(run.get("stderr", ""), max_chars=18000),
                "stdout": _compact_text(run.get("stdout", ""), max_chars=6000),
            }
        )
    prepare_failures: list[dict[str, Any]] = []
    for step in compile_payload.get("prepare_steps", []) or []:
        if step.get("ok"):
            continue
        prepare_failures.append(
            {
                "cmd": step.get("cmd", ""),
                "returncode": step.get("returncode"),
                "stderr": _compact_text(step.get("stderr", ""), max_chars=12000),
                "stdout": _compact_text(step.get("stdout", ""), max_chars=4000),
            }
        )
    apply_payload = validation.get("apply") or {}
    apply_result = apply_payload.get("result") or {}
    return {
        "iteration": iteration,
        "repair_reason": reason,
        "status": validation.get("status"),
        "stage_results": validation.get("validation_stage_results", []),
        "apply": {
            "no_op_candidate": apply_payload.get("no_op_candidate"),
            "patch_path": apply_payload.get("patch_path"),
            "touched_files": apply_payload.get("touched_files", []),
            "result": {
                "cmd": apply_result.get("cmd", ""),
                "returncode": apply_result.get("returncode"),
                "stderr": _compact_text(apply_result.get("stderr", ""), max_chars=12000),
                "stdout": _compact_text(apply_result.get("stdout", ""), max_chars=4000),
            },
        },
        "compile": {
            "success": bool(compile_payload.get("success")),
            "error": compile_payload.get("error", ""),
            "compiled_dirs": compile_payload.get("compiled_dirs", []),
            "kbuild_target": compile_payload.get("kbuild_target"),
            "compiled_goals": compile_payload.get("compiled_goals", []),
            "prepare_failures": prepare_failures,
            "failed_runs": failed_runs,
            "autofix": compile_payload.get("autofix", {}),
        },
        "open_questions": validation.get("open_questions", []),
    }


def _write_patch_iteration_snapshot(
    *,
    run_dir: Path,
    iteration: int,
    patch_generation: dict[str, Any],
    validation: dict[str, Any],
    repair_reason: str | None,
) -> dict[str, Any]:
    iter_dir = run_dir / "patch_iterations" / f"iter_{iteration:02d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    write_json(iter_dir / "patch_generation.json", patch_generation)
    write_json(iter_dir / "validation.json", validation)
    validation_artifacts = run_dir / "validation"
    if validation_artifacts.exists():
        shutil.copytree(validation_artifacts, iter_dir / "validation_artifacts", dirs_exist_ok=True)
    post_reasoner_artifacts = run_dir / "llm_calls" / "04_post_reasoner"
    if post_reasoner_artifacts.exists():
        shutil.copytree(post_reasoner_artifacts, iter_dir / "llm_calls" / "04_post_reasoner", dirs_exist_ok=True)
    selected = {}
    for cand in patch_generation.get("patch_candidates", []) or []:
        if cand.get("candidate_id") == patch_generation.get("selected_candidate_id"):
            selected = cand
            break
    if not selected and patch_generation.get("patch_candidates"):
        selected = patch_generation["patch_candidates"][0]
    record = {
        "iteration": iteration,
        "patch_stage": patch_generation.get("stage"),
        "patch_generation_mode": patch_generation.get("patch_generation_mode"),
        "selected_candidate_id": patch_generation.get("selected_candidate_id"),
        "no_op_candidate": bool(selected.get("no_op_candidate")) if selected else None,
        "touched_files": selected.get("touched_files", []) if selected else [],
        "validation_status": validation.get("status"),
        "compile_success": bool((validation.get("compile") or {}).get("success")),
        "repair_reason": repair_reason,
        "will_repair": repair_reason is not None,
    }
    write_json(iter_dir / "iteration_result.json", record)
    return record


def _override_patch_generation_with_file(
    *,
    run_dir: Path,
    patch_generation: dict[str, Any],
    patch_file: Path,
) -> dict[str, Any]:
    if not patch_file.exists() or not patch_file.is_file():
        raise FileNotFoundError(f"validation patch file not found: {patch_file}")

    patch_text = read_text(patch_file)
    if not patch_text.strip():
        raise ValueError(f"validation patch file is empty: {patch_file}")

    candidate_id = "USER-CORRECT-001"
    cdir = run_dir / "patch_runs" / "user_correct_001"
    cdir.mkdir(parents=True, exist_ok=True)

    raw_path = cdir / "01_raw_patch.diff"
    fixed_path = cdir / "02_fixed_patch.diff"
    write_text(raw_path, patch_text)
    write_text(fixed_path, patch_text)

    touched = _extract_touched_files_from_patch(patch_text)
    out = dict(patch_generation)
    out["patch_candidates"] = [
        {
            "candidate_id": candidate_id,
            "strategy": "user_supplied_correct_patch",
            "rationale": f"override from {patch_file.as_posix()}",
            "evidence": ["user supplied kernel module patch diff"],
            "open_questions": [],
            "touched_files": touched,
            "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
            "fixed_patch_path": fixed_path.relative_to(run_dir).as_posix(),
            "line_fix_ok": True,
            "line_fix_messages": ["override_patch_no_line_fix_required"],
        }
    ]
    out["selected_candidate_id"] = candidate_id
    out["manual_patch_override"] = True
    out["manual_patch_override_source"] = patch_file.as_posix()

    notes = list(out.get("module_level_notes", []))
    notes.append(f"manual patch override from {patch_file.as_posix()}")
    out["module_level_notes"] = notes

    write_json(run_dir / "patch_generation.json", out)
    return out


def _make_case_id(
    module_path: str,
    old_ref: str | None,
    new_ref: str | None,
    kbuild_target: str | None = None,
) -> str:
    parts = [module_path.replace("/", "_")]
    if kbuild_target:
        parts.append(kbuild_target.replace("/", "_").replace(".", "_"))
    if old_ref:
        parts.append(old_ref)
    if new_ref:
        parts.append(new_ref)
    return slugify("__".join(parts))


def _oot_repo_slug(repo_url: str) -> str:
    return repo_url.rstrip("/").removesuffix(".git").split("/")[-1] or "oot_driver"


def _oot_synthetic_module_path(args: argparse.Namespace) -> str:
    repo_slug = _oot_repo_slug(args.oot_repo_url or "oot_driver")
    subdir = (args.oot_source_subdir or "source").strip("/")
    return f"oot/{repo_slug}/{subdir}"


def run_pipeline(args: argparse.Namespace) -> tuple[Path, bool]:
    if not args.module_path:
        if args.oot_repo_url and args.oot_pipeline_mode == "integrated":
            args.module_path = _oot_synthetic_module_path(args)
        else:
            raise SystemExit("--module-path is required for the in-tree driver pipeline")
    oot_integrated = bool(args.oot_repo_url and args.oot_pipeline_mode == "integrated")
    if args.kbuild_target and oot_integrated:
        raise SystemExit("--kbuild-target is only supported for the in-tree driver pipeline")

    accessor = RepositoryAccessor.from_args(
        repo=args.repo,
        old_ref=args.old_ref,
        new_ref=args.new_ref,
        old_tree=args.old_tree,
        new_tree=args.new_tree,
    )
    accessor.validate()

    case_id = _make_case_id(args.module_path, args.old_ref, args.new_ref, args.kbuild_target)
    ts = now_timestamp()
    run_dir = (Path(args.out_dir) / case_id / ts).resolve()
    extracted_dir = run_dir / "extracted"
    llm_calls_dir = run_dir / "llm_calls"
    reports_dir = run_dir / "reports"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    llm_calls_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    metadata: dict[str, Any] = {
        "case_id": case_id,
        "timestamp": ts,
        "module_path": args.module_path,
        "case_scope": "kbuild_target" if args.kbuild_target else "directory",
        "kbuild_target": args.kbuild_target,
        "mode": accessor.describe(),
        "jobs": args.jobs,
        "dry_run": args.dry_run,
        "llm_enabled": args.enable_llm,
        "llm_provider": args.llm_provider,
        "llm_model": args.llm_model,
        "llm_max_tokens": args.llm_max_tokens,
        "affected_func_signal_mode": args.affected_func_signal_mode,
        "include_kernel_commit_info": bool(args.include_kernel_commit_info),
        "commit_info_policy": (
            "disabled_by_default; enable only for private/debug ablations with sanitized kernel-only evidence"
            if not args.include_kernel_commit_info
            else "enabled_explicitly; kernel-side commit info is collected with driver paths excluded"
        ),
        "patch_prompt_strength": args.patch_prompt_strength,
        "patch_source_mode": args.patch_source_mode,
        "patch_source_with_lineno": bool(args.patch_source_with_lineno),
        "patch_generation_mode": args.patch_generation_mode,
        "max_patch_iterations": max(1, int(args.max_patch_iterations or 1)),
        "kernel_config_target": args.kernel_config_target,
        "oot_pipeline_mode": args.oot_pipeline_mode if args.oot_repo_url else None,
        "oot_repo_url": args.oot_repo_url,
        "oot_repo_ref": args.oot_repo_ref,
        "oot_source_subdir": args.oot_source_subdir,
        "semantic_obligation_table": args.semantic_obligation_table,
        "stages": [],
    }
    write_json(run_dir / "metadata.json", metadata)

    external_source_root: Path | None = None
    external_source_info: dict[str, Any] | None = None
    if oot_integrated:
        if not args.oot_repo_ref or not args.oot_source_subdir:
            raise SystemExit("--oot-pipeline-mode=integrated requires --oot-repo-ref and --oot-source-subdir")
        external_source_root = run_dir / "oot_source_snapshot"
        external_source_info = export_oot_source_snapshot(
            repo_url=args.oot_repo_url,
            ref=args.oot_repo_ref,
            source_subdir=args.oot_source_subdir,
            dest=external_source_root,
            cache_root=Path(__file__).resolve().parents[1] / "external_repos",
        )
        metadata["oot_external_source"] = {
            k: v for k, v in external_source_info.items() if k != "fetch"
        }
        metadata["input_boundary"] = (
            "old OOT target driver source + old/new kernel-side definitions/diffs; "
            "no target new driver source is used"
        )
        write_json(run_dir / "metadata.json", metadata)

    allow_driver_evidence = os.environ.get("DRIVER_AGENT_ALLOW_DRIVER_EVIDENCE") == "1"
    if args.affected_func_signal_mode != "kernel_only" and not allow_driver_evidence:
        raise SystemExit(
            "--affected-func-signal-mode=kernel_plus_other_drivers is disabled by policy; "
            "the public workflow uses only target old-source plus old/new kernel-side definitions."
        )
    if args.semantic_obligation_table and not allow_driver_evidence:
        raise SystemExit(
            "--semantic-obligation-table is disabled by policy; "
            "semantic/collateral driver evidence is not part of the default workflow."
        )

    if int(args.jobs or 1) >= 8:
        # The definition locator can issue multiple git-grep subprocesses in
        # parallel. Keep each grep single-threaded unless the caller explicitly
        # overrides it to avoid N workers * M grep threads oversubscription.
        os.environ.setdefault("DRIVER_AGENT_GIT_GREP_THREADS", "1")
        metadata["git_grep_threads"] = os.environ.get("DRIVER_AGENT_GIT_GREP_THREADS")
        write_json(run_dir / "metadata.json", metadata)

    pipeline_t0 = time.perf_counter()
    semantic_obligation_table = (
        args.semantic_obligation_table
        if args.affected_func_signal_mode == "kernel_plus_other_drivers"
        else None
    )
    if args.semantic_obligation_table and semantic_obligation_table is None:
        metadata["semantic_obligation_table_ignored"] = {
            "path": args.semantic_obligation_table,
            "reason": "affected_func_signal_mode=kernel_only uses kernel-side evidence only",
        }
        metadata["semantic_obligation_table"] = None
        write_json(run_dir / "metadata.json", metadata)

    def stage(name: str, fn):
        t0 = time.perf_counter()
        started_at = dt.datetime.now().isoformat(timespec="seconds")
        try:
            out = fn()
            duration_sec = round(time.perf_counter() - t0, 3)
            metadata["stages"].append(
                {
                    "name": name,
                    "status": "ok",
                    "started_at": started_at,
                    "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "duration_sec": duration_sec,
                }
            )
            write_json(run_dir / "metadata.json", metadata)
            return out
        except Exception as exc:
            duration_sec = round(time.perf_counter() - t0, 3)
            metadata["stages"].append(
                {
                    "name": name,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "duration_sec": duration_sec,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            write_json(run_dir / "metadata.json", metadata)
            raise

    if oot_integrated:
        assert external_source_root is not None
        module_result = stage(
            "01_module_source_loader",
            lambda: run_external_module_source_loader(
                source_root=external_source_root,
                module_path=args.module_path,
                extracted_dir=extracted_dir,
                old_ref=args.old_ref,
                new_ref=args.new_ref,
                new_path_exists=False,
                note="out-of-tree source snapshot loaded under a synthetic module path for full Driver-Agent analysis",
            ),
        )
    else:
        module_result = stage(
            "01_module_source_loader",
            lambda: run_module_source_loader(
                accessor,
                module_path=args.module_path,
                extracted_dir=extracted_dir,
                kbuild_target=args.kbuild_target,
            ),
        )
    metadata["case_scope"] = module_result.case_scope
    metadata["kbuild_target_resolution"] = module_result.kbuild_target_resolution
    write_json(run_dir / "metadata.json", metadata)

    scan_payload = stage(
        "02_old_driver_symbol_scanner",
        lambda: run_symbol_scanner(module_result, extracted_dir),
    )
    oot_pre_patch_build_diagnostics: dict[str, Any] | None = None
    if oot_integrated:
        assert external_source_root is not None
        oot_pre_patch_build_diagnostics = stage(
            "02b_oot_pre_patch_build_diagnostics",
            lambda: collect_oot_pre_patch_build_diagnostics(
                accessor=accessor,
                run_dir=run_dir,
                external_source_root=external_source_root,
                jobs=args.jobs,
                make_extra_args=args.oot_make_extra_arg or [],
                kernel_config_target=args.kernel_config_target,
                kernel_config_options=args.oot_kernel_config or [],
            ),
        )
        write_json(extracted_dir / "oot_pre_patch_build_diagnostics.json", oot_pre_patch_build_diagnostics)
        scan_payload = stage(
            "02c_oot_compile_diagnostic_symbol_filter",
            lambda: filter_scan_payload_by_oot_compile_diagnostics(
                scan_payload=scan_payload,
                diagnostics=oot_pre_patch_build_diagnostics or {},
                extracted_dir=extracted_dir,
                module_path=args.module_path,
            ),
        )

    baseline_source_only = str(args.patch_generation_mode or "").strip().lower() == "baseline_source_only"

    if baseline_source_only:
        metadata["baseline_source_only_policy"] = (
            "kernel definition location/diff, impact graph, obligations, and pre-patch LLM analysis "
            "are skipped; the patch LLM receives only old target module source"
        )
        write_json(run_dir / "metadata.json", metadata)

        def _baseline_empty_locations() -> dict[str, Any]:
            payload = {
                "items": [],
                "skipped": True,
                "skip_reason": "baseline_source_only receives no kernel-side evidence",
            }
            write_json(extracted_dir / "kernel_definition_locations.json", payload)
            return payload

        def _baseline_empty_kernel_diffs() -> dict[str, Any]:
            payload = {
                "items": [],
                "affected_func_signal_mode": args.affected_func_signal_mode,
                "commit_exclude_prefixes": [args.module_path],
                "skipped": True,
                "skip_reason": "baseline_source_only receives no kernel-side evidence",
            }
            write_json(extracted_dir / "kernel_definition_diffs.json", payload)
            write_text(extracted_dir / "kernel_relevant_diff.patch", "")
            return payload

        def _baseline_empty_impact_graph() -> dict[str, Any]:
            payload = {
                "module_path": args.module_path,
                "affected_functions": [],
                "edit_required_functions": [],
                "edit_candidate_functions": [],
                "context_only_functions": [],
                "context_functions": [],
                "editable_functions": [],
                "edges": [],
                "affected_tier_policy": {
                    "skipped": True,
                    "skip_reason": "baseline_source_only does not use affected-function analysis",
                },
                "skipped": True,
                "skip_reason": "baseline_source_only does not use affected-function analysis",
            }
            write_json(extracted_dir / "impact_graph.json", payload)
            write_json(extracted_dir / "callback_graph.json", {"roots": [], "reachable": {}, "edges": []})
            ensure_dir(extracted_dir / "affected_function_slices")
            return payload

        def _baseline_empty_obligations() -> dict[str, Any]:
            payload = {
                "obligations": [],
                "semantic_obligations": [],
                "semantic_obligation_summary": {"count": 0, "strong_count": 0},
                "skipped": True,
                "skip_reason": "baseline_source_only receives no compatibility obligations",
            }
            write_json(extracted_dir / "compatibility_obligations.json", payload)
            return payload

        locations_payload = stage("03_kernel_definition_locator", _baseline_empty_locations)
        kernel_diffs = stage("04_kernel_definition_diff_builder", _baseline_empty_kernel_diffs)
        impact_graph = stage("05_impact_graph_builder", _baseline_empty_impact_graph)
        callback_graph = {"roots": [], "reachable": {}, "edges": []}
        obligations = stage("06_compatibility_obligation_builder", _baseline_empty_obligations)
    else:
        locations_payload = stage(
            "03_kernel_definition_locator",
            lambda: run_definition_locator(
                accessor,
                extracted_dir,
                scan_payload["driver_used_symbols"],
                module_path=args.module_path,
                exclude_driver_paths=(args.affected_func_signal_mode == "kernel_only"),
                jobs=max(1, int(args.jobs or 1)),
            ),
        )

        kernel_diffs = stage(
            "04_kernel_definition_diff_builder",
            lambda: run_kernel_diff_builder(
                accessor,
                extracted_dir,
                locations_payload,
                args.module_path,
                collect_commits=bool(args.include_kernel_commit_info),
                affected_func_signal_mode=args.affected_func_signal_mode,
                old_ref=args.old_ref,
                new_ref=args.new_ref,
                semantic_obligation_table=semantic_obligation_table,
            ),
        )

        impact_graph = stage(
            "05_impact_graph_builder",
            lambda: run_impact_graph_builder(module_result, extracted_dir, scan_payload, kernel_diffs),
        )

        if oot_integrated:
            assert external_source_root is not None
            if oot_pre_patch_build_diagnostics is None:
                oot_pre_patch_build_diagnostics = stage(
                    "05b_oot_pre_patch_build_diagnostics",
                    lambda: collect_oot_pre_patch_build_diagnostics(
                        accessor=accessor,
                        run_dir=run_dir,
                        external_source_root=external_source_root,
                        jobs=args.jobs,
                        make_extra_args=args.oot_make_extra_arg or [],
                        kernel_config_target=args.kernel_config_target,
                        kernel_config_options=args.oot_kernel_config or [],
                    ),
                )
            write_json(extracted_dir / "oot_pre_patch_build_diagnostics.json", oot_pre_patch_build_diagnostics)
            impact_graph = stage(
                "05c_oot_compile_diagnostic_impact_filter",
                lambda: filter_impact_graph_by_oot_compile_diagnostics(
                    impact_graph=impact_graph,
                    diagnostics=oot_pre_patch_build_diagnostics,
                    extracted_dir=extracted_dir,
                    module_path=args.module_path,
                ),
            )
            write_json(extracted_dir / "impact_graph.json", impact_graph)

        callback_graph = {
            "roots": scan_payload.get("driver_callbacks", {}).get("callbacks", []),
            "reachable": {},
            "edges": scan_payload.get("call_graph", {}).get("edges", []),
        }

        obligations = stage(
            "06_compatibility_obligation_builder",
            lambda: run_obligation_builder(
                extracted_dir,
                kernel_diffs,
                impact_graph,
                old_ref=args.old_ref,
                new_ref=args.new_ref,
                module_path=args.module_path,
                semantic_obligation_table=semantic_obligation_table,
            ),
        )

    llm_client = LLMClient(
        LLMConfig(
            provider=args.llm_provider,
            model=args.llm_model,
            enabled=bool(args.enable_llm and not args.dry_run),
            api_key=args.llm_api_key,
            base_url=args.llm_base_url,
            config_ini=args.config_ini,
            timeout_sec=args.llm_timeout_sec,
            max_tokens=args.llm_max_tokens,
            max_retries=args.llm_max_retries,
            retry_backoff_sec=args.llm_retry_backoff_sec,
        )
    )

    baseline_patch_generation = str(args.patch_generation_mode or "agent").startswith("baseline_")
    agent_no_required_migration_edit = bool(
        not baseline_patch_generation and not _requires_effective_migration_edit(obligations)
    )
    pre_patch_llm_dry_run = bool(args.dry_run or baseline_patch_generation or agent_no_required_migration_edit)
    if baseline_patch_generation:
        metadata["baseline_pre_patch_llm_policy"] = (
            "pre-spec and pre-reasoner are dry-run in baseline patch-generation modes; "
            "only the baseline patch prompt calls the LLM"
        )
        write_json(run_dir / "metadata.json", metadata)
    if agent_no_required_migration_edit:
        metadata["agent_no_required_migration_edit_policy"] = (
            "No hard or strong semantic compatibility obligation was found; "
            "pre-spec/pre-reasoner LLM calls are skipped and patch generation may emit a deterministic no-op candidate."
        )
        write_json(run_dir / "metadata.json", metadata)

    pre_specs = stage(
        "07_pre_patch_spec_agent",
        lambda: run_pre_spec_agent(
            llm_client=llm_client,
            llm_calls_dir=llm_calls_dir,
            extracted_dir=extracted_dir,
            case_id=case_id,
            module_path=args.module_path,
            kernel_definition_diffs=kernel_diffs,
            compatibility_obligations=obligations,
            impact_graph=impact_graph,
            dry_run=pre_patch_llm_dry_run,
        ),
    )

    pre_reasoning = stage(
        "08_pre_patch_reasoner_agent",
        lambda: run_pre_reasoner_agent(
            llm_client=llm_client,
            llm_calls_dir=llm_calls_dir,
            extracted_dir=extracted_dir,
            case_id=case_id,
            module_path=args.module_path,
            pre_specs=pre_specs,
            compatibility_obligations=obligations,
            kernel_definition_diffs=kernel_diffs,
            impact_graph=impact_graph,
            callback_graph=callback_graph,
            dry_run=pre_patch_llm_dry_run,
        ),
    )

    context_packet = stage(
        "09_context_packet_builder",
        lambda: run_context_packet_builder(
            out_dir=run_dir,
            case_id=case_id,
            module_path=args.module_path,
            old_ref=args.old_ref,
            new_ref=args.new_ref,
            kernel_diffs=kernel_diffs,
            impact_graph=impact_graph,
            obligations=obligations,
            pre_specs=pre_specs,
            pre_reasoning=pre_reasoning,
        ),
    )

    patch_generation = stage(
        "09b_patch_generation_agent",
        lambda: run_patch_generation_agent(
            llm_client=llm_client,
            llm_calls_dir=llm_calls_dir,
            run_dir=run_dir,
            extracted_dir=extracted_dir,
            case_id=case_id,
            module_path=args.module_path,
            context_packet=context_packet,
            pre_reasoning=pre_reasoning,
            impact_graph=impact_graph,
            compatibility_obligations=obligations,
            dry_run=args.dry_run,
            max_candidates=max(1, args.patch_candidates),
            prompt_strength=args.patch_prompt_strength,
            source_mode=args.patch_source_mode,
            source_with_line_numbers=bool(args.patch_source_with_lineno),
            patch_generation_mode=args.patch_generation_mode,
            old_ref=args.old_ref,
            new_ref=args.new_ref,
            kernel_definition_diffs=kernel_diffs,
        ),
    )

    if args.validation_patch_file:
        patch_generation = stage(
            "09b1_patch_override",
            lambda: _override_patch_generation_with_file(
                run_dir=run_dir,
                patch_generation=patch_generation,
                patch_file=Path(args.validation_patch_file).expanduser().resolve(),
            ),
        )

    if oot_integrated:
        assert external_source_root is not None
        validation = stage(
            "09c_validation_agent",
            lambda: run_oot_integrated_validation_agent(
                accessor=accessor,
                llm_client=llm_client,
                llm_calls_dir=llm_calls_dir,
                run_dir=run_dir,
                case_id=case_id,
                module_path=args.module_path,
                external_source_root=external_source_root,
                module_ko=args.oot_module_ko,
                module_name=args.oot_module_name,
                patch_generation_result=patch_generation,
                context_packet=context_packet,
                impact_graph=impact_graph,
                compatibility_obligations=obligations,
                dry_run=args.dry_run,
                jobs=args.jobs,
                enable_qemu=args.enable_qemu,
                qemu_timeout_sec=args.qemu_timeout_sec,
                qemu_check_script=args.oot_qemu_check_script,
                qemu_test_mode=args.oot_qemu_test_mode,
                qemu_test_level=args.oot_qemu_test_level,
                qemu_device_node=args.oot_device_node,
                qemu_write_data=args.oot_qemu_write_data,
                qemu_expect_data=args.oot_qemu_expect_data,
                make_extra_args=args.oot_make_extra_arg or [],
                kernel_config_target=args.kernel_config_target,
                kernel_config_options=args.oot_kernel_config or [],
                kernel_module_dirs=args.oot_kernel_module_dir or [],
                kernel_module_targets=args.oot_kernel_module or [],
                keep_worktrees=not args.cleanup_worktrees,
            ),
        )
    else:
        max_patch_iterations = max(1, int(args.max_patch_iterations or 1))
        repair_enabled = bool(
            max_patch_iterations > 1
            and args.enable_llm
            and not args.dry_run
            and not args.validation_patch_file
        )
        patch_iteration_records: list[dict[str, Any]] = []
        validation: dict[str, Any] = {}
        for patch_iteration in range(1, max_patch_iterations + 1):
            validation_stage = (
                "09c_validation_agent"
                if patch_iteration == 1
                else f"09c_validation_agent_iter_{patch_iteration:02d}"
            )
            validation = stage(
                validation_stage,
                lambda pg=patch_generation: run_validation_agent(
                    accessor=accessor,
                    llm_client=llm_client,
                    llm_calls_dir=llm_calls_dir,
                    run_dir=run_dir,
                    case_id=case_id,
                    module_path=args.module_path,
                    old_ref=args.old_ref,
                    new_ref=args.new_ref,
                    patch_generation_result=pg,
                    context_packet=context_packet,
                    impact_graph=impact_graph,
                    compatibility_obligations=obligations,
                    dry_run=args.dry_run,
                    jobs=args.jobs,
                    enable_qemu=args.enable_qemu,
                    qemu_timeout_sec=args.qemu_timeout_sec,
                    skip_system_test_generation=args.skip_system_test_generation,
                    enable_host_suite=args.enable_host_suite,
                    keep_worktrees=not args.cleanup_worktrees,
                    kernel_config_target=args.kernel_config_target,
                    kbuild_target=args.kbuild_target,
                    target_source_files=module_result.source_files_old if args.kbuild_target else None,
                ),
            )
            repair_reason = _validation_repair_reason(validation)
            iter_record = _write_patch_iteration_snapshot(
                run_dir=run_dir,
                iteration=patch_iteration,
                patch_generation=patch_generation,
                validation=validation,
                repair_reason=repair_reason,
            )
            patch_iteration_records.append(iter_record)
            write_json(
                run_dir / "patch_iterations.json",
                {
                    "max_patch_iterations": max_patch_iterations,
                    "repair_enabled": repair_enabled,
                    "iterations": patch_iteration_records,
                },
            )
            metadata["patch_iterations"] = patch_iteration_records
            write_json(run_dir / "metadata.json", metadata)

            if not repair_reason:
                break
            if not repair_enabled:
                iter_record["repair_skipped_reason"] = (
                    "disabled: requires --max-patch-iterations > 1, --enable-llm, no --dry-run, and no --validation-patch-file"
                )
                iter_record["will_repair"] = False
                write_json(run_dir / "patch_iterations" / f"iter_{patch_iteration:02d}" / "iteration_result.json", iter_record)
                patch_iteration_records[-1] = iter_record
                write_json(
                    run_dir / "patch_iterations.json",
                    {
                        "max_patch_iterations": max_patch_iterations,
                        "repair_enabled": repair_enabled,
                        "iterations": patch_iteration_records,
                    },
                )
                break
            if patch_iteration >= max_patch_iterations:
                iter_record["repair_skipped_reason"] = "max_patch_iterations_reached"
                iter_record["will_repair"] = False
                write_json(run_dir / "patch_iterations" / f"iter_{patch_iteration:02d}" / "iteration_result.json", iter_record)
                patch_iteration_records[-1] = iter_record
                write_json(
                    run_dir / "patch_iterations.json",
                    {
                        "max_patch_iterations": max_patch_iterations,
                        "repair_enabled": repair_enabled,
                        "iterations": patch_iteration_records,
                    },
                )
                break

            repair_context = _validation_feedback_for_repair(validation, patch_iteration, repair_reason)
            previous_patch_generation = patch_generation
            patch_generation = stage(
                f"09b_patch_repair_iter_{patch_iteration + 1:02d}",
                lambda pg=previous_patch_generation, rc=repair_context, next_iter=patch_iteration + 1: run_patch_generation_agent(
                    llm_client=llm_client,
                    llm_calls_dir=llm_calls_dir,
                    run_dir=run_dir,
                    extracted_dir=extracted_dir,
                    case_id=case_id,
                    module_path=args.module_path,
                    context_packet=context_packet,
                    pre_reasoning=pre_reasoning,
                    impact_graph=impact_graph,
                    compatibility_obligations=obligations,
                    dry_run=False,
                    max_candidates=max(1, args.patch_candidates),
                    prompt_strength=args.patch_prompt_strength,
                    source_mode=args.patch_source_mode,
                    source_with_line_numbers=bool(args.patch_source_with_lineno),
                    patch_generation_mode=args.patch_generation_mode,
                    old_ref=args.old_ref,
                    new_ref=args.new_ref,
                    kernel_definition_diffs=kernel_diffs,
                    repair_iteration=next_iter,
                    repair_context=rc,
                    previous_patch_generation=pg,
                ),
            )

    validation_summary_v2 = stage(
        "09d_validation_summary_v2",
        lambda: build_validation_summary_v2(run_dir, write_outputs=True),
    )

    stage(
        "10_final_report",
        lambda: run_final_report_builder(
            out_dir=run_dir,
            case_id=case_id,
            module_path=args.module_path,
            old_ref=args.old_ref,
            new_ref=args.new_ref,
            module_source={
                "case_scope": module_result.case_scope,
                "kbuild_target": module_result.kbuild_target,
                "kbuild_target_resolution": module_result.kbuild_target_resolution,
                "source_files_old": module_result.source_files_old,
                "headers_old": module_result.headers_old,
                "module_loc_old": module_result.module_loc_old,
            },
            kernel_diffs=kernel_diffs,
            impact_graph=impact_graph,
            callback_graph=callback_graph,
            obligations=obligations,
            pre_specs=pre_specs,
            pre_reasoning=pre_reasoning,
            context_packet=context_packet,
            patch_generation=patch_generation,
            validation=validation,
            validation_summary=validation_summary_v2,
            stage_timings=metadata.get("stages", []),
        ),
    )

    pipeline_ok = str(validation.get("status", "")).lower() == "ok"
    metadata["status"] = "ok" if pipeline_ok else "failed_validation"
    metadata["total_duration_sec"] = round(time.perf_counter() - pipeline_t0, 3)
    write_json(run_dir / "metadata.json", metadata)

    # Small standalone timing artifact for quick lookup.
    write_json(
        run_dir / "stage_timings.json",
        {
            "case_id": case_id,
            "module_path": args.module_path,
            "total_duration_sec": metadata["total_duration_sec"],
            "stages": metadata.get("stages", []),
        },
    )
    return run_dir, pipeline_ok


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.oot_repo_url and args.oot_pipeline_mode == "parallel":
        run_dir, ok = _run_oot_parallel_path(args)
    else:
        run_dir, ok = run_pipeline(args)
    print(f"[driver-agent-pregen] completed: {run_dir}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
