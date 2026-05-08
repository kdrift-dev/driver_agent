from __future__ import annotations

import argparse
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
from .utils import now_timestamp, slugify, write_json
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
    p.add_argument("--enable-qemu", action="store_true", help="enable qemu/kunit runtime command execution if present in generated suite commands")
    p.add_argument("--qemu-timeout-sec", type=int, default=3600, help="timeout seconds for suite/qemu commands")
    p.add_argument("--cleanup-worktrees", action="store_true", help="remove temporary worktrees after validation")
    return p


def _make_case_id(module_path: str, old_ref: str | None, new_ref: str | None) -> str:
    parts = [module_path.replace("/", "_")]
    if old_ref:
        parts.append(old_ref)
    if new_ref:
        parts.append(new_ref)
    return slugify("__".join(parts))


def run_pipeline(args: argparse.Namespace) -> Path:
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
        "stages": [],
    }
    write_json(run_dir / "metadata.json", metadata)

    def stage(name: str, fn):
        try:
            out = fn()
            metadata["stages"].append({"name": name, "status": "ok"})
            write_json(run_dir / "metadata.json", metadata)
            return out
        except Exception as exc:
            metadata["stages"].append(
                {
                    "name": name,
                    "status": "failed",
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
        lambda: run_kernel_diff_builder(accessor, extracted_dir, locations_payload, args.module_path),
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
        lambda: run_obligation_builder(extracted_dir, kernel_diffs, impact_graph),
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
        ),
    )

    metadata["status"] = "ok"
    write_json(run_dir / "metadata.json", metadata)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_dir = run_pipeline(args)
    print(f"[driver-agent-pregen] completed: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
