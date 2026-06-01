from __future__ import annotations

from collections import defaultdict
import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .llm_client import LLMClient
from .patch_line_fixer import fix_patch_line_numbers
from .prompts import build_baseline_patch_generation_prompt, build_patch_generation_prompt, build_patch_repair_prompt
from .utils import read_json, read_text, truncate_text, write_json, write_text


DEFAULT_PATCH_INPUT_BUDGET_CHARS = int(os.environ.get("DRIVER_AGENT_PATCH_INPUT_BUDGET_CHARS", "180000"))


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
    if os.environ.get("DRIVER_AGENT_ALLOW_CASE_SPECIFIC_FIXUPS") != "1":
        return patch_text, []

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


def _extract_file_include_block(
    *,
    extracted_dir: Path,
    rel_file: str,
    roles: list[str],
    editable: bool,
    with_line_numbers: bool,
    max_lines: int = 80,
) -> dict[str, Any] | None:
    src_path = extracted_dir / "old_module_sources" / rel_file
    if not src_path.exists() or not src_path.is_file():
        return None
    lines = read_text(src_path).splitlines()
    if not lines:
        return None

    last_include_idx = -1
    for idx, line in enumerate(lines[:max_lines]):
        if line.lstrip().startswith("#include"):
            last_include_idx = idx
    if last_include_idx < 0:
        return None

    # Include enough top-of-file context for unified diffs that add headers and
    # update file-level declarations/structs used by editable functions. Keep
    # read-only context narrower to avoid full-module prompt noise.
    preamble_limit = max_lines if editable else min(max_lines, 30)
    end = min(len(lines), max(last_include_idx + 3, preamble_limit))
    raw_text = "\n".join(lines[:end])
    content = _render_with_line_numbers(raw_text, start_line=1) if with_line_numbers else raw_text
    return {
        "path": rel_file,
        "function": None,
        "start_line": 1,
        "end_line": end,
        "roles": sorted(set(roles)),
        "content": content,
        "source_item_kind": "file_preamble_block" if editable else "file_include_block",
        "editable": editable,
        "edit_scope": "edit_required" if editable else "context_only",
        "candidate_reasons": [],
        "context_reasons": ["file include block for direct include dependency edits"],
    }


def _local_headers_in_source(extracted_dir: Path, rel_file: str, module_path: str, max_lines: int = 120) -> list[str]:
    src_path = extracted_dir / "old_module_sources" / rel_file
    if not src_path.exists() or not src_path.is_file():
        return []
    base_dir = str(Path(rel_file).parent)
    out: list[str] = []
    for line in read_text(src_path).splitlines()[:max_lines]:
        m = re.match(r'\s*#\s*include\s+"([^"]+)"', line)
        if not m:
            continue
        hdr = (Path(base_dir) / m.group(1)).as_posix()
        if not hdr.startswith(module_path.rstrip("/") + "/"):
            continue
        if hdr not in out:
            out.append(hdr)
    return out


def _record_to_source_candidate(item: dict[str, Any], role: str) -> dict[str, Any] | None:
    fn = str(item.get("function", "") or "").strip()
    rel_file = str(item.get("file", "") or "").strip()
    if not fn or not rel_file:
        return None
    try:
        start = int(item.get("start_line", 1) or 1)
        end = int(item.get("end_line", start) or start)
    except Exception:
        start = 1
        end = 1
    return {
        "name": fn,
        "file": rel_file,
        "start_line": start,
        "end_line": end,
        "role": role,
        "source_meta": item,
    }


def _build_focused_old_sources(
    *,
    extracted_dir: Path,
    module_path: str,
    impact_graph: dict[str, Any],
    with_line_numbers: bool,
    max_chars_per_block: int = 8000,
    max_blocks: int = 48,
) -> list[dict[str, Any]]:
    required = list(impact_graph.get("edit_required_functions", []) or [])
    candidates = list(impact_graph.get("edit_candidate_functions", []) or [])
    context_only = list(impact_graph.get("context_only_functions", []) or impact_graph.get("context_functions", []) or [])
    affected = list(impact_graph.get("affected_functions", []) or [])

    # Backward compatibility for old run artifacts without explicit edit scopes.
    if not required and not candidates and affected:
        required = [x for x in affected if str(x.get("edit_scope", "edit_required")) == "edit_required"]
        candidates = [x for x in affected if str(x.get("edit_scope", "")) == "edit_candidate"]
        if not required and not candidates:
            required = affected

    if not required and not candidates and not context_only:
        return []

    roles_by_fn: dict[str, set[str]] = defaultdict(set)
    records_by_fn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_meta_by_fn: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add_record(item: dict[str, Any], role: str) -> None:
        rec = _record_to_source_candidate(item, role)
        if not rec:
            return
        fn = str(rec["name"])
        roles_by_fn[fn].add(role)
        records_by_fn[fn].append(rec)
        source_meta_by_fn[fn].append(dict(item))

    for item in required:
        add_record(item, "edit_required")

    for item in candidates:
        add_record(item, "edit_candidate")

    for item in context_only:
        add_record(item, "context_only")

    # Add direct callers / callback roots as read-only context unless they are
    # already edit_required/edit_candidate.
    for af in required + candidates:
        for caller in af.get("ordinary_callers", []) or []:
            c = str(caller or "").strip()
            if c:
                roles_by_fn[c].add("context_direct_caller")

        for cb in af.get("callback_roots", []) or []:
            root_fn = str((cb or {}).get("root_function", "")).strip()
            if root_fn:
                roles_by_fn[root_fn].add("context_callback_root")

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
            meta_items = source_meta_by_fn.get(fn_name, [])
            is_editable = bool({"edit_required", "edit_candidate"} & set(roles))
            if "context_only" in roles and not is_editable:
                block["source_item_kind"] = "context_function_block"
            block["editable"] = is_editable
            block["edit_scope"] = (
                "edit_required"
                if "edit_required" in roles
                else "edit_candidate"
                if "edit_candidate" in roles
                else "context_only"
            )
            block["candidate_reasons"] = sorted(
                {
                    str(x)
                    for meta in meta_items
                    for x in (meta.get("candidate_reasons", []) or [])
                    if str(x).strip()
                }
            )
            block["context_reasons"] = sorted(
                {
                    str(x)
                    for meta in meta_items
                    for x in (meta.get("context_reasons", []) or [])
                    if str(x).strip()
                }
            )
            blocks.append(block)
            if len(blocks) >= max_blocks:
                return blocks

    # Focused mode still needs top-of-file include context when obligations
    # require adding direct kernel headers. Add one include block per file that
    # already has editable source slices.
    file_roles: dict[str, set[str]] = defaultdict(set)
    file_editable: dict[str, bool] = defaultdict(bool)
    for block in blocks:
        rel = str(block.get("path", "") or "")
        if not rel:
            continue
        for role in block.get("roles", []) or []:
            file_roles[rel].add(str(role))
        if bool(block.get("editable")):
            file_editable[rel] = True
    include_blocks: list[dict[str, Any]] = []
    for rel in sorted(file_roles):
        ib = _extract_file_include_block(
            extracted_dir=extracted_dir,
            rel_file=rel,
            roles=["file_include_context"] + sorted(file_roles[rel]),
            editable=bool(file_editable[rel]),
            with_line_numbers=with_line_numbers,
            max_lines=160 if file_editable[rel] else 80,
        )
        if ib:
            include_blocks.append(ib)

    # If an editable C file declares callbacks/helpers in a local module header,
    # provide that header preamble as editable context. This keeps focused mode
    # small while allowing required signature changes to update both the
    # definition and declaration.
    seen_include_paths = {str(x.get("path", "")) for x in include_blocks}
    for rel in sorted(file_roles):
        if not file_editable.get(rel):
            continue
        if Path(rel).suffix.lower() not in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        for hdr in _local_headers_in_source(extracted_dir, rel, module_path):
            if hdr in seen_include_paths:
                continue
            ib = _extract_file_include_block(
                extracted_dir=extracted_dir,
                rel_file=hdr,
                roles=["local_header_edit_context"],
                editable=True,
                with_line_numbers=with_line_numbers,
                max_lines=160,
            )
            if ib:
                ib["context_reasons"] = [
                    f"local header included by editable source {rel}; declarations may need signature-compatible edits"
                ]
                include_blocks.append(ib)
                seen_include_paths.add(hdr)

    blocks = include_blocks + blocks
    if len(blocks) > max_blocks:
        return blocks[:max_blocks]
    return blocks


def _collect_file_contents(extracted_dir: Path, paths: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    base = extracted_dir / "old_module_sources"
    for rel in paths:
        p = base / rel
        if p.exists() and p.is_file():
            out[rel] = read_text(p)
    return out


def _extract_diagnostic_source_paths(
    *,
    repair_context: dict[str, Any],
    extracted_dir: Path,
    module_path: str,
    max_files: int = 6,
) -> list[str]:
    texts: list[str] = []
    for run in ((repair_context.get("compile") or {}).get("failed_runs", []) or []):
        texts.append(str(run.get("stderr", "") or ""))
        texts.append(str(run.get("stdout", "") or ""))
    for run in ((repair_context.get("compile") or {}).get("prepare_failures", []) or []):
        texts.append(str(run.get("stderr", "") or ""))
        texts.append(str(run.get("stdout", "") or ""))

    root = extracted_dir / "old_module_sources"
    module_root = root / module_path
    by_basename: dict[str, str] = {}
    if module_root.exists():
        for p in module_root.rglob("*"):
            if p.is_file() and p.suffix.lower() in {".c", ".h", ".rs"}:
                by_basename.setdefault(p.name, p.relative_to(root).as_posix())

    out: list[str] = []
    path_re = re.compile(r"\b(drivers/[A-Za-z0-9_./+\-]+\.(?:c|h|rs))(?::\d+)?")
    basename_re = re.compile(r"\b([A-Za-z0-9_+\-]+\.(?:c|h|rs))(?::\d+)?")
    for text in texts:
        for m in path_re.finditer(text):
            rel = m.group(1)
            if rel.startswith(module_path.rstrip("/") + "/") and (root / rel).exists() and rel not in out:
                out.append(rel)
        for m in basename_re.finditer(text):
            rel = by_basename.get(m.group(1))
            if rel and rel not in out:
                out.append(rel)
        if len(out) >= max_files:
            break
    return out[:max_files]


def _load_repair_diagnostic_sources(
    *,
    extracted_dir: Path,
    module_path: str,
    repair_context: dict[str, Any],
    with_line_numbers: bool,
    max_chars_per_file: int = 12000,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rel in _extract_diagnostic_source_paths(
        repair_context=repair_context,
        extracted_dir=extracted_dir,
        module_path=module_path,
    ):
        p = extracted_dir / "old_module_sources" / rel
        if not p.exists():
            continue
        text = read_text(p)
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + "\n...[truncated_diagnostic_file]"
        if with_line_numbers:
            text = _render_with_line_numbers(text, start_line=1)
        out.append(
            {
                "path": rel,
                "content": text,
                "source_item_kind": "diagnostic_failure_file",
                "editable": True,
                "edit_scope": "edit_candidate",
                "roles": ["compile_failure_diagnostic_context"],
                "candidate_reasons": ["file appears in compile failure diagnostics"],
                "context_reasons": [],
            }
        )
    return out


def _function_key(item: dict[str, Any]) -> tuple[str, str] | None:
    fn = str(item.get("function", "") or "").strip()
    path = str(item.get("file", "") or "").strip()
    if not fn or not path:
        return None
    return (path, fn)


def _edit_scope_payload(
    impact_graph: dict[str, Any],
    *,
    max_required: int = 24,
    max_candidates: int = 16,
    max_context: int = 8,
) -> dict[str, Any]:
    required = list(impact_graph.get("edit_required_functions", []) or [])
    candidates = list(impact_graph.get("edit_candidate_functions", []) or [])
    context_only = list(impact_graph.get("context_only_functions", []) or impact_graph.get("context_functions", []) or [])

    def slim(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        out = []
        for item in items[:limit]:
            out.append(
                {
                    "function": item.get("function"),
                    "file": item.get("file"),
                    "edit_scope": item.get("edit_scope"),
                    "tier": item.get("tier"),
                    "candidate_reasons": [
                        truncate_text(str(x), max_chars=140)
                        for x in (item.get("candidate_reasons", []) or [])[:4]
                    ],
                    "context_reasons": [
                        truncate_text(str(x), max_chars=140)
                        for x in (item.get("context_reasons", []) or [])[:3]
                    ],
                    "uses_changed_kernel_symbols": [
                        truncate_text(str(x), max_chars=120)
                        for x in (item.get("uses_changed_kernel_symbols", []) or [])[:8]
                    ],
                }
            )
        return out

    return {
        "policy": impact_graph.get("affected_tier_policy", {}),
        "edit_required_functions": slim(required, max_required),
        "edit_candidate_functions": slim(candidates, max_candidates),
        "context_only_functions": slim(context_only, max_context),
        "editable_functions": slim(required + candidates, max_required + max_candidates),
        "omitted_counts": {
            "edit_required_functions": max(0, len(required) - max_required),
            "edit_candidate_functions": max(0, len(candidates) - max_candidates),
            "context_only_functions": max(0, len(context_only) - max_context),
        },
        "rule_summary": (
            "edit_required functions are default edit targets; edit_candidate functions may be edited only "
            "when candidate evidence is cited; context_only functions are non-editable reasoning context."
        ),
    }


def _compact_obligation_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in (evidence or [])[:3]:
        value = ev.get("value")
        if isinstance(value, str):
            value = truncate_text(value, max_chars=250)
        elif isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if k in {"file", "required_include", "provider", "line", "symbol", "path"}
            }
        out.append({"type": ev.get("type"), "value": value})
    return out


def _compact_replacement_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "score": item.get("score"),
        "evidence": [
            truncate_text(str(x), max_chars=160)
            for x in (item.get("evidence", []) or [])[:2]
            if str(x).strip()
        ],
        "paths": [str(x) for x in (item.get("paths", []) or [])[:2] if str(x).strip()],
        "new_location": {
            k: v
            for k, v in (item.get("new_location") or {}).items()
            if k in {"path", "line", "match"}
        },
        "return_type_guess": item.get("return_type_guess"),
    }


def _compact_obligation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "affected_kernel_symbol": item.get("affected_kernel_symbol"),
        "affected_driver_functions": [str(x) for x in (item.get("affected_driver_functions", []) or [])[:12]],
        "statement": truncate_text(str(item.get("statement", "") or ""), max_chars=360),
        "kind": item.get("kind"),
        "strength": item.get("strength"),
        "recommended_action": item.get("recommended_action"),
        "hard_checks": [truncate_text(str(x), max_chars=180) for x in (item.get("hard_checks", []) or [])[:4]],
        "semantic_checks": [truncate_text(str(x), max_chars=180) for x in (item.get("semantic_checks", []) or [])[:3]],
        "confidence": item.get("confidence"),
        "semantic_strength": item.get("semantic_strength"),
        "migration_signal": item.get("migration_signal", False),
        "replacement_candidates": [
            _compact_replacement_candidate(x)
            for x in (item.get("replacement_candidates", []) or [])[:3]
            if x.get("symbol")
        ],
        "patch_guidance_seed": truncate_text(str(item.get("patch_guidance_seed", "") or ""), max_chars=360),
        "open_questions": [truncate_text(str(x), max_chars=160) for x in (item.get("open_questions", []) or [])[:4]],
        "evidence": _compact_obligation_evidence(item.get("evidence", []) or []),
    }


def _obligation_priority(item: dict[str, Any]) -> tuple[int, int, int, int]:
    hard = 1 if item.get("hard_checks") else 0
    strong = 1 if str(item.get("semantic_strength", item.get("strength", ""))).lower() == "strong" else 0
    replacement = 1 if item.get("replacement_candidates") else 0
    high_confidence = 1 if str(item.get("confidence", "")).lower() == "high" else 0
    return (hard, strong, replacement, high_confidence)


def _compact_agent_obligations(compatibility_obligations: dict[str, Any]) -> dict[str, Any]:
    required_obligations = [
        x
        for x in compatibility_obligations.get("obligations", []) or []
        if x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong"
    ]
    required_obligations = sorted(required_obligations, key=_obligation_priority, reverse=True)
    strong_semantic = [
        x
        for x in compatibility_obligations.get("semantic_obligations", []) or []
        if str(x.get("strength", "")).lower() == "strong"
    ]
    strong_semantic = sorted(strong_semantic, key=_obligation_priority, reverse=True)
    max_obligations = int(os.environ.get("DRIVER_AGENT_PATCH_MAX_OBLIGATIONS", "32"))
    max_semantic = int(os.environ.get("DRIVER_AGENT_PATCH_MAX_SEMANTIC_OBLIGATIONS", "12"))
    return {
        "obligations": [
            _compact_obligation(x)
            for x in required_obligations[:max_obligations]
        ],
        "semantic_obligations": [
            _compact_obligation(x)
            for x in strong_semantic[:max_semantic]
        ],
        "semantic_obligation_summary": compatibility_obligations.get("semantic_obligation_summary", {}),
        "semantic_obligations_omitted_from_llm": {
            "count": sum(
                1
                for x in compatibility_obligations.get("semantic_obligations", []) or []
                if str(x.get("strength", "")).lower() != "strong"
            )
            + sum(
                1
                for x in compatibility_obligations.get("obligations", []) or []
                if not (x.get("hard_checks") or str(x.get("semantic_strength", "")).lower() == "strong")
            ),
            "reason": "medium/low semantic guidance is not a required edit and is omitted from LLM payload",
        },
        "required_obligations_omitted_from_llm": {
            "count": max(0, len(required_obligations) - max_obligations),
            "reason": "patch-generation prompt budget; highest-priority hard/strong obligations are kept first",
        },
        "strong_semantic_obligations_omitted_from_llm": {
            "count": max(0, len(strong_semantic) - max_semantic),
            "reason": "patch-generation prompt budget; highest-priority semantic obligations are kept first",
        },
        "llm_payload_filter": "compact obligations; full evidence remains in extracted/compatibility_obligations.json",
    }


def _target_edit_checklist(compatibility_obligations: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact required-edit checklist for the patch generator.

    This is intentionally redundant with compatibility_obligations, but much
    easier for the LLM to follow than a large evidence blob. It contains no new
    evidence or reference-driver information.
    """
    rows: list[dict[str, Any]] = []
    for item in compatibility_obligations.get("obligations", []) or []:
        hard = list(item.get("hard_checks", []) or [])
        strength = str(item.get("semantic_strength", "") or "").lower()
        if not hard and strength != "strong":
            continue
        replacements = [
            {
                "symbol": c.get("symbol"),
                "evidence": [truncate_text(str(x), max_chars=120) for x in (c.get("evidence", []) or [])[:2]],
                "paths": (c.get("paths", []) or [])[:2],
            }
            for c in (item.get("replacement_candidates", []) or [])[:3]
            if c.get("symbol")
        ]
        rows.append(
            {
                "obligation_id": item.get("id"),
                "old_symbol": item.get("affected_kernel_symbol"),
                "affected_driver_functions": [str(x) for x in (item.get("affected_driver_functions", []) or [])[:12]],
                "required": True,
                "hard_checks": [truncate_text(str(x), max_chars=160) for x in hard[:3]],
                "replacement_candidates": replacements,
                "patch_guidance_seed": truncate_text(str(item.get("patch_guidance_seed", "") or ""), max_chars=280),
                "confidence": item.get("confidence"),
            }
        )
    return rows[:24]


def _compact_pre_reasoning(pre_reasoning: dict[str, Any]) -> dict[str, Any]:
    out = {
        "overall_verdict": pre_reasoning.get("overall_verdict"),
        "required_changes": [truncate_text(str(x), max_chars=240) for x in (pre_reasoning.get("required_changes", []) or [])[:12]],
        "optional_changes": [truncate_text(str(x), max_chars=160) for x in (pre_reasoning.get("optional_changes", []) or [])[:6]],
        "unsupported_assumptions": [truncate_text(str(x), max_chars=160) for x in (pre_reasoning.get("unsupported_assumptions", []) or [])[:6]],
        "evidence_used": [truncate_text(str(x), max_chars=160) for x in (pre_reasoning.get("evidence_used", []) or [])[:10]],
        "confidence": pre_reasoning.get("confidence"),
        "function_results": [],
        "llm_payload_filter": "compact pre-reasoning; full output remains in llm_calls/02_pre_reasoner/parsed_output.json",
    }
    for item in pre_reasoning.get("function_results", []) or []:
        out["function_results"].append(
            {
                "function": item.get("function"),
                "verdict": item.get("verdict"),
                "spec_claim": truncate_text(str(item.get("spec_claim", "") or ""), max_chars=600),
                "actual_behavior": truncate_text(str(item.get("actual_behavior", "") or ""), max_chars=400),
                "trigger_condition": truncate_text(str(item.get("trigger_condition", "") or ""), max_chars=260),
                "patch_guidance": truncate_text(str(item.get("patch_guidance", "") or ""), max_chars=520),
                "required_changes": item.get("required_changes", [])[:6],
                "optional_changes": item.get("optional_changes", [])[:4],
                "evidence_used": [truncate_text(str(x), max_chars=140) for x in (item.get("evidence_used", []) or [])[:4]],
                "confidence": item.get("confidence"),
            }
        )
    return out


def _compact_guidance_rows(rows: list[Any], *, max_rows: int, max_text: int = 360) -> list[Any]:
    out: list[Any] = []
    for row in (rows or [])[:max_rows]:
        if isinstance(row, dict):
            out.append(
                {
                    "function": row.get("function"),
                    "guidance": truncate_text(str(row.get("guidance", "") or row.get("patch_guidance", "") or ""), max_chars=max_text),
                    "evidence": [truncate_text(str(x), max_chars=140) for x in (row.get("evidence", []) or [])[:3]],
                }
            )
        else:
            out.append(truncate_text(str(row), max_chars=max_text))
    return out


def _compact_context_packet_for_patch(context_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "kernel_change_summary": context_packet.get("kernel_change_summary", ""),
        "patch_generation_guidance": _compact_guidance_rows(
            context_packet.get("patch_generation_guidance", []) or [],
            max_rows=12,
            max_text=360,
        ),
        "hard_validation_plan": [truncate_text(str(x), max_chars=180) for x in (context_packet.get("hard_validation_plan", []) or [])[:10]],
        "semantic_validation_plan": [truncate_text(str(x), max_chars=180) for x in (context_packet.get("semantic_validation_plan", []) or [])[:8]],
        "open_questions": [truncate_text(str(x), max_chars=180) for x in (context_packet.get("open_questions", []) or [])[:8]],
    }


def _source_payload_summary(
    *,
    mode: str,
    requested_mode: str,
    old_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    kinds: dict[str, int] = defaultdict(int)
    editable = 0
    readonly = 0
    paths: set[str] = set()
    for item in old_sources:
        kinds[str(item.get("source_item_kind", "unknown"))] += 1
        paths.add(str(item.get("path", "")))
        if bool(item.get("editable")):
            editable += 1
        else:
            readonly += 1
    return {
        "requested_mode": requested_mode,
        "actual_mode": mode,
        "full_module_fallback_used": requested_mode == "focused" and mode == "full_module",
        "source_item_count": len(old_sources),
        "editable_source_items": editable,
        "readonly_context_source_items": readonly,
        "source_item_kinds": dict(sorted(kinds.items())),
        "source_paths": sorted(p for p in paths if p)[:40],
        "source_paths_omitted": max(0, len([p for p in paths if p]) - 40),
        "policy": (
            "focused mode provides edit-scope function slices plus read-only context slices; "
            "full module source is supplied only when focused extraction is empty or when full_module is explicitly requested"
        ),
    }


def _json_size(data: Any) -> int:
    return len(json.dumps(data, ensure_ascii=False))


def _shrink_patch_input_payload(input_payload: dict[str, Any], *, budget_chars: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply last-mile prompt-budget trimming without changing evidence sources.

    The main compactors above keep the payload small for ordinary modules. This
    guard handles broad parent directories by trimming low-priority context and
    recording exactly what was omitted. It deliberately does not invent
    replacement hints or read new target driver code.
    """
    payload = dict(input_payload)
    notes: list[str] = []
    initial_size = _json_size(payload)
    if initial_size <= budget_chars:
        return payload, {"initial_chars": initial_size, "final_chars": initial_size, "budget_chars": budget_chars, "trimmed": False, "notes": []}

    sources = list(payload.get("old_module_sources") or [])
    if len(sources) > 32:
        editable = [x for x in sources if bool(x.get("editable"))]
        readonly = [x for x in sources if not bool(x.get("editable"))]
        payload["old_module_sources"] = editable[:28] + readonly[:4]
        notes.append(f"trimmed old_module_sources {len(sources)} -> {len(payload['old_module_sources'])}")
        sources = list(payload.get("old_module_sources") or [])

    for item in sources:
        content = str(item.get("content", "") or "")
        if len(content) > 6000:
            item["content"] = content[:6000] + "\n...[truncated_by_patch_input_budget]"
    if sources:
        notes.append("capped source block content at 6000 chars")

    # Drop read-only context entirely if the payload is still too large.
    if _json_size(payload) > budget_chars and sources:
        editable = [x for x in sources if bool(x.get("editable"))]
        payload["old_module_sources"] = editable[:24]
        notes.append(f"removed read-only old_module_sources; kept editable={len(payload['old_module_sources'])}")

    final_size = _json_size(payload)
    return payload, {
        "initial_chars": initial_size,
        "final_chars": final_size,
        "budget_chars": budget_chars,
        "trimmed": bool(notes),
        "over_budget_after_trim": final_size > budget_chars,
        "notes": notes,
    }


def _load_function_ranges(extracted_dir: Path) -> dict[str, list[dict[str, Any]]]:
    payload = read_json(extracted_dir / "driver_functions.json", default={}) or {}
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fn in payload.get("functions", []) or []:
        if not isinstance(fn, dict):
            continue
        file_rel = str(fn.get("file", "") or "").strip()
        name = str(fn.get("name", "") or "").strip()
        if not file_rel or not name:
            continue
        try:
            start = int(fn.get("start_line", 0) or 0)
            end = int(fn.get("end_line", start) or start)
        except Exception:
            continue
        if start <= 0 or end < start:
            continue
        by_file[file_rel].append({"function": name, "start_line": start, "end_line": end})
    for funcs in by_file.values():
        funcs.sort(key=lambda x: (int(x["start_line"]), int(x["end_line"])))
    return dict(by_file)


def _parse_touched_function_scopes(patch_text: str, extracted_dir: Path) -> list[dict[str, Any]]:
    ranges = _load_function_ranges(extracted_dir)
    touched: list[dict[str, Any]] = []
    current_file = ""
    hunk_re = re.compile(r"@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    def flush_modified_points(file_rel: str, points: list[int], hunk_start: int, hunk_end: int) -> None:
        if not file_rel or not points:
            return
        matched = False
        for fn in ranges.get(file_rel, []):
            start = int(fn["start_line"])
            end = int(fn["end_line"])
            if not any(start <= p <= end for p in points):
                continue
            touched.append(
                {
                    "file": file_rel,
                    "function": fn["function"],
                    "start_line": start,
                    "end_line": end,
                    "hunk_old_start": hunk_start,
                    "hunk_old_end": hunk_end,
                    "modified_old_lines": sorted(set(points)),
                    "scope_kind": "function",
                }
            )
            matched = True
        if not matched:
            touched.append(
                {
                    "file": file_rel,
                    "function": None,
                    "hunk_old_start": hunk_start,
                    "hunk_old_end": hunk_end,
                    "modified_old_lines": sorted(set(points)),
                    "scope_kind": "global_or_unknown",
                }
            )

    old_line = 0
    hunk_start = 0
    hunk_end = 0
    modified_points: list[int] = []
    last_deleted_old_line: int | None = None

    for line in (patch_text or "").splitlines():
        if line.startswith("diff --git ") or line.startswith("--- "):
            flush_modified_points(current_file, modified_points, hunk_start, hunk_end)
            modified_points = []
            old_line = 0
            hunk_start = 0
            hunk_end = 0
            last_deleted_old_line = None
        if line.startswith("--- a/"):
            current_file = line[len("--- a/") :].strip()
            continue
        if line.startswith("@@") and current_file:
            flush_modified_points(current_file, modified_points, hunk_start, hunk_end)
            modified_points = []
            old_line = 0
            hunk_start = 0
            hunk_end = 0
            last_deleted_old_line = None
            m = hunk_re.search(line)
            if not m:
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "1")
            old_line = old_start
            hunk_start = old_start
            hunk_end = old_start if old_count <= 0 else old_start + old_count - 1
            continue
        if not current_file or not hunk_start:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            modified_points.append(old_line)
            last_deleted_old_line = old_line
            old_line += 1
            continue
        if line.startswith("+") and not line.startswith("+++"):
            # Insertions have no old-side line number. Attribute them to the
            # insertion point in the old file so policy checks still identify
            # the containing function without counting unchanged context. If
            # this is a replacement, keep the inserted line attached to the
            # just-deleted old line instead of drifting into the next function.
            modified_points.append(last_deleted_old_line if last_deleted_old_line is not None else max(hunk_start, old_line))
            continue
        if line.startswith(" "):
            last_deleted_old_line = None
            old_line += 1

    flush_modified_points(current_file, modified_points, hunk_start, hunk_end)

    # de-duplicate repeated hunks in the same function while keeping enough evidence.
    dedup: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int, tuple[int, ...]]] = set()
    for item in touched:
        key = (
            str(item.get("file", "")),
            str(item.get("function", "")),
            int(item.get("hunk_old_start", 0) or 0),
            int(item.get("hunk_old_end", 0) or 0),
            tuple(int(x) for x in item.get("modified_old_lines", []) or []),
        )
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
    return dedup


def _edit_scope_policy_check(
    *,
    patch_text: str,
    extracted_dir: Path,
    impact_graph: dict[str, Any],
    candidate_rationale_text: str = "",
) -> dict[str, Any]:
    required = {_function_key(x) for x in impact_graph.get("edit_required_functions", []) or []}
    candidates = {_function_key(x) for x in impact_graph.get("edit_candidate_functions", []) or []}
    context_only = {
        _function_key(x)
        for x in (impact_graph.get("context_only_functions", []) or impact_graph.get("context_functions", []) or [])
    }
    required.discard(None)
    candidates.discard(None)
    context_only.discard(None)
    editable = set(required) | set(candidates)
    candidate_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for item in impact_graph.get("edit_candidate_functions", []) or []:
        key = _function_key(item)
        if key:
            candidate_meta[key] = item

    touched = _parse_touched_function_scopes(patch_text, extracted_dir)
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rationale_low = str(candidate_rationale_text or "").lower()
    for item in touched:
        if item.get("scope_kind") != "function":
            warnings.append({"reason": "global_or_unknown_hunk", **item})
            continue
        key = (str(item.get("file", "")), str(item.get("function", "")))
        modified_lines = {int(x) for x in item.get("modified_old_lines", []) or []}
        if key not in editable and modified_lines:
            overlap_covered = True
            for point in modified_lines:
                if not any(
                    other is not item
                    and other.get("scope_kind") == "function"
                    and (str(other.get("file", "")), str(other.get("function", ""))) in editable
                    and point in {int(x) for x in other.get("modified_old_lines", []) or []}
                    for other in touched
                ):
                    overlap_covered = False
                    break
            if overlap_covered:
                warnings.append({"reason": "overlapping_function_boundary_covered_by_editable_scope", **item})
                continue
        if key in context_only:
            violations.append({"reason": "context_only_function_modified", **item})
            continue
        if key not in editable:
            violations.append({"reason": "function_outside_edit_scope", **item})
            continue
        if key in candidates:
            meta = candidate_meta.get(key, {})
            fn_low = key[1].lower()
            reason_terms = [str(x).lower() for x in meta.get("candidate_reasons", []) or [] if str(x).strip()]
            evidence_terms = [str(x).lower() for x in meta.get("candidate_evidence", []) or [] if str(x).strip()]
            cites_fn = fn_low in rationale_low
            cites_reason = any(t and t in rationale_low for t in reason_terms)
            cites_evidence = any(t and t[:120] in rationale_low for t in evidence_terms)
            if not (cites_fn and (cites_reason or cites_evidence)):
                violations.append(
                    {
                        "reason": "edit_candidate_modified_without_cited_candidate_evidence",
                        "candidate_reasons": meta.get("candidate_reasons", []),
                        "candidate_evidence": meta.get("candidate_evidence", []),
                        **item,
                    }
                )

    return {
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "touched_function_scopes": touched,
        "allowed_edit_required": sorted([{"file": p, "function": f} for p, f in required], key=lambda x: (x["file"], x["function"])),
        "allowed_edit_candidate": sorted([{"file": p, "function": f} for p, f in candidates], key=lambda x: (x["file"], x["function"])),
    }


