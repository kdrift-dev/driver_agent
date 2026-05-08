from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .llm_client import LLMClient
from .patch_line_fixer import fix_patch_line_numbers
from .prompts import build_patch_generation_prompt
from .utils import read_json, read_text, write_json, write_text


def _extract_diff_text(text: str) -> str:
    if not text:
        return ""
    stripped = text.strip()
    if stripped.startswith("diff --git"):
        return stripped + ("\n" if not stripped.endswith("\n") else "")

    fenced = re.findall(r"```(?:diff|patch)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    for block in fenced:
        blk = block.strip()
        if blk.startswith("diff --git") or ("@@" in blk and "--- " in blk and "+++ " in blk):
            return blk + ("\n" if not blk.endswith("\n") else "")

    idx = text.find("diff --git")
    if idx >= 0:
        tail = text[idx:].strip()
        return tail + ("\n" if not tail.endswith("\n") else "")

    return ""


def _list_touched_files_from_patch(patch_text: str) -> list[str]:
    out: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        p = line[len("+++ b/") :].strip()
        if p == "/dev/null":
            continue
        if p and p not in out:
            out.append(p)
    return out


def _normalize_patch_semantics(patch_text: str) -> tuple[str, list[str]]:
    """
    Deterministic post-fix for known migration-sensitive idioms.

    Current rule:
    - When patch introduces `region_intersects_soft_reserve(...)`, avoid
      collapsing `if (rc != REGION_INTERSECTS)` into `if (!rc)`.
    """
    msgs: list[str] = []
    lines = patch_text.splitlines()
    out: list[str] = []
    saw_soft_reserve_call = False

    for line in lines:
        raw = line
        body = raw
        prefix = ""
        if raw.startswith(("+", "-", " ")):
            prefix = raw[0]
            body = raw[1:]

        if "region_intersects_soft_reserve(" in body:
            saw_soft_reserve_call = True

        if saw_soft_reserve_call and body.strip() == "if (!rc)":
            indent = body[: len(body) - len(body.lstrip(" \t"))]
            fixed = f"{prefix}{indent}if (rc != REGION_INTERSECTS)"
            out.append(fixed)
            msgs.append("normalized condition: if (!rc) -> if (rc != REGION_INTERSECTS)")
            continue

        out.append(line)

    normalized = "\n".join(out)
    if patch_text.endswith("\n"):
        normalized += "\n"
    return normalized, msgs


def _load_old_module_sources(extracted_dir: Path, module_path: str, max_chars_per_file: int = 20000) -> list[dict[str, Any]]:
    module_root = extracted_dir / "old_module_sources" / module_path
    if not module_root.exists():
        return []

    entries: list[dict[str, Any]] = []
    for p in sorted(module_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(extracted_dir / "old_module_sources").as_posix()
        txt = read_text(p)
        if len(txt) > max_chars_per_file:
            txt = txt[:max_chars_per_file] + "\n...[truncated]"
        entries.append({"path": rel, "content": txt})
    return entries


def _collect_file_contents(extracted_dir: Path, paths: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    base = extracted_dir / "old_module_sources"
    for rel in paths:
        p = base / rel
        if p.exists() and p.is_file():
            out[rel] = read_text(p)
    return out


def _parse_guided_replacements(pre_reasoning: dict[str, Any]) -> list[Tuple[str, str]]:
    repls: list[Tuple[str, str]] = []
    pat = re.compile(
        r"replace(?:\s+the\s+call)?\s+['`\"]([^'`\"]+)['`\"]\s+with\s+['`\"]([^'`\"]+)['`\"]",
        flags=re.IGNORECASE,
    )
    for fr in pre_reasoning.get("function_results", []):
        guide = fr.get("patch_guidance", "")
        for m in pat.finditer(guide or ""):
            old = m.group(1).strip()
            new = m.group(2).strip()
            if old and new and (old, new) not in repls:
                repls.append((old, new))
    return repls


def _heuristic_patch_from_guidance(
    *,
    extracted_dir: Path,
    module_path: str,
    pre_reasoning: dict[str, Any],
) -> dict[str, Any]:
    repls = _parse_guided_replacements(pre_reasoning)
    if not repls:
        return {"ok": False, "reason": "no_replacement_pattern_in_patch_guidance", "patch": "", "touched_files": []}

    root = extracted_dir / "old_module_sources" / module_path
    touched: list[str] = []
    patch_chunks: list[str] = []

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(extracted_dir / "old_module_sources").as_posix()
        orig = read_text(p)
        new = orig
        changed = False
        for old_call, new_call in repls:
            if old_call in new:
                new = new.replace(old_call, new_call, 1)
                changed = True
        if not changed:
            continue

        udiff = list(
            difflib.unified_diff(
                orig.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        if not udiff:
            continue

        patch_chunks.append(f"diff --git a/{rel} b/{rel}\n")
        patch_chunks.extend(udiff)
        touched.append(rel)

    if not patch_chunks:
        return {"ok": False, "reason": "replacement_patterns_not_found_in_old_sources", "patch": "", "touched_files": []}

    patch_text = "".join(patch_chunks)
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"

    return {
        "ok": True,
        "reason": "heuristic_from_pre_reasoning_guidance",
        "patch": patch_text,
        "touched_files": touched,
    }


def run_patch_generation_agent(
    *,
    llm_client: LLMClient,
    llm_calls_dir: Path,
    run_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    context_packet: dict[str, Any],
    pre_reasoning: dict[str, Any],
    impact_graph: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    dry_run: bool,
    max_candidates: int = 1,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "03_patch_generation"
    stage_dir.mkdir(parents=True, exist_ok=True)

    old_sources = _load_old_module_sources(extracted_dir, module_path)
    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "max_candidates": max_candidates,
        "compatibility_obligations": compatibility_obligations,
        "impact_graph": impact_graph,
        "pre_reasoning": pre_reasoning,
        "context_packet": {
            "kernel_change_summary": context_packet.get("kernel_change_summary", ""),
            "patch_generation_guidance": context_packet.get("patch_generation_guidance", []),
            "hard_validation_plan": context_packet.get("hard_validation_plan", []),
            "semantic_validation_plan": context_packet.get("semantic_validation_plan", []),
            "open_questions": context_packet.get("open_questions", []),
        },
        "old_module_sources": old_sources,
        "instruction": "Generate unified diff patch candidate(s) for migration. Keep patch minimal and evidence-grounded.",
    }

    prompt = build_patch_generation_prompt(input_payload)
    default_output = {
        "case_id": case_id,
        "stage": "patch_generation",
        "patch_candidates": [],
        "module_level_notes": ["dry-run or no-provider placeholder"],
    }

    llm_output = llm_client.call_json(
        stage_dir=stage_dir,
        prompt_text=prompt,
        input_payload=input_payload,
        dry_run=dry_run,
        default_output=default_output,
    )

    patch_runs_dir = run_dir / "patch_runs"
    patch_runs_dir.mkdir(parents=True, exist_ok=True)

    raw_candidates = llm_output.get("patch_candidates", [])
    normalized: list[dict[str, Any]] = []

    for idx, cand in enumerate(raw_candidates[:max_candidates], start=1):
        cid = cand.get("candidate_id") or f"CAND-{idx:03d}"
        cdir = patch_runs_dir / cid.lower().replace("/", "_")
        cdir.mkdir(parents=True, exist_ok=True)

        raw_diff = _extract_diff_text(cand.get("patch_diff", ""))
        raw_path = cdir / "01_raw_patch.diff"
        write_text(raw_path, raw_diff)

        touched = cand.get("touched_files") or _list_touched_files_from_patch(raw_diff)
        file_map = _collect_file_contents(extracted_dir, touched)
        fixed_ok, fixed_text, fix_msgs = fix_patch_line_numbers(raw_diff, file_contents=file_map)
        fixed_text, sem_msgs = _normalize_patch_semantics(fixed_text if fixed_text else raw_diff)
        fix_msgs.extend(sem_msgs)

        fixed_path = cdir / "02_fixed_patch.diff"
        write_text(fixed_path, fixed_text if fixed_text else raw_diff)

        fix_report = {
            "candidate_id": cid,
            "raw_patch_nonempty": bool(raw_diff.strip()),
            "line_fix_ok": fixed_ok,
            "messages": fix_msgs,
            "touched_files": touched,
        }
        write_json(cdir / "patch_fix_report.json", fix_report)

        normalized.append(
            {
                "candidate_id": cid,
                "strategy": cand.get("strategy", ""),
                "rationale": cand.get("rationale", ""),
                "evidence": cand.get("evidence", []),
                "open_questions": cand.get("open_questions", []),
                "touched_files": touched,
                "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
                "fixed_patch_path": fixed_path.relative_to(run_dir).as_posix(),
                "line_fix_ok": fixed_ok,
                "line_fix_messages": fix_msgs,
            }
        )

    if not normalized:
        heur = _heuristic_patch_from_guidance(
            extracted_dir=extracted_dir,
            module_path=module_path,
            pre_reasoning=pre_reasoning,
        )
        if heur.get("ok"):
            cid = "CAND-HEURISTIC-001"
            cdir = patch_runs_dir / cid.lower()
            cdir.mkdir(parents=True, exist_ok=True)
            raw_patch = heur.get("patch", "")
            raw_path = cdir / "01_raw_patch.diff"
            write_text(raw_path, raw_patch)

            touched = heur.get("touched_files", [])
            file_map = _collect_file_contents(extracted_dir, touched)
            fixed_ok, fixed_text, fix_msgs = fix_patch_line_numbers(raw_patch, file_contents=file_map)
            fixed_text, sem_msgs = _normalize_patch_semantics(fixed_text if fixed_text else raw_patch)
            fix_msgs.extend(sem_msgs)
            fixed_path = cdir / "02_fixed_patch.diff"
            write_text(fixed_path, fixed_text if fixed_text else raw_patch)

            fix_report = {
                "candidate_id": cid,
                "raw_patch_nonempty": bool(raw_patch.strip()),
                "line_fix_ok": fixed_ok,
                "messages": fix_msgs,
                "touched_files": touched,
                "fallback_reason": heur.get("reason", ""),
            }
            write_json(cdir / "patch_fix_report.json", fix_report)

            normalized.append(
                {
                    "candidate_id": cid,
                    "strategy": "heuristic_fallback",
                    "rationale": heur.get("reason", ""),
                    "evidence": ["pre_reasoning.patch_guidance"],
                    "open_questions": [],
                    "touched_files": touched,
                    "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
                    "fixed_patch_path": fixed_path.relative_to(run_dir).as_posix(),
                    "line_fix_ok": fixed_ok,
                    "line_fix_messages": fix_msgs,
                }
            )

    out = {
        "case_id": case_id,
        "stage": "patch_generation",
        "parse_status": llm_output.get("parse_status", "unknown"),
        "module_level_notes": llm_output.get("module_level_notes", []),
        "patch_candidates": normalized,
        "selected_candidate_id": normalized[0]["candidate_id"] if normalized else None,
    }

    write_json(run_dir / "patch_generation.json", out)
    return out
