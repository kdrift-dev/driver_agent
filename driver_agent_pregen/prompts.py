from __future__ import annotations

import json
from typing import Any


PRE_SPEC_INSTRUCTION = """
You are a Linux driver migration analyst.
Task: generate migration-specific pre/post conditions for affected driver functions.
Constraints:
1. Only use evidence present in input.
2. If evidence is missing, keep open_questions and do not invent kernel semantics.
3. Do not require edits unless compatibility obligations are directly supported by evidence.
4. Separate required compatibility changes from optional modernization/cleanup.
5. If API usage remains source-compatible and no strong evidence proves it incorrect, mark as optional or UNKNOWN.
6. Do not require caller/sibling edits unless evidence shows caller-visible contract/resource/locking/cleanup changes.
3. Output MUST be strict JSON matching the required schema.
""".strip()


PRE_REASONER_INSTRUCTION = """
You are a Linux driver migration pre-patch reasoner.
Task: determine whether old driver behavior matches new kernel compatibility obligations.
Constraints:
1. Verdict must be MATCH, MISMATCH, or UNKNOWN.
2. Use UNKNOWN when evidence is insufficient.
3. Every claim must cite evidence from input.
4. Generate textual patch guidance only, no code patch.
5. Do not require edits unless compatibility obligations are directly supported by evidence.
6. Separate required compatibility changes from optional modernization/cleanup.
7. If API usage remains source-compatible and no strong evidence proves it incorrect, mark as optional or UNKNOWN.
8. Do not require caller/sibling edits unless evidence shows caller-visible contract/resource/locking/cleanup changes.
5. Output MUST be strict JSON matching the required schema.
""".strip()


PATCH_GEN_INSTRUCTION = """
You are a Linux driver migration patch generator.
Task: generate patch candidates (unified diff) from compatibility obligations and pre-patch reasoning.
Constraints:
1. Patch MUST only touch files under module_path unless clearly required by evidence.
2. Preserve ownership, error path, locking, and cleanup semantics.
3. Do not invent kernel APIs. Use only APIs appearing in evidence or obligations.
4. Output MUST be strict JSON matching the required schema.
5. Patch text must be valid unified diff with diff --git headers.
""".strip()


PATCH_GEN_SOURCE_FULL_APPENDIX = """
Patch source mode: full_module
1. Input provides module-wide old sources. Treat non-affected files/functions as context, not edit targets.
2. Prioritize edits within editable/affected functions from impact_graph unless evidence requires otherwise.
3. Avoid opportunistic cleanup in unrelated functions.
""".strip()


PATCH_GEN_SOURCE_FOCUSED_APPENDIX = """
Patch source mode: focused
1. Input provides only focused code blocks (affected functions and code referencing them).
2. Line prefixes like `L123:` are location hints and are NOT part of real source code.
3. Do not emit line-prefix tokens inside the patch diff.
4. Do not infer edits outside provided focused blocks unless evidence explicitly requires cross-function changes.
""".strip()


PATCH_GEN_STRICT_APPENDIX = """
Strict mode constraints:
1. Do not produce comment-only edits. If you cannot justify a code change, emit an explicit no-op candidate with empty patch_diff.
2. Do not keep an old API call unchanged while only adding explanatory comments around it.
3. If evidence includes a strong semantic migration signal with an explicit replacement candidate
   (for example: kzalloc -> kzalloc_obj, kcalloc -> kzalloc_objs), prefer emitting a concrete callsite replacement patch.
4. Separate required compatibility edits from optional modernization in rationale/evidence; optional does not mean comment-only diff.
""".strip()


POST_REASONER_INSTRUCTION = """
You are a Linux driver migration post-patch reasoner.
Task: evaluate whether patched code satisfies compatibility obligations and whether remaining risk exists.
Constraints:
1. Verdict must be PASS, FAIL, or UNKNOWN.
2. Use UNKNOWN when evidence is insufficient.
3. Every claim must cite evidence from input.
4. Post-patch MATCH/PASS means the patch satisfies stated compatibility obligations, not global correctness.
5. Separate required compatibility changes from optional modernization/cleanup.
6. If evidence is insufficient for semantic correctness, prefer UNKNOWN and list unsupported assumptions.
4. Output MUST be strict JSON matching the required schema.
""".strip()


SYSTEM_TEST_GEN_INSTRUCTION = """
You are a Linux kernel system-level test planner for driver migration.
Task: generate multiple entrypoint-level test cases for lifecycle/callback validation.
Constraints:
1. Focus on init/probe/remove/pm/irq/workqueue/timer or module entry paths relevant to the module.
2. Prefer executable commands and clear pass/fail assertions.
3. Do not invent unavailable hardware; when hardware is uncertain, provide graceful-skip logic.
4. For shell suites, avoid brittle early-exit style:
   - do NOT use `set -e`
   - use per-test return-code handling
   - on missing hardware/platform prerequisites, mark test as SKIP instead of FAIL
5. Output MUST be strict JSON matching the required schema.
""".strip()


def build_pre_spec_prompt(input_payload: dict[str, Any]) -> str:
    schema = {
        "case_id": "string",
        "stage": "pre_spec_generation",
        "function_specs": [
            {
                "function": "string",
                "file": "string",
                "precondition": "string",
                "postcondition": "string",
                "compatibility_obligation": "string",
                "caller_lifecycle_expectation": "string",
                "relevant_kernel_contract": "string",
                "evidence": ["string"],
                "open_questions": ["string"],
                "required_changes": ["string"],
                "optional_changes": ["string"],
                "unsupported_assumptions": ["string"],
                "evidence_used": ["string"],
                "confidence": "high|medium|low",
            }
        ],
        "module_spec": {
            "summary": "string",
            "required_module_level_properties": ["string"],
            "required_changes": ["string"],
            "optional_changes": ["string"],
            "unsupported_assumptions": ["string"],
            "evidence_used": ["string"],
            "confidence": "high|medium|low",
        },
    }
    return (
        PRE_SPEC_INSTRUCTION
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )


def build_pre_reasoner_prompt(input_payload: dict[str, Any]) -> str:
    schema = {
        "case_id": "string",
        "stage": "pre_code_reasoning",
        "overall_verdict": "MATCH|MISMATCH|UNKNOWN",
        "function_results": [
            {
                "function": "string",
                "verdict": "MATCH|MISMATCH|UNKNOWN",
                "spec_claim": "string",
                "actual_behavior": "string",
                "code_evidence": "string",
                "trigger_condition": "string",
                "reasoning": "string",
                "patch_guidance": "string",
                "required_changes": ["string"],
                "optional_changes": ["string"],
                "unsupported_assumptions": ["string"],
                "evidence_used": ["string"],
                "confidence": "high|medium|low",
            }
        ],
        "module_level_findings": ["string"],
        "hard_check_requirements": ["string"],
        "semantic_check_requirements": ["string"],
        "required_changes": ["string"],
        "optional_changes": ["string"],
        "unsupported_assumptions": ["string"],
        "evidence_used": ["string"],
        "confidence": "high|medium|low",
    }
    return (
        PRE_REASONER_INSTRUCTION
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )


def build_patch_generation_prompt(
    input_payload: dict[str, Any],
    *,
    strict_mode: bool = False,
    source_mode: str = "full_module",
    source_with_line_numbers: bool = False,
) -> str:
    schema = {
        "case_id": "string",
        "stage": "patch_generation",
        "patch_candidates": [
            {
                "candidate_id": "string",
                "strategy": "string",
                "rationale": "string",
                "touched_files": ["string"],
                "patch_diff": "string",
                "evidence": ["string"],
                "open_questions": ["string"],
            }
        ],
        "module_level_notes": ["string"],
    }
    instruction = PATCH_GEN_INSTRUCTION
    mode = str(source_mode or "full_module").strip().lower()
    if mode == "focused":
        instruction = instruction + "\n\n" + PATCH_GEN_SOURCE_FOCUSED_APPENDIX
    else:
        instruction = instruction + "\n\n" + PATCH_GEN_SOURCE_FULL_APPENDIX
    if source_with_line_numbers:
        instruction = instruction + "\n\nSource note: line-number prefixes are hints only; never include them in output patch."
    if strict_mode:
        instruction = instruction + "\n\n" + PATCH_GEN_STRICT_APPENDIX
    return (
        instruction
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )


def build_post_reasoner_prompt(input_payload: dict[str, Any]) -> str:
    schema = {
        "case_id": "string",
        "stage": "post_patch_reasoning",
        "overall_verdict": "PASS|FAIL|UNKNOWN",
        "function_results": [
            {
                "function": "string",
                "verdict": "PASS|FAIL|UNKNOWN",
                "obligation": "string",
                "patched_behavior": "string",
                "evidence": ["string"],
                "residual_risk": "string",
                "required_changes": ["string"],
                "optional_changes": ["string"],
                "unsupported_assumptions": ["string"],
                "evidence_used": ["string"],
                "confidence": "high|medium|low",
            }
        ],
        "module_level_findings": ["string"],
        "remaining_open_questions": ["string"],
        "required_changes": ["string"],
        "optional_changes": ["string"],
        "unsupported_assumptions": ["string"],
        "evidence_used": ["string"],
        "confidence": "high|medium|low",
    }
    return (
        POST_REASONER_INSTRUCTION
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )


def build_system_test_generation_prompt(input_payload: dict[str, Any]) -> str:
    schema = {
        "case_id": "string",
        "stage": "system_test_generation",
        "test_cases": [
            {
                "id": "string",
                "name": "string",
                "entry_kind": "init|probe|remove|pm|irq|workqueue|timer|boot|unknown",
                "intent": "string",
                "preconditions": ["string"],
                "steps": [
                    {
                        "cmd": "string",
                        "why": "string",
                    }
                ],
                "assertions": ["string"],
                "timeout_sec": 120,
                "skip_if": ["string"],
                "artifacts": ["string"],
            }
        ],
        "suite_commands": ["string"],
        "notes": ["string"],
    }
    return (
        SYSTEM_TEST_GEN_INSTRUCTION
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )
