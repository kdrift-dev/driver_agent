from __future__ import annotations

import argparse
import datetime as dt
import time
import traceback
from pathlib import Path
from typing import Any

from .context_packet import run_context_packet_builder
from .definition_locator import run_definition_locator
from .impact_graph_builder import run_impact_graph_builder
from .kernel_diff_builder import run_kernel_diff_builder
from .llm_client import LLMClient, LLMConfig
from .module_source_loader import run_module_source_loader
from .obligation_builder import run_obligation_builder
from .patch_generation import run_patch_generation_agent
from .reasoner_agent import run_pre_reasoner_agent
from .report import run_final_report_builder
from .repo_utils import RepositoryAccessor
from .spec_agent import run_pre_spec_agent
from .symbol_scanner import run_symbol_scanner
from .utils import now_timestamp, read_text, slugify, write_json, write_text
from .validation import run_validation_agent


def build_parser() -> argparse.ArgumentParser:
    default_config = str(Path(__file__).resolve().parents[1] / "config.ini")
    p = argparse.ArgumentParser(description="Driver migration pre-generation pipeline")
    p.add_argument("--repo", type=str, default=None, help="path to git repo (preferred)")
    p.add_argument("--old-ref", type=str, default=None)
    p.add_argument("--new-ref", type=str, default=None)
    p.add_argument("--old-tree", type=str, default=None, help="fallback old source tree")
    p.add_argument("--new-tree", type=str, default=None, help="fallback new source tree")
    p.add_argument("--module-path", type=str, required=True)
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
    p.add_argument("--llm-max-retries", type=int, default=2, help="LLM request max retries")
    p.add_argument("--llm-retry-backoff-sec", type=int, default=3, help="LLM retry backoff seconds")

    p.add_argument("--patch-candidates", type=int, default=1, help="number of patch candidates to request from LLM")
    p.add_argument(
        "--patch-prompt-strength",
        type=str,
        choices=["normal", "strict"],
        default="strict",
        help="patch-generation prompt strictness (strict forbids comment-only edits and pushes concrete semantic migration replacements)",
    )
    p.add_argument(
        "--patch-source-mode",
        type=str,
        choices=["full_module", "focused"],
        default="full_module",
        help=(
            "source payload mode for patch generation: "
            "full_module (all old module sources) or focused "
            "(affected functions and referencing callers/callback roots only)"
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
        "--affected-func-signal-mode",
        type=str,
        choices=["kernel_only", "kernel_plus_other_drivers"],
        default="kernel_only",
        help=(
            "affected-function signal source mode: "
            "'kernel_only' uses strict kernel-side evidence only (default); "
            "'kernel_plus_other_drivers' is an opt-in ablation mode that additionally allows "
            "migration hints from non-target drivers (target module diff is still excluded)."
        ),
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
        "--semantic-obligation-table",
        type=str,
        default=None,
        help="optional semantic obligation table JSON path; queried to enrich kernel/driver semantic obligations",
    )
    p.add_argument(
        "--skip-system-test-generation",
        action="store_true",
        help="skip LLM system-test generation and suite/qemu execution; keep patch apply/compile/post-reasoning",
    )
    p.add_argument("--cleanup-worktrees", action="store_true", help="remove temporary worktrees after validation")
    return p


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


def _make_case_id(module_path: str, old_ref: str | None, new_ref: str | None) -> str:
    parts = [module_path.replace("/", "_")]
    if old_ref:
        parts.append(old_ref)
    if new_ref:
        parts.append(new_ref)
    return slugify("__".join(parts))


def run_pipeline(args: argparse.Namespace) -> tuple[Path, bool]:
    accessor = RepositoryAccessor.from_args(
        repo=args.repo,
        old_ref=args.old_ref,
        new_ref=args.new_ref,
        old_tree=args.old_tree,
        new_tree=args.new_tree,
    )
    accessor.validate()

    case_id = _make_case_id(args.module_path, args.old_ref, args.new_ref)
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
        "mode": accessor.describe(),
        "jobs": args.jobs,
        "dry_run": args.dry_run,
        "llm_enabled": args.enable_llm,
        "llm_provider": args.llm_provider,
        "llm_model": args.llm_model,
        "affected_func_signal_mode": args.affected_func_signal_mode,
        "patch_prompt_strength": args.patch_prompt_strength,
        "patch_source_mode": args.patch_source_mode,
        "patch_source_with_lineno": bool(args.patch_source_with_lineno),
        "semantic_obligation_table": args.semantic_obligation_table,
        "stages": [],
    }
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

    module_result = stage(
        "01_module_source_loader",
        lambda: run_module_source_loader(accessor, module_path=args.module_path, extracted_dir=extracted_dir),
    )

    scan_payload = stage(
        "02_old_driver_symbol_scanner",
        lambda: run_symbol_scanner(module_result, extracted_dir),
    )

    locations_payload = stage(
        "03_kernel_definition_locator",
        lambda: run_definition_locator(
            accessor,
            extracted_dir,
            scan_payload["driver_used_symbols"],
            module_path=args.module_path,
        ),
    )

    kernel_diffs = stage(
        "04_kernel_definition_diff_builder",
        lambda: run_kernel_diff_builder(
            accessor,
            extracted_dir,
            locations_payload,
            args.module_path,
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
            max_retries=args.llm_max_retries,
            retry_backoff_sec=args.llm_retry_backoff_sec,
        )
    )

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
            dry_run=args.dry_run,
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
            dry_run=args.dry_run,
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

    validation = stage(
        "09c_validation_agent",
        lambda: run_validation_agent(
            accessor=accessor,
            llm_client=llm_client,
            llm_calls_dir=llm_calls_dir,
            run_dir=run_dir,
            case_id=case_id,
            module_path=args.module_path,
            old_ref=args.old_ref,
            new_ref=args.new_ref,
            patch_generation_result=patch_generation,
            context_packet=context_packet,
            impact_graph=impact_graph,
            compatibility_obligations=obligations,
            dry_run=args.dry_run,
            jobs=args.jobs,
            enable_qemu=args.enable_qemu,
            qemu_timeout_sec=args.qemu_timeout_sec,
            skip_system_test_generation=args.skip_system_test_generation,
            keep_worktrees=not args.cleanup_worktrees,
        ),
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
    run_dir, ok = run_pipeline(args)
    print(f"[driver-agent-pregen] completed: {run_dir}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
