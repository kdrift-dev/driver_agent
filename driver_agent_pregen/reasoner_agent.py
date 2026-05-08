from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_post_reasoner_prompt, build_pre_reasoner_prompt
from .utils import read_text


def _collect_slices(extracted_dir: Path, impact_graph: dict[str, Any], max_chars: int = 6000) -> list[dict[str, Any]]:
    out = []
    for afn in impact_graph.get("affected_functions", []):
        rel = afn.get("local_slice_path")
        if not rel:
            continue
        p = extracted_dir / rel
        if not p.exists():
            continue
        txt = read_text(p)
        if len(txt) > max_chars:
            txt = txt[:max_chars] + "\n...[truncated]"
        out.append({"function": afn.get("function"), "file": afn.get("file"), "slice": txt})
    return out


def run_pre_reasoner_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    pre_specs: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    kernel_definition_diffs: dict[str, Any],
    impact_graph: dict[str, Any],
    callback_graph: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "02_pre_reasoner"
    stage_dir.mkdir(parents=True, exist_ok=True)

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "pre_patch_specs": pre_specs,
        "compatibility_obligations": compatibility_obligations,
        "kernel_definition_diffs": kernel_definition_diffs,
        "impact_graph": impact_graph,
        "callback_graph": callback_graph,
        "affected_function_slices": _collect_slices(extracted_dir, impact_graph),
        "instruction": "Judge old driver compatibility against new kernel obligations and provide textual patch guidance.",
    }

    prompt = build_pre_reasoner_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "pre_code_reasoning",
        "overall_verdict": "UNKNOWN",
        "function_results": [],
        "module_level_findings": ["dry-run or no-provider placeholder"],
        "hard_check_requirements": [],
        "semantic_check_requirements": [],
    }

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )


def run_post_reasoner_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    case_id: str,
    module_path: str,
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    patched_function_slices: list[dict[str, Any]],
    compile_result: dict[str, Any],
    test_generation_result: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "04_post_reasoner"
    stage_dir.mkdir(parents=True, exist_ok=True)

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "compatibility_obligations": compatibility_obligations,
        "impact_graph": impact_graph,
        "patched_function_slices": patched_function_slices,
        "compile_result": compile_result,
        "generated_system_tests": test_generation_result,
        "instruction": "Evaluate whether the patched module satisfies obligations and list residual risks.",
    }

    prompt = build_post_reasoner_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "post_patch_reasoning",
        "overall_verdict": "UNKNOWN",
        "function_results": [],
        "module_level_findings": ["dry-run or no-provider placeholder"],
        "remaining_open_questions": [],
    }

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )
