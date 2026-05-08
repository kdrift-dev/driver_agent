from __future__ import annotations

from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .prompts import build_pre_spec_prompt
from .utils import read_text


def _load_slices(extracted_dir: Path, impact_graph: dict[str, Any], max_chars_per_slice: int = 6000) -> list[dict[str, Any]]:
    slices = []
    for afn in impact_graph.get("affected_functions", []):
        rel = afn.get("local_slice_path")
        if not rel:
            continue
        p = extracted_dir / rel
        if not p.exists():
            continue
        txt = read_text(p)
        if len(txt) > max_chars_per_slice:
            txt = txt[:max_chars_per_slice] + "\n...[truncated]"
        slices.append(
            {
                "function": afn.get("function"),
                "file": afn.get("file"),
                "slice_path": rel,
                "slice": txt,
            }
        )
    return slices


def run_pre_spec_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    kernel_definition_diffs: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "01_pre_spec"
    stage_dir.mkdir(parents=True, exist_ok=True)

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "kernel_definition_diffs": kernel_definition_diffs,
        "compatibility_obligations": compatibility_obligations,
        "impact_graph": impact_graph,
        "affected_function_slices": _load_slices(extracted_dir, impact_graph),
        "instruction": "Generate migration-specific pre/post conditions grounded in kernel compatibility obligations and caller/callback context.",
    }

    prompt = build_pre_spec_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "pre_spec_generation",
        "function_specs": [],
        "module_spec": {
            "summary": "dry-run or no-provider placeholder",
            "required_module_level_properties": [],
        },
    }

    return llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )
