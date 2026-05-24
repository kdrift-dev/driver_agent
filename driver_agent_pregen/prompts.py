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
7. Respect edit_scope: edit_required functions need function-level specs; edit_candidate functions need conditional specs; context_only functions provide lifecycle/resource context and must not become required edits without evidence.
8. Output MUST be strict JSON matching the required schema.
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
9. Respect edit_scope: only edit_required functions can be required by default; edit_candidate functions require explicit cross-function evidence; context_only functions are non-editable reasoning context.
10. Output MUST be strict JSON matching the required schema.
""".strip()


PATCH_GEN_INSTRUCTION = """
You are a Linux driver migration patch generator.
Task: generate patch candidates (unified diff) from compatibility obligations and pre-patch reasoning.
Constraints:
1. Patch MUST only touch files under module_path unless clearly required by evidence.
2. Preserve ownership, error path, locking, and cleanup semantics.
3. Do not invent kernel APIs. Use only APIs appearing in evidence or obligations.
3a. Preserve return-value conventions. If a replacement helper returns int or an enum-like status, do not convert existing comparisons into boolean checks unless evidence explicitly says the new helper returns bool.
4. Respect edit_scope_policy:
   - edit_required functions are default edit targets.
   - edit_candidate functions may be edited only when rationale cites their candidate evidence.
   - context_only functions must not be edited.
5. If a needed edit appears outside edit_required/edit_candidate scope, emit no-op with open_questions instead of patching outside scope.
6. Output MUST be strict JSON matching the required schema.
7. Patch text must be valid unified diff with diff --git headers.
""".strip()


PATCH_GEN_SOURCE_FULL_APPENDIX = """
Patch source mode: full_module
1. Input provides module-wide old sources. This is a fallback/debug mode; treat non-affected files/functions as context, not edit targets.
2. Prioritize edits within edit_required functions.
3. Edit edit_candidate functions only if their candidate_reasons explain why the cross-function edit is needed.
4. Never edit context_only functions.
5. Avoid opportunistic cleanup in unrelated functions.
""".strip()


PATCH_GEN_SOURCE_FOCUSED_APPENDIX = """
Patch source mode: focused
1. Input provides focused code blocks, not full module source.
2. Blocks with source_item_kind=function_block and editable=true are the only patchable source blocks.
3. Blocks with source_item_kind=context_function_block or editable=false are read-only context; do not edit them.
4. Line prefixes like `L123:` are location hints and are NOT part of real source code.
5. Do not emit line-prefix tokens inside the patch diff.
6. Do not infer edits outside edit_required/edit_candidate blocks.
7. If a correct patch seems to require code outside the provided editable blocks, emit a no-op candidate with open_questions instead of guessing.
""".strip()


PATCH_GEN_STRICT_APPENDIX = """
Strict mode constraints:
1. Do not produce comment-only edits. If you cannot justify a code change, emit an explicit no-op candidate with empty patch_diff.
2. Do not keep an old API call unchanged while only adding explanatory comments around it.
3. If evidence includes a strong semantic migration signal with an explicit replacement candidate,
   prefer emitting a concrete callsite replacement patch.
4. Separate required compatibility edits from optional modernization in rationale/evidence; optional does not mean comment-only diff.
""".strip()


BASELINE_PATCH_GEN_INSTRUCTION = """
You are a Linux driver migration baseline patch generator.
Task: generate patch candidates (unified diff) using only the baseline input payload.
Constraints:
1. Do not use or assume target driver new-version source, target driver diff, reference patch, compatibility obligations, affected-function graph, pre-specs, or pre/post reasoning unless they are explicitly present in the baseline input.
2. Patch MUST only touch files under module_path.
3. Preserve ownership, error path, locking, cleanup, and existing driver behavior unless the input gives evidence for a required migration.
3a. Preserve return-value conventions. If a replacement helper returns int or an enum-like status, do not rewrite existing comparisons as boolean checks unless the supplied evidence explicitly says the helper returns bool.
4. If the input does not provide enough evidence for a code change, emit an explicit no-op candidate with empty patch_diff.
5. Do not invent kernel APIs. In kernel-evidence mode, use only APIs appearing in the supplied kernel evidence or old module source.
6. Output MUST be strict JSON matching the required schema.
7. Patch text must be valid unified diff with diff --git headers.
""".strip()


BASELINE_SOURCE_ONLY_APPENDIX = """
Baseline mode: source_only
1. Input contains old target module source and old/new ref names only.
2. Do not infer hidden kernel API changes from the version numbers.
3. This baseline is expected to be conservative; no-op is acceptable when evidence is insufficient.
""".strip()


BASELINE_KERNEL_EVIDENCE_APPENDIX = """
Baseline mode: kernel_evidence
1. Input contains old target module source plus kernel-side evidence extracted from symbols used by that old module.
2. Use kernel evidence directly; do not assume non-target driver migrations or target driver new-version changes.
3. Prefer minimal callsite changes only when kernel-side evidence clearly supports a migration.
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


OOT_QEMU_TEST_GEN_INSTRUCTION = """
You are generating an in-QEMU shell smoke test for an out-of-tree Linux driver.
Task: produce a shell script body that will run inside the generated initramfs after /proc, /sys, and /dev are mounted.
Constraints:
1. Use only BusyBox-compatible /bin/sh commands: insmod, rmmod, cat, grep, awk, find, test, ls, head, tail, wc, cut, sort, uniq, printf, echo, sleep, dmesg, uname, poweroff.
2. The runner exports DRIVER_AGENT_MODULE_KO and DRIVER_AGENT_MODULE_NAME.
3. Do not use host paths, package managers, network access, systemd, udev, modprobe, Python, Perl, or bash-only syntax.
4. Do not use `set -e`; handle command failures explicitly.
5. Prefer real interface checks: sysfs/procfs/debugfs/device-node read/write if visible in source.
6. If no functional interface is evident, generate a load/unload smoke test and say so.
7. On success, the script MUST print DRIVER_AGENT_QEMU_SMOKE_PASS before poweroff.
8. On failure, print a DRIVER_AGENT_*_FAIL marker before poweroff.
9. On unavailable optional interface/hardware, prefer a clear SKIP marker only when module load itself succeeds.
10. Output MUST be strict JSON matching the required schema.
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


def build_baseline_patch_generation_prompt(
    input_payload: dict[str, Any],
    *,
    baseline_mode: str,
    strict_mode: bool = False,
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
    mode = str(baseline_mode or "source_only").strip().lower()
    instruction = BASELINE_PATCH_GEN_INSTRUCTION
    if mode == "kernel_evidence":
        instruction = instruction + "\n\n" + BASELINE_KERNEL_EVIDENCE_APPENDIX
    else:
        instruction = instruction + "\n\n" + BASELINE_SOURCE_ONLY_APPENDIX
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


def build_oot_qemu_test_generation_prompt(input_payload: dict[str, Any]) -> str:
    schema = {
        "case_id": "string",
        "stage": "oot_qemu_test_generation",
        "test_level": "load_unload|interface_smoke|semantic_scenario|unknown",
        "script": "shell script body, no markdown fences",
        "expected_markers": ["DRIVER_AGENT_QEMU_SMOKE_PASS"],
        "preconditions": ["string"],
        "assertions": ["string"],
        "skip_conditions": ["string"],
        "notes": ["string"],
        "unsupported_assumptions": ["string"],
        "confidence": "high|medium|low",
    }
    return (
        OOT_QEMU_TEST_GEN_INSTRUCTION
        + "\n\nRequired output schema:\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
        + "\nInput payload:\n```json\n"
        + json.dumps(input_payload, indent=2)
        + "\n```\n"
    )