def _drop_policy_violation_hunks(patch_text: str, violations: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Remove whole hunks that policy already proved outside editable scope.

    This is intentionally conservative: only scope violations with exact file
    and old hunk coordinates are dropped. If the resulting patch no longer has
    effective changes, callers still reject it.
    """
    drop_keys = {
        (
            str(v.get("file", "")),
            int(v.get("hunk_old_start", 0) or 0),
            int(v.get("hunk_old_end", 0) or 0),
        )
        for v in violations
        if str(v.get("reason", "")) in {"function_outside_edit_scope", "context_only_function_modified"}
    }
    drop_keys.discard(("", 0, 0))
    if not drop_keys:
        return patch_text, []

    hunk_re = re.compile(r"@@\s+-(\d+)(?:,(\d+))?\s+\+\d+(?:,\d+)?\s+@@")
    lines = (patch_text or "").splitlines(keepends=True)
    out: list[str] = []
    removed: list[str] = []
    section_header: list[str] = []
    section_hunks: list[list[str]] = []
    current_file = ""
    current_hunk: list[str] | None = None
    current_hunk_key: tuple[str, int, int] | None = None

    def flush_section() -> None:
        nonlocal section_header, section_hunks, current_hunk, current_hunk_key
        if current_hunk is not None:
            if current_hunk_key in drop_keys:
                removed.append(f"{current_hunk_key[0]}:{current_hunk_key[1]}-{current_hunk_key[2]}")
            else:
                section_hunks.append(current_hunk)
            current_hunk = None
            current_hunk_key = None
        if section_hunks:
            out.extend(section_header)
            for h in section_hunks:
                out.extend(h)
        section_header = []
        section_hunks = []

    for line in lines:
        if line.startswith("diff --git "):
            flush_section()
            section_header = [line]
            current_file = ""
            continue
        if not section_header:
            out.append(line)
            continue
        if line.startswith("--- a/"):
            current_file = line[len("--- a/") :].strip()
            section_header.append(line)
            continue
        if line.startswith("@@"):
            if current_hunk is not None:
                if current_hunk_key in drop_keys:
                    removed.append(f"{current_hunk_key[0]}:{current_hunk_key[1]}-{current_hunk_key[2]}")
                else:
                    section_hunks.append(current_hunk)
            m = hunk_re.search(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2) or "1")
                old_end = old_start if old_count <= 0 else old_start + old_count - 1
                current_hunk_key = (current_file, old_start, old_end)
            else:
                current_hunk_key = None
            current_hunk = [line]
            continue
        if current_hunk is not None:
            current_hunk.append(line)
        else:
            section_header.append(line)

    flush_section()
    pruned = "".join(out)
    return pruned, removed


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


def _selected_candidate(patch_generation: dict[str, Any]) -> dict[str, Any]:
    candidates = patch_generation.get("patch_candidates", []) or []
    if not candidates:
        return {}
    selected_id = patch_generation.get("selected_candidate_id") or candidates[0].get("candidate_id")
    for cand in candidates:
        if cand.get("candidate_id") == selected_id:
            return cand
    return candidates[0]


def _repair_previous_patch_payload(run_dir: Path, patch_generation: dict[str, Any]) -> dict[str, Any]:
    cand = _selected_candidate(patch_generation)
    patch_text = ""
    patch_rel = cand.get("fixed_patch_path") or cand.get("raw_patch_path")
    if patch_rel:
        patch_path = run_dir / str(patch_rel)
        if patch_path.exists():
            patch_text = read_text(patch_path)
    return {
        "selected_candidate_id": patch_generation.get("selected_candidate_id"),
        "parse_status": patch_generation.get("parse_status"),
        "patch_generation_mode": patch_generation.get("patch_generation_mode"),
        "candidate": {
            "candidate_id": cand.get("candidate_id"),
            "strategy": cand.get("strategy"),
            "rationale": truncate_text(str(cand.get("rationale", "") or ""), max_chars=1000),
            "evidence": [truncate_text(str(x), max_chars=240) for x in (cand.get("evidence", []) or [])[:8]],
            "open_questions": [truncate_text(str(x), max_chars=240) for x in (cand.get("open_questions", []) or [])[:8]],
            "touched_files": cand.get("touched_files", []),
            "line_fix_ok": cand.get("line_fix_ok"),
            "line_fix_messages": cand.get("line_fix_messages", [])[:8],
            "no_op_candidate": cand.get("no_op_candidate"),
            "policy_fallback": cand.get("policy_fallback"),
        },
        "patch_diff": truncate_text(patch_text, max_chars=24000),
    }


def _should_emit_auto_noop(
    *,
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
) -> bool:
    # A deterministic no-op is valid only when no hard requirement and no
    # strong replacement obligation exists. Medium semantic guidance is kept as
    # context/evidence, not a required edit.
    return not _requires_effective_patch(compatibility_obligations)


def _requires_effective_patch(compatibility_obligations: dict[str, Any]) -> bool:
    for item in compatibility_obligations.get("obligations", []) or []:
        if item.get("hard_checks"):
            return True
        if item.get("replacement_candidates") and str(item.get("semantic_strength", "")).lower() == "strong":
            return True
    for item in compatibility_obligations.get("semantic_obligations", []):
        if str(item.get("strength", "")).lower() == "strong" and item.get("replacement_candidates"):
            return True
    return False


def _required_symbol_keys(compatibility_obligations: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in compatibility_obligations.get("obligations", []) or []:
        if item.get("hard_checks") or str(item.get("semantic_strength", "")).lower() == "strong":
            sym = str(item.get("affected_kernel_symbol", "")).strip()
            if sym:
                keys.add(sym)
    for item in compatibility_obligations.get("semantic_obligations", []) or []:
        if str(item.get("strength", "")).lower() == "strong":
            sym = str(item.get("affected_kernel_symbol", "")).strip()
            if sym:
                keys.add(sym)
    return keys


def _filter_impact_graph_for_symbols(impact_graph: dict[str, Any], symbol_keys: set[str]) -> dict[str, Any]:
    if not symbol_keys:
        return impact_graph

    def keep(item: dict[str, Any]) -> bool:
        uses = {str(x) for x in item.get("uses_changed_kernel_symbols", []) or []}
        return bool(uses.intersection(symbol_keys))

    out = dict(impact_graph)
    for key in [
        "affected_functions",
        "edit_required_functions",
        "edit_candidate_functions",
        "context_only_functions",
        "context_functions",
    ]:
        out[key] = [x for x in impact_graph.get(key, []) or [] if keep(x)]

    kept_functions = {str(x.get("function", "")) for x in out.get("affected_functions", []) or []}
    out["edges"] = [
        e
        for e in impact_graph.get("edges", []) or []
        if str(e.get("from", "")) in kept_functions
        or str(e.get("to", "")) in kept_functions
        or str(e.get("to", "")) in symbol_keys
    ]
    out["llm_payload_filter"] = "hard/strong required symbols only; full graph remains in extracted/impact_graph.json"
    return out


def _slim_kernel_definition_diffs(kernel_definition_diffs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep baseline kernel evidence compact and evidence-only."""
    payload = kernel_definition_diffs or {}
    items_out: list[dict[str, Any]] = []
    for item in payload.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        old_locations = item.get("old_locations", []) or []
        new_locations = item.get("new_locations", []) or []
        change_type = str(item.get("change_type_guess", "") or "unknown")
        has_signal = bool(
            item.get("definition_changed")
            or item.get("migration_signal")
            or (old_locations and not new_locations)
            or (new_locations and not old_locations)
            or change_type not in {"", "unknown"}
        )
        if not has_signal:
            continue

        commits_out: list[dict[str, Any]] = []
        for commit in item.get("commits", []) or []:
            if not isinstance(commit, dict):
                continue
            commits_out.append(
                {
                    "hash": commit.get("hash", ""),
                    "subject": commit.get("subject", ""),
                    "message": truncate_text(str(commit.get("message", "") or ""), max_chars=1200),
                    "changed_files": commit.get("changed_files", []) or [],
                    "diff_excerpt": truncate_text(str(commit.get("diff_excerpt", "") or ""), max_chars=2500),
                }
            )

        items_out.append(
            {
                "symbol": item.get("symbol", ""),
                "kind": item.get("kind", ""),
                "used_by_driver": item.get("used_by_driver", []) or [],
                "old_locations": old_locations,
                "new_locations": new_locations,
                "old_definition": truncate_text(str(item.get("old_definition", "") or ""), max_chars=2000),
                "new_definition": truncate_text(str(item.get("new_definition", "") or ""), max_chars=2000),
                "definition_changed": bool(item.get("definition_changed")),
                "definition_diff": truncate_text(str(item.get("definition_diff", "") or ""), max_chars=4000),
                "change_type_guess": change_type,
                "migration_signal": item.get("migration_signal"),
                "replacement_candidates": item.get("replacement_candidates", []) or [],
                "commits": commits_out[:8],
                "confidence": item.get("confidence", ""),
                "open_questions": item.get("open_questions", []) or [],
            }
        )

    return {
        "affected_func_signal_mode": payload.get("affected_func_signal_mode", ""),
        "commit_exclude_prefixes": payload.get("commit_exclude_prefixes", []) or [],
        "semantic_obligation_table": payload.get("semantic_obligation_table", ""),
        "items": items_out,
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
    prompt_strength: str = "normal",
    source_mode: str = "full_module",
    source_with_line_numbers: bool = True,
    patch_generation_mode: str = "agent",
    old_ref: str | None = None,
    new_ref: str | None = None,
    kernel_definition_diffs: dict[str, Any] | None = None,
    repair_iteration: int | None = None,
    repair_context: dict[str, Any] | None = None,
    previous_patch_generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_repair = repair_context is not None
    stage_dir = llm_calls_dir / (
        f"03_patch_repair_iter_{int(repair_iteration or 0):02d}" if is_repair else "03_patch_generation"
    )
    stage_dir.mkdir(parents=True, exist_ok=True)
    patch_runs_dir = run_dir / "patch_runs"
    patch_runs_dir.mkdir(parents=True, exist_ok=True)

    generation_mode = str(patch_generation_mode or "agent").strip().lower()
    if generation_mode not in {"agent", "baseline_source_only", "baseline_kernel_evidence"}:
        raise ValueError(
            f"unsupported patch_generation_mode={patch_generation_mode!r}; "
            "expected one of: agent, baseline_source_only, baseline_kernel_evidence"
        )

    requested_source_mode = str(source_mode or "focused").strip().lower()
    mode = requested_source_mode
    if mode not in {"full_module", "focused"}:
        mode = "focused"
        requested_source_mode = "focused"
    if generation_mode != "agent":
        # Baselines intentionally receive full old-module source only; no affected graph is used
        # to choose or narrow edit scope.
        mode = "full_module"
        requested_source_mode = "full_module"

    deterministic_noop = (
        not is_repair
        and generation_mode == "agent"
        and _should_emit_auto_noop(
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
    )
    )
    required_symbol_keys = _required_symbol_keys(compatibility_obligations) if generation_mode == "agent" else set()
    llm_impact_graph = (
        _filter_impact_graph_for_symbols(impact_graph, required_symbol_keys)
        if generation_mode == "agent" and not deterministic_noop
        else impact_graph
    )

    source_payload_summary_override: dict[str, Any] | None = None
    if deterministic_noop:
        # No LLM patch-generation call is needed when there are no obligations
        # and no affected functions. Do not load full module source as a
        # fallback here; doing so misrepresents what the patch agent consumed.
        old_sources: list[dict[str, Any]] = []
        mode = requested_source_mode if requested_source_mode in {"focused", "full_module"} else "focused"
        source_payload_summary_override = {
            "requested_source_mode": requested_source_mode,
            "actual_source_mode": "none",
            "full_module_fallback_used": False,
            "source_item_count": 0,
            "editable_source_items": 0,
            "readonly_context_source_items": 0,
            "source_item_kinds": {},
            "source_paths": [],
            "reason": "deterministic_noop_short_circuit_without_llm_source_payload",
        }
    elif mode == "focused":
        old_sources = _build_focused_old_sources(
            extracted_dir=extracted_dir,
            module_path=module_path,
            impact_graph=llm_impact_graph,
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

    if is_repair:
        diagnostic_sources = _load_repair_diagnostic_sources(
            extracted_dir=extracted_dir,
            module_path=module_path,
            repair_context=repair_context or {},
            with_line_numbers=bool(source_with_line_numbers),
        )
        if diagnostic_sources:
            seen_source_keys = {
                (str(item.get("path", "")), str(item.get("source_item_kind", "")))
                for item in old_sources
            }
            for item in diagnostic_sources:
                key = (str(item.get("path", "")), str(item.get("source_item_kind", "")))
                if key not in seen_source_keys:
                    old_sources.append(item)
                    seen_source_keys.add(key)

    if generation_mode == "agent":
        if deterministic_noop:
            input_payload = {
                "case_id": case_id,
                "module_path": module_path,
                "max_candidates": max_candidates,
                "patch_generation_mode": generation_mode,
                "patch_source_mode": "none",
                "patch_source_with_line_numbers": bool(source_with_line_numbers),
                "source_payload_summary": source_payload_summary_override,
                "compatibility_obligations": {
                    "obligation_count": len(compatibility_obligations.get("obligations", []) or []),
                    "semantic_obligation_count": len(compatibility_obligations.get("semantic_obligations", []) or []),
                    "required_obligation_count": 0,
                    "llm_payload_filter": "full obligations remain in extracted/compatibility_obligations.json",
                },
                "impact_graph": {
                    "tier_counts": impact_graph.get("tier_counts", {}),
                    "llm_payload_filter": "omitted because no required migration edit exists",
                },
                "edit_scope_policy": {
                    "rule_summary": "no editable target is needed because no hard or strong migration edit exists"
                },
                "pre_reasoning": {
                    "overall_verdict": pre_reasoning.get("overall_verdict"),
                    "llm_payload_filter": "omitted because no required migration edit exists",
                },
                "context_packet": {
                    "kernel_change_summary": context_packet.get("kernel_change_summary", ""),
                    "llm_payload_filter": "omitted because no required migration edit exists",
                },
                "old_module_sources": [],
                "instruction": "No hard or strong semantic migration edit exists; emit deterministic no-op candidate.",
            }
        else:
            input_payload = {
                "case_id": case_id,
                "module_path": module_path,
                "max_candidates": max_candidates,
                "patch_generation_mode": generation_mode,
                "patch_source_mode": mode,
                "patch_source_with_line_numbers": bool(source_with_line_numbers),
                "source_payload_summary": source_payload_summary_override or _source_payload_summary(
                    mode=mode,
                    requested_mode=requested_source_mode,
                    old_sources=old_sources,
                ),
                "target_edit_checklist": _target_edit_checklist(compatibility_obligations),
                "compatibility_obligations": _compact_agent_obligations(compatibility_obligations),
                "impact_graph": {
                    "tier_counts": llm_impact_graph.get("tier_counts", {}),
                    "edit_scope": _edit_scope_payload(llm_impact_graph),
                    "llm_payload_filter": "compact impact graph; full graph remains in extracted/impact_graph.json",
                },
                "edit_scope_policy": _edit_scope_payload(llm_impact_graph),
                "pre_reasoning": _compact_pre_reasoning(pre_reasoning),
                "context_packet": _compact_context_packet_for_patch(context_packet),
                "old_module_sources": old_sources,
                "instruction": "Generate unified diff patch candidate(s) for migration. Keep patch minimal and evidence-grounded.",
            }
    else:
        input_payload = {
            "case_id": case_id,
            "module_path": module_path,
            "old_ref": old_ref,
            "new_ref": new_ref,
            "max_candidates": max_candidates,
            "patch_generation_mode": generation_mode,
            "patch_source_mode": mode,
            "patch_source_with_line_numbers": bool(source_with_line_numbers),
            "source_payload_summary": _source_payload_summary(
                mode=mode,
                requested_mode=requested_source_mode,
                old_sources=old_sources,
            ),
            "old_module_sources": old_sources,
            "instruction": "Generate baseline patch candidate(s). Use only fields present in this baseline input.",
        }
        if generation_mode == "baseline_kernel_evidence":
            input_payload["kernel_definition_diffs"] = _slim_kernel_definition_diffs(kernel_definition_diffs)
            relevant_patch_path = extracted_dir / "kernel_relevant_diff.patch"
            input_payload["kernel_relevant_diff"] = (
                truncate_text(read_text(relevant_patch_path), max_chars=16000)
                if relevant_patch_path.exists()
                else ""
            )

    if is_repair:
        input_payload = {
            **input_payload,
            "stage": "patch_repair",
            "repair_iteration": int(repair_iteration or 0),
            "repair_policy": {
                "patch_diff_contract": (
                    "Return a complete replacement unified diff against the original old target module source. "
                    "Do not return an incremental diff against the previously failed patch."
                ),
                "input_boundary": (
                    "Repair may use old target module source, kernel-side evidence, generated obligations/specs, "
                    "and validation diagnostics only. Target driver new-version diff remains evaluation-only and is absent."
                ),
            },
            "previous_patch_generation": _repair_previous_patch_payload(
                run_dir,
                previous_patch_generation or {},
            ),
            "validation_feedback": repair_context or {},
            "instruction": (
                "Repair the previous patch. Produce one complete replacement unified diff that applies to the "
                "original old module source and addresses the validation failure."
            ),
        }

    budget_info: dict[str, Any] = {}
    if generation_mode == "agent":
        input_payload, budget_info = _shrink_patch_input_payload(
            input_payload,
            budget_chars=DEFAULT_PATCH_INPUT_BUDGET_CHARS,
        )
        summary = input_payload.get("source_payload_summary")
        if isinstance(summary, dict):
            summary["patch_input_budget"] = budget_info
        else:
            input_payload["patch_input_budget"] = budget_info

    strict_mode = str(prompt_strength or "normal").strip().lower() == "strict"
    if is_repair:
        prompt = build_patch_repair_prompt(
            input_payload,
            strict_mode=True,
            source_mode=mode,
            source_with_line_numbers=bool(source_with_line_numbers),
        )
    elif generation_mode == "baseline_source_only":
        prompt = build_baseline_patch_generation_prompt(
            input_payload,
            baseline_mode="source_only",
            strict_mode=strict_mode,
            source_with_line_numbers=bool(source_with_line_numbers),
        )
    elif generation_mode == "baseline_kernel_evidence":
        prompt = build_baseline_patch_generation_prompt(
            input_payload,
            baseline_mode="kernel_evidence",
            strict_mode=strict_mode,
            source_with_line_numbers=bool(source_with_line_numbers),
        )
    else:
        prompt = build_patch_generation_prompt(
            input_payload,
            strict_mode=strict_mode,
            source_mode=mode,
            source_with_line_numbers=bool(source_with_line_numbers),
        )
    default_output = {
        "case_id": case_id,
        "stage": "patch_repair" if is_repair else "patch_generation",
        "patch_candidates": [],
        "module_level_notes": ["dry-run or no-provider placeholder"],
    }

    input_budget_exceeded = bool(budget_info.get("over_budget_after_trim"))

    if deterministic_noop:
        write_text(stage_dir / "prompt.md", prompt)
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "raw_output.txt", "")
        llm_output = {
            "case_id": case_id,
            "stage": "patch_generation",
            "patch_candidates": [],
            "module_level_notes": [
                "deterministic_noop_short_circuit: no hard or strong semantic migration edit is required"
            ],
            "parse_status": "skipped_auto_noop",
        }
        write_json(stage_dir / "parsed_output.json", llm_output)
    elif input_budget_exceeded:
        write_text(stage_dir / "prompt.md", prompt)
        write_json(stage_dir / "input.json", input_payload)
        write_text(stage_dir / "raw_output.txt", "")
        llm_output = {
            "case_id": case_id,
            "stage": "patch_repair" if is_repair else "patch_generation",
            "patch_candidates": [],
            "module_level_notes": [
                "input_budget_exceeded: patch-generation prompt remained over budget after conservative trimming",
                f"budget_info={budget_info}",
            ],
            "parse_status": "input_budget_exceeded",
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
    effective_patch_required = _requires_effective_patch(compatibility_obligations) if generation_mode == "agent" else False
    strong_semantic_required = effective_patch_required

    for idx, cand in enumerate(raw_candidates[:max_candidates], start=1):
        raw_cid = cand.get("candidate_id") or f"CAND-{idx:03d}"
        cid = f"REPAIR-{int(repair_iteration or 0):02d}-{raw_cid}" if is_repair else raw_cid
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
            if effective_patch_required:
                fix_msgs.append("rejected_noop_due_to_required_migration_obligation")
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
                        "reason": "required_migration_obligation_disallows_noop_candidate",
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
            # Do not fall back to raw LLM text: it may contain malformed hunk
            # fragments that the line fixer already rebuilt into a syntactic
            # patch. Keep the rebuilt text for validation and surface
            # line_fix_ok=false in the report.
            candidate_patch = fixed_text if fixed_text else raw_diff
            fix_msgs.append("line_fix_failed; using_rebuilt_patch_text_for_validation")

        scope_check = (
            _edit_scope_policy_check(
                patch_text=candidate_patch,
                extracted_dir=extracted_dir,
                impact_graph=impact_graph,
                candidate_rationale_text=" ".join(
                    [
                        str(cand.get("strategy", "") or ""),
                        str(cand.get("rationale", "") or ""),
                        " ".join(str(x) for x in cand.get("evidence", []) or []),
                    ]
                ),
            )
            if generation_mode == "agent" and candidate_patch
            else {"ok": True, "violations": [], "warnings": [], "touched_function_scopes": []}
        )
        if not scope_check.get("ok", True):
            pruned_patch, removed_hunks = _drop_policy_violation_hunks(
                candidate_patch,
                list(scope_check.get("violations", []) or []),
            )
            if removed_hunks and _has_effective_changes(pruned_patch):
                pruned_touched = _list_touched_files_from_patch(pruned_patch)
                pruned_file_map = _collect_file_contents(extracted_dir, pruned_touched)
                pruned_fixed_ok, pruned_fixed_text, pruned_fix_msgs = fix_patch_line_numbers(
                    pruned_patch,
                    file_contents=pruned_file_map,
                )
                pruned_candidate_patch = pruned_fixed_text if pruned_fixed_text else pruned_patch
                pruned_candidate_patch, pruned_sem_msgs = _normalize_patch_semantics(pruned_candidate_patch)
                pruned_fix_msgs.extend(pruned_sem_msgs)
                pruned_scope_check = _edit_scope_policy_check(
                    patch_text=pruned_candidate_patch,
                    extracted_dir=extracted_dir,
                    impact_graph=impact_graph,
                    candidate_rationale_text=" ".join(
                        [
                            str(cand.get("strategy", "") or ""),
                            str(cand.get("rationale", "") or ""),
                            " ".join(str(x) for x in cand.get("evidence", []) or []),
                        ]
                    ),
                )
                if pruned_fixed_ok and pruned_scope_check.get("ok", True):
                    fix_msgs.append("pruned_edit_scope_violation_hunks:" + ",".join(removed_hunks))
                    fix_msgs.extend(pruned_fix_msgs)
                    candidate_patch = pruned_candidate_patch
                    touched = pruned_touched
                    fixed_ok = pruned_fixed_ok
                    scope_check = pruned_scope_check

        if not scope_check.get("ok", True):
            fix_msgs.append("rejected_by_edit_scope_policy")
            fix_report = {
                "candidate_id": cid,
                "raw_patch_nonempty": bool(raw_diff.strip()),
                "line_fix_ok": fixed_ok,
                "messages": fix_msgs,
                "touched_files": touched,
                "no_op_candidate": False,
                "rejected_by_policy": True,
                "edit_scope_policy_check": scope_check,
            }
            write_json(cdir / "patch_fix_report.json", fix_report)
            policy_rejections.append(
                {
                    "candidate_id": cid,
                    "reason": "edit_scope_policy_violation",
                    "violations": scope_check.get("violations", []),
                }
            )
            continue

        fixed_path = cdir / "02_fixed_patch.diff"
        write_text(fixed_path, candidate_patch)

        fix_report = {
            "candidate_id": cid,
            "raw_patch_nonempty": bool(raw_diff.strip()),
            "line_fix_ok": fixed_ok,
            "messages": fix_msgs,
            "touched_files": touched,
            "no_op_candidate": no_op_candidate,
            "edit_scope_policy_check": scope_check,
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
                "edit_scope_policy_check": scope_check,
            }
        )

    if not normalized and generation_mode == "agent":
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
                if effective_patch_required:
                    fix_msgs.append("rejected_noop_due_to_required_migration_obligation")
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
                            "reason": "required_migration_obligation_disallows_noop_candidate",
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
                candidate_patch = fixed_text if fixed_text else raw_patch
                fix_msgs.append("line_fix_failed; using_rebuilt_patch_text_for_validation")
            if not reject_heur_candidate:
                scope_check = (
                    _edit_scope_policy_check(
                        patch_text=candidate_patch,
                        extracted_dir=extracted_dir,
                        impact_graph=impact_graph,
                        candidate_rationale_text=f"heuristic_fallback {heur.get('reason', '')} pre_reasoning.patch_guidance",
                    )
                    if candidate_patch
                    else {"ok": True, "violations": [], "warnings": [], "touched_function_scopes": []}
                )
                if not scope_check.get("ok", True):
                    pruned_patch, removed_hunks = _drop_policy_violation_hunks(
                        candidate_patch,
                        list(scope_check.get("violations", []) or []),
                    )
                    if removed_hunks and _has_effective_changes(pruned_patch):
                        pruned_touched = _list_touched_files_from_patch(pruned_patch)
                        pruned_file_map = _collect_file_contents(extracted_dir, pruned_touched)
                        pruned_fixed_ok, pruned_fixed_text, pruned_fix_msgs = fix_patch_line_numbers(
                            pruned_patch,
                            file_contents=pruned_file_map,
                        )
                        pruned_candidate_patch = pruned_fixed_text if pruned_fixed_text else pruned_patch
                        pruned_candidate_patch, pruned_sem_msgs = _normalize_patch_semantics(pruned_candidate_patch)
                        pruned_fix_msgs.extend(pruned_sem_msgs)
                        pruned_scope_check = _edit_scope_policy_check(
                            patch_text=pruned_candidate_patch,
                            extracted_dir=extracted_dir,
                            impact_graph=impact_graph,
                            candidate_rationale_text=f"heuristic_fallback {heur.get('reason', '')} pre_reasoning.patch_guidance",
                        )
                        if pruned_fixed_ok and pruned_scope_check.get("ok", True):
                            fix_msgs.append("pruned_edit_scope_violation_hunks:" + ",".join(removed_hunks))
                            fix_msgs.extend(pruned_fix_msgs)
                            candidate_patch = pruned_candidate_patch
                            touched = pruned_touched
                            fixed_ok = pruned_fixed_ok
                            scope_check = pruned_scope_check

                if not scope_check.get("ok", True):
                    fix_msgs.append("rejected_by_edit_scope_policy")
                    fix_report = {
                        "candidate_id": cid,
                        "raw_patch_nonempty": bool(raw_patch.strip()),
                        "line_fix_ok": fixed_ok,
                        "messages": fix_msgs,
                        "touched_files": touched,
                        "fallback_reason": heur.get("reason", ""),
                        "no_op_candidate": False,
                        "rejected_by_policy": True,
                        "edit_scope_policy_check": scope_check,
                    }
                    write_json(cdir / "patch_fix_report.json", fix_report)
                    policy_rejections.append(
                        {
                            "candidate_id": cid,
                            "reason": "edit_scope_policy_violation",
                            "fallback": "heuristic",
                            "violations": scope_check.get("violations", []),
                        }
                    )
                    reject_heur_candidate = True

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
                    "edit_scope_policy_check": scope_check,
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
                        "edit_scope_policy_check": scope_check,
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
                "rationale": "No hard or strong semantic migration edit is required; keep old source unchanged for validation continuity.",
                "evidence": [
                    f"pre_reasoning.overall_verdict={pre_reasoning.get('overall_verdict', '')}",
                    "compatibility_obligations contains no hard checks",
                    "compatibility_obligations contains no strong replacement obligation",
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
        "stage": "patch_repair" if is_repair else "patch_generation",
        "repair_iteration": int(repair_iteration or 0) if is_repair else None,
        "is_repair": bool(is_repair),
        "patch_generation_mode": generation_mode,
        "patch_source_mode": mode,
        "requested_patch_source_mode": requested_source_mode,
        "source_payload_summary": source_payload_summary_override or _source_payload_summary(
            mode=mode,
            requested_mode=requested_source_mode,
            old_sources=old_sources,
        ),
        "patch_source_with_line_numbers": bool(source_with_line_numbers),
        "parse_status": llm_output.get("parse_status", "unknown"),
        "module_level_notes": llm_output.get("module_level_notes", []),
        "patch_candidates": normalized,
        "selected_candidate_id": normalized[0]["candidate_id"] if normalized else None,
        "policy_rejections": policy_rejections,
        "strong_semantic_required": strong_semantic_required,
        "effective_patch_required": effective_patch_required,
    }

    write_json(run_dir / "patch_generation.json", out)
    return out
