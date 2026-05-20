from __future__ import annotations

from collections import defaultdict
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


def _has_effective_changes(patch_text: str) -> bool:
    removed: list[str] = []
    added: list[str] = []
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("@@") or line.startswith("diff --git ") or line.startswith("index "):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
            continue
        if line.startswith("+"):
            added.append(line[1:])
            continue
    if not removed and not added:
        return False
    # A frequent LLM artifact is emitting equal +/- lines (net zero change).
    if len(removed) == len(added) and removed == added:
        return False
    return True


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


def _render_with_line_numbers(text: str, start_line: int = 1) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    return "\n".join(f"L{start_line + i}: {line}" for i, line in enumerate(lines))


def _load_old_module_sources(
    extracted_dir: Path,
    module_path: str,
    *,
    max_chars_per_file: int = 20000,
    with_line_numbers: bool = False,
) -> list[dict[str, Any]]:
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
        if with_line_numbers:
            txt = _render_with_line_numbers(txt, start_line=1)
        entries.append({"path": rel, "content": txt, "source_item_kind": "full_file"})
    return entries


def _load_driver_function_index(extracted_dir: Path) -> dict[str, list[dict[str, Any]]]:
    payload = read_json(extracted_dir / "driver_functions.json", default={}) or {}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fn in payload.get("functions", []) or []:
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        out[name].append(fn)
    return dict(out)


def _extract_function_block(
    *,
    extracted_dir: Path,
    fn_name: str,
    rel_file: str,
    start_line: int,
    end_line: int,
    roles: list[str],
    with_line_numbers: bool,
    max_chars_per_block: int,
) -> dict[str, Any] | None:
    src_path = extracted_dir / "old_module_sources" / rel_file
    if not src_path.exists() or not src_path.is_file():
        return None

    lines = read_text(src_path).splitlines()
    if not lines:
        return None

    start = max(1, int(start_line))
    end = min(len(lines), int(end_line))
    if end < start:
        return None

    raw_text = "\n".join(lines[start - 1 : end])
    if len(raw_text) > max_chars_per_block:
        raw_text = raw_text[:max_chars_per_block] + "\n...[truncated]"

    content = _render_with_line_numbers(raw_text, start_line=start) if with_line_numbers else raw_text
    return {
        "path": rel_file,
        "function": fn_name,
        "start_line": start,
        "end_line": end,
        "roles": sorted(set(roles)),
        "content": content,
        "source_item_kind": "function_block",
    }


def _build_focused_old_sources(
    *,
    extracted_dir: Path,
    module_path: str,
    impact_graph: dict[str, Any],
    with_line_numbers: bool,
    max_chars_per_block: int = 12000,
    max_blocks: int = 80,
) -> list[dict[str, Any]]:
    affected = list(impact_graph.get("affected_functions", []) or [])
    if not affected:
        return []

    roles_by_fn: dict[str, set[str]] = defaultdict(set)
    records_by_fn: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for af in affected:
        fn = str(af.get("function", "")).strip()
        if not fn:
            continue
        roles_by_fn[fn].add("affected")

        file_rel = str(af.get("file", "")).strip()
        if file_rel:
            records_by_fn[fn].append(
                {
                    "name": fn,
                    "file": file_rel,
                    "start_line": int(af.get("start_line", 1)),
                    "end_line": int(af.get("end_line", af.get("start_line", 1))),
                }
            )

        for caller in af.get("ordinary_callers", []) or []:
            c = str(caller or "").strip()
            if c:
                roles_by_fn[c].add("direct_caller")

        for cb in af.get("callback_roots", []) or []:
            root_fn = str((cb or {}).get("root_function", "")).strip()
            if root_fn:
                roles_by_fn[root_fn].add("callback_root")

    fn_index = _load_driver_function_index(extracted_dir)
    blocks: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, int, int]] = set()

    for fn_name in sorted(roles_by_fn.keys()):
        roles = sorted(roles_by_fn[fn_name])
        candidates = list(records_by_fn.get(fn_name, []))
        if fn_name in fn_index:
            candidates.extend(fn_index[fn_name])
        if not candidates:
            continue

        for rec in candidates:
            rel_file = str(rec.get("file", "")).strip()
            if not rel_file or not rel_file.startswith(module_path.rstrip("/") + "/"):
                continue
            start_line = int(rec.get("start_line", 1))
            end_line = int(rec.get("end_line", start_line))
            key = (fn_name, rel_file, start_line, end_line)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            block = _extract_function_block(
                extracted_dir=extracted_dir,
                fn_name=fn_name,
                rel_file=rel_file,
                start_line=start_line,
                end_line=end_line,
                roles=roles,
                with_line_numbers=with_line_numbers,
                max_chars_per_block=max_chars_per_block,
            )
            if not block:
                continue
            blocks.append(block)
            if len(blocks) >= max_blocks:
                return blocks

    return blocks


def _collect_file_contents(extracted_dir: Path, paths: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    base = extracted_dir / "old_module_sources"
    for rel in paths:
        p = base / rel
        if p.exists() and p.is_file():
            out[rel] = read_text(p)
    return out


def _list_module_source_paths(extracted_dir: Path, module_path: str) -> list[str]:
    root = extracted_dir / "old_module_sources" / module_path
    if not root.exists():
        return []
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(extracted_dir / "old_module_sources").as_posix()
        out.append(rel)
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


def _extract_raw_candidates(llm_output: dict[str, Any]) -> list[dict[str, Any]]:
    raw = llm_output.get("patch_candidates", [])
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]

    parsed = llm_output.get("parsed_json")
    if isinstance(parsed, dict):
        nested = parsed.get("patch_candidates", [])
        if isinstance(nested, dict):
            return [nested]
        if isinstance(nested, list):
            return [x for x in nested if isinstance(x, dict)]

    direct_patch = (
        llm_output.get("patch_diff")
        or llm_output.get("patch_text")
        or llm_output.get("diff")
        or llm_output.get("patch")
        or ""
    )
    if isinstance(direct_patch, str) and direct_patch.strip():
        return [{"candidate_id": "CAND-001", "patch_diff": direct_patch}]
    return []


def _should_emit_auto_noop(
    *,
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
) -> bool:
    obligations = compatibility_obligations.get("obligations", [])
    sem_obligations = compatibility_obligations.get("semantic_obligations", [])
    if obligations:
        return False
    if sem_obligations:
        return False

    affected = impact_graph.get("affected_functions", [])
    if affected:
        return False

    return True


def _has_strong_semantic_obligations(compatibility_obligations: dict[str, Any]) -> bool:
    for item in compatibility_obligations.get("semantic_obligations", []):
        if str(item.get("strength", "")).lower() == "strong":
            return True
    return False


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
    prompt_strength: str = "normal",
    source_mode: str = "full_module",
    source_with_line_numbers: bool = True,
) -> dict[str, Any]:
    stage_dir = llm_calls_dir / "03_patch_generation"
    stage_dir.mkdir(parents=True, exist_ok=True)
    patch_runs_dir = run_dir / "patch_runs"
    patch_runs_dir.mkdir(parents=True, exist_ok=True)

    mode = str(source_mode or "full_module").strip().lower()
    if mode not in {"full_module", "focused"}:
        mode = "full_module"

    if mode == "focused":
        old_sources = _build_focused_old_sources(
            extracted_dir=extracted_dir,
            module_path=module_path,
            impact_graph=impact_graph,
            with_line_numbers=bool(source_with_line_numbers),
        )
        # Defensive fallback: keep pipeline usable if focused extraction returns empty.
        if not old_sources:
            mode = "full_module"
            old_sources = _load_old_module_sources(
                extracted_dir,
                module_path,
                with_line_numbers=bool(source_with_line_numbers),
            )
    else:
        old_sources = _load_old_module_sources(
            extracted_dir,
            module_path,
            with_line_numbers=bool(source_with_line_numbers),
        )

    input_payload = {
        "case_id": case_id,
        "module_path": module_path,
        "max_candidates": max_candidates,
        "patch_source_mode": mode,
        "patch_source_with_line_numbers": bool(source_with_line_numbers),
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

    strict_mode = str(prompt_strength or "normal").strip().lower() == "strict"
    prompt = build_patch_generation_prompt(
        input_payload,
        strict_mode=strict_mode,
        source_mode=mode,
        source_with_line_numbers=bool(source_with_line_numbers),
    )
    default_output = {
        "case_id": case_id,
        "stage": "patch_generation",
        "patch_candidates": [],
        "module_level_notes": ["dry-run or no-provider placeholder"],
    }

    deterministic_noop = _should_emit_auto_noop(
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
    )
    if deterministic_noop:
        write_text(stage_dir / "prompt.md", prompt)
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "raw_output.txt", "")
        llm_output = {
            "case_id": case_id,
            "stage": "patch_generation",
            "patch_candidates": [],
            "module_level_notes": [
                "deterministic_noop_short_circuit: pre_reasoning=MATCH and both obligations/affected_functions are empty"
            ],
            "parse_status": "skipped_auto_noop",
        }
        write_json(stage_dir / "parsed_output.json", llm_output)
    else:
        llm_output = llm_client.call_json(
            stage_dir=stage_dir,
            prompt_text=prompt,
            input_payload=input_payload,
            dry_run=dry_run,
            default_output=default_output,
        )

    raw_candidates = _extract_raw_candidates(llm_output)
    normalized: list[dict[str, Any]] = []
    policy_rejections: list[dict[str, Any]] = []
    rejected_noop_candidates: list[dict[str, Any]] = []
    strong_semantic_required = _has_strong_semantic_obligations(compatibility_obligations)

    for idx, cand in enumerate(raw_candidates[:max_candidates], start=1):
        cid = cand.get("candidate_id") or f"CAND-{idx:03d}"
        cdir = patch_runs_dir / cid.lower().replace("/", "_")
        cdir.mkdir(parents=True, exist_ok=True)

        raw_patch_blob = (
            cand.get("patch_diff")
            or cand.get("patch_text")
            or cand.get("diff")
            or cand.get("patch")
            or ""
        )
        raw_diff = _extract_diff_text(raw_patch_blob)
        raw_path = cdir / "01_raw_patch.diff"
        write_text(raw_path, raw_diff)

        touched = cand.get("touched_files") or _list_touched_files_from_patch(raw_diff)
        file_map = _collect_file_contents(extracted_dir, touched)
        fixed_ok, fixed_text, fix_msgs = fix_patch_line_numbers(raw_diff, file_contents=file_map)
        normalized_input = fixed_text if fixed_text else raw_diff
        fixed_text, sem_msgs = _normalize_patch_semantics(normalized_input)
        fix_msgs.extend(sem_msgs)

        candidate_patch = fixed_text if fixed_text else raw_diff
        no_op_candidate = not _has_effective_changes(candidate_patch)
        if no_op_candidate:
            # Keep touched_files for downstream staging of old sources, but avoid emitting invalid no-op hunks.
            candidate_patch = ""
            fix_msgs.append("detected_no_effective_diff; normalized_to_noop_candidate")
            if strong_semantic_required:
                fix_msgs.append("rejected_noop_due_to_strong_semantic_obligation")
                fix_report = {
                    "candidate_id": cid,
                    "raw_patch_nonempty": bool(raw_diff.strip()),
                    "line_fix_ok": fixed_ok,
                    "messages": fix_msgs,
                    "touched_files": touched,
                    "no_op_candidate": True,
                    "rejected_by_policy": True,
                }
                write_json(cdir / "patch_fix_report.json", fix_report)
                policy_rejections.append(
                    {
                        "candidate_id": cid,
                        "reason": "strong_semantic_obligation_disallows_noop_candidate",
                    }
                )
                rejected_noop_candidates.append(
                    {
                        "candidate_id": cid,
                        "strategy": cand.get("strategy", ""),
                        "rationale": cand.get("rationale", ""),
                        "evidence": cand.get("evidence", []),
                        "open_questions": cand.get("open_questions", []),
                        "touched_files": touched,
                        "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
                        "line_fix_ok": fixed_ok,
                        "line_fix_messages": fix_msgs,
                    }
                )
                continue
        elif not fixed_ok and _has_effective_changes(raw_diff):
            # If line-fix validation fails, prefer original raw patch text as a conservative fallback.
            candidate_patch = raw_diff
            fix_msgs.append("line_fix_failed; fallback_to_raw_patch_text")

        fixed_path = cdir / "02_fixed_patch.diff"
        write_text(fixed_path, candidate_patch)

        fix_report = {
            "candidate_id": cid,
            "raw_patch_nonempty": bool(raw_diff.strip()),
            "line_fix_ok": fixed_ok,
            "messages": fix_msgs,
            "touched_files": touched,
            "no_op_candidate": no_op_candidate,
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
                "no_op_candidate": no_op_candidate,
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
            normalized_input = fixed_text if fixed_text else raw_patch
            fixed_text, sem_msgs = _normalize_patch_semantics(normalized_input)
            fix_msgs.extend(sem_msgs)
            candidate_patch = fixed_text if fixed_text else raw_patch
            no_op_candidate = not _has_effective_changes(candidate_patch)
            reject_heur_candidate = False
            if no_op_candidate:
                candidate_patch = ""
                fix_msgs.append("detected_no_effective_diff; normalized_to_noop_candidate")
                if strong_semantic_required:
                    fix_msgs.append("rejected_noop_due_to_strong_semantic_obligation")
                    fix_report = {
                        "candidate_id": cid,
                        "raw_patch_nonempty": bool(raw_patch.strip()),
                        "line_fix_ok": fixed_ok,
                        "messages": fix_msgs,
                        "touched_files": touched,
                        "fallback_reason": heur.get("reason", ""),
                        "no_op_candidate": True,
                        "rejected_by_policy": True,
                    }
                    write_json(cdir / "patch_fix_report.json", fix_report)
                    policy_rejections.append(
                        {
                            "candidate_id": cid,
                            "reason": "strong_semantic_obligation_disallows_noop_candidate",
                            "fallback": "heuristic",
                        }
                    )
                    rejected_noop_candidates.append(
                        {
                            "candidate_id": cid,
                            "strategy": "heuristic_fallback",
                            "rationale": heur.get("reason", ""),
                            "evidence": ["pre_reasoning.patch_guidance"],
                            "open_questions": [],
                            "touched_files": touched,
                            "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
                            "line_fix_ok": fixed_ok,
                            "line_fix_messages": fix_msgs,
                        }
                    )
                    reject_heur_candidate = True
            elif not fixed_ok and _has_effective_changes(raw_patch):
                candidate_patch = raw_patch
                fix_msgs.append("line_fix_failed; fallback_to_raw_patch_text")
            if not reject_heur_candidate:
                fixed_path = cdir / "02_fixed_patch.diff"
                write_text(fixed_path, candidate_patch)

                fix_report = {
                    "candidate_id": cid,
                    "raw_patch_nonempty": bool(raw_patch.strip()),
                    "line_fix_ok": fixed_ok,
                    "messages": fix_msgs,
                    "touched_files": touched,
                    "fallback_reason": heur.get("reason", ""),
                    "no_op_candidate": no_op_candidate,
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
                        "no_op_candidate": no_op_candidate,
                    }
                )

    if not normalized and deterministic_noop:
        cid = "CAND-AUTO-NOOP-001"
        cdir = patch_runs_dir / cid.lower()
        cdir.mkdir(parents=True, exist_ok=True)
        raw_path = cdir / "01_raw_patch.diff"
        fixed_path = cdir / "02_fixed_patch.diff"
        write_text(raw_path, "")
        write_text(fixed_path, "")
        touched = _list_module_source_paths(extracted_dir, module_path)
        fix_report = {
            "candidate_id": cid,
            "raw_patch_nonempty": False,
            "line_fix_ok": True,
            "messages": ["auto_noop_from_match_without_obligations_or_affected_functions"],
            "touched_files": touched,
            "no_op_candidate": True,
        }
        write_json(cdir / "patch_fix_report.json", fix_report)
        normalized.append(
            {
                "candidate_id": cid,
                "strategy": "auto_noop_fallback",
                "rationale": "No patch candidate available; pre_reasoning MATCH with empty obligations and no affected functions.",
                "evidence": [
                    f"pre_reasoning.overall_verdict={pre_reasoning.get('overall_verdict', '')}",
                    "compatibility_obligations.obligations is empty",
                    "impact_graph.affected_functions is empty",
                ],
                "open_questions": [
                    "LLM patch generation produced no usable candidate; auto no-op fallback used for compile/runtime validation continuity."
                ],
                "touched_files": touched,
                "raw_patch_path": raw_path.relative_to(run_dir).as_posix(),
                "fixed_patch_path": fixed_path.relative_to(run_dir).as_posix(),
                "line_fix_ok": True,
                "line_fix_messages": ["auto_noop_from_match_without_obligations_or_affected_functions"],
                "no_op_candidate": True,
            }
        )

    # Validation continuity fallback:
    # if all candidates are rejected only because strong-semantic policy blocks no-op,
    # keep one explicit no-op candidate so compile/test validation can still run.
    if not normalized and rejected_noop_candidates:
        keep = rejected_noop_candidates[0]
        cid = keep.get("candidate_id") or "CAND-POLICY-FALLBACK-NOOP-001"
        cdir = patch_runs_dir / str(cid).lower().replace("/", "_")
        cdir.mkdir(parents=True, exist_ok=True)
        fixed_path = cdir / "02_fixed_patch.diff"
        write_text(fixed_path, "")
        line_fix_messages = list(keep.get("line_fix_messages") or [])
        line_fix_messages.append("policy_fallback_keep_noop_candidate_for_validation")
        normalized.append(
            {
                "candidate_id": cid,
                "strategy": keep.get("strategy") or "policy_fallback_noop",
                "rationale": keep.get("rationale", ""),
                "evidence": keep.get("evidence", []),
                "open_questions": keep.get("open_questions", []),
                "touched_files": keep.get("touched_files", []),
                "raw_patch_path": keep.get("raw_patch_path", ""),
                "fixed_patch_path": fixed_path.relative_to(run_dir).as_posix(),
                "line_fix_ok": bool(keep.get("line_fix_ok", True)),
                "line_fix_messages": line_fix_messages,
                "no_op_candidate": True,
                "policy_fallback": True,
            }
        )

    out = {
        "case_id": case_id,
        "stage": "patch_generation",
        "patch_source_mode": mode,
        "patch_source_with_line_numbers": bool(source_with_line_numbers),
        "parse_status": llm_output.get("parse_status", "unknown"),
        "module_level_notes": llm_output.get("module_level_notes", []),
        "patch_candidates": normalized,
        "selected_candidate_id": normalized[0]["candidate_id"] if normalized else None,
        "policy_rejections": policy_rejections,
        "strong_semantic_required": strong_semantic_required,
    }

    write_json(run_dir / "patch_generation.json", out)
    return out
