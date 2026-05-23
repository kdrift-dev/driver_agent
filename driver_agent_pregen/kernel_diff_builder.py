from __future__ import annotations

import difflib
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .repo_utils import RepositoryAccessor
from .utils import read_json, truncate_text, write_json, write_text


KEYWORDS_PRIORITY = [
    "deprecated",
    "remove",
    "rename",
    "replace",
    "use instead",
    "signature",
    "return",
    "ownership",
    "refcount",
    "lifetime",
    "lock",
    "callback",
    "probe",
    "pm",
]

MIGRATION_SUBJECT_HINTS = [
    "replace",
    "use instead",
    "deprecated",
    "deprecate",
    "treewide",
    "remove",
    "rename",
]

ALLOC_FAMILY_REPLACEMENTS = {
    "kmalloc": ["kmalloc_obj", "kmalloc_flex"],
    "kzalloc": ["kzalloc_obj", "kzalloc_flex"],
    "kvmalloc": ["kvmalloc_obj"],
    "vzalloc": ["vzalloc_obj"],
    "kmalloc_array": ["kmalloc_objs"],
    "kvmalloc_array": ["kvmalloc_objs"],
    "kcalloc": ["kzalloc_objs"],
    "kvcalloc": ["kvzalloc_objs"],
}

# Hand-written replacement maps are intentionally disabled in the paper-facing
# workflow. Replacement candidates should come from old/new kernel definitions
# or explicitly enabled private/debug evidence, not case-specific hints.
SYMBOL_REPLACEMENT_HINTS: dict[str, list[str]] = {}

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _is_diff_metadata_line(line: str) -> bool:
    return line.startswith(("+++", "---", "diff --git ", "index ", "@@"))


def _choose_location(locations: list[dict], module_path: str) -> dict | None:
    if not locations:
        return None
    for loc in locations:
        path = loc.get("path", "")
        if path and not path.startswith(module_path.rstrip("/") + "/"):
            return loc
    return locations[0]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_signature_line(s: str) -> str:
    text = _norm(s)
    text = re.sub(r"\bextern\s+", "", text)
    text = re.sub(r"\s*;\s*$", ";", text)
    return text


def _strip_lineno_prefix(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        lines.append(re.sub(r"^\s*\d+:\s?", "", line))
    return "\n".join(lines)


def _guess_change_type(kind: str, definition_changed: bool, old_exists: bool, new_exists: bool, blob: str) -> str:
    text = (blob or "").lower()
    if kind == "include":
        return "include_dependency" if (definition_changed or old_exists != new_exists) else "unknown"
    if old_exists and not new_exists:
        if "rename" in text:
            return "rename"
        return "api_deprecation"
    if "signature" in text:
        return "signature_change"
    if kind in {"field", "struct"} and definition_changed:
        return "struct_field_change"
    if "return" in text:
        return "return_value_change"
    if any(k in text for k in {"ownership", "refcount", "lifetime", "free", "alloc", "get", "put"}):
        return "ownership_change"
    if any(k in text for k in {"lock", "unlock", "spin_lock", "mutex"}):
        return "locking_change"
    if definition_changed:
        return "signature_change" if kind == "function" else "unknown"
    return "unknown"


def _confidence(old_def: str, new_def: str, old_exists: bool, new_exists: bool, change_type: str) -> str:
    if old_exists and not new_exists:
        return "high"
    if old_def and new_def and old_def != new_def:
        if change_type in {
            "api_deprecation",
            "rename",
            "signature_change",
            "struct_field_change",
            "return_value_change",
            "ownership_change",
            "locking_change",
            "include_dependency",
        }:
            return "high"
        return "medium"
    if old_exists or new_exists:
        return "medium"
    return "low"


def _confidence_rank(level: str) -> int:
    lv = str(level or "").lower()
    if lv == "high":
        return 3
    if lv == "medium":
        return 2
    if lv == "low":
        return 1
    return 0


def _max_conf(a: str, b: str) -> str:
    return a if _confidence_rank(a) >= _confidence_rank(b) else b


def _strength_rank(level: str) -> int:
    lv = str(level or "").lower()
    if lv == "strong":
        return 3
    if lv == "medium":
        return 2
    if lv == "low":
        return 1
    return 0


def _load_semantic_obligation_table(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {}
    data = read_json(p, default={}) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _table_kernel_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(table.get("kernel_evidence_rows", []) or [])
    if rows:
        return [r for r in rows if isinstance(r, dict)]
    rows = list(table.get("kernel_symbol_rows", []) or [])
    return [r for r in rows if isinstance(r, dict)]


def _lookup_table_rows(
    *,
    table: dict[str, Any],
    old_ref: str | None,
    new_ref: str | None,
    symbol: str,
    kind: str,
) -> list[dict[str, Any]]:
    rows = _table_kernel_rows(table)
    if not rows:
        return []

    idx = table.get("indexes", {}).get("by_ref_symbol", {})
    key = f"{old_ref or ''}|{new_ref or ''}|{symbol}|{kind}"
    id_to_row = {str(r.get("row_id")): r for r in rows if r.get("row_id")}
    if isinstance(idx, dict) and key in idx:
        out = []
        for rid in idx.get(key, []) or []:
            row = id_to_row.get(str(rid))
            if row:
                out.append(row)
        if out:
            return out

    out = []
    for row in rows:
        if str(row.get("symbol", "")) != symbol:
            continue
        if str(row.get("kind", "unknown")) != kind:
            continue
        if old_ref and str(row.get("old_ref", "")) != old_ref:
            continue
        if new_ref and str(row.get("new_ref", "")) != new_ref:
            continue
        out.append(row)
    return out


def _merge_replacement_candidates(
    base: list[dict[str, Any]],
    second: list[dict[str, Any]],
    *,
    second_source: str = "table",
    max_candidates: int = 8,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    def add_one(item: dict[str, Any], source: str) -> None:
        sym = str(item.get("symbol", "")).strip()
        if not sym:
            return
        score = int(item.get("score", 1) or 1)
        ev = [str(x) for x in (item.get("evidence", []) or []) if str(x).strip()]
        if source == "table":
            ev = ["semantic_obligation_table"] + ev
        rec = merged.get(sym)
        if not rec:
            merged[sym] = {
                "symbol": sym,
                "score": score,
                "evidence": dedupe_list(ev)[:8],
            }
            return
        rec["score"] = max(int(rec.get("score", 1) or 1), score)
        rec["evidence"] = dedupe_list(list(rec.get("evidence", [])) + ev)[:8]

    for c in base or []:
        add_one(c, "base")
    for c in second or []:
        add_one(c, second_source)

    out = list(merged.values())
    out.sort(key=lambda x: (-int(x.get("score", 1) or 1), str(x.get("symbol", ""))))
    return out[:max_candidates]


def _table_row_votes(table_hits: list[dict[str, Any]]) -> tuple[bool, str, list[dict[str, Any]], list[str], str]:
    migration = False
    best_change_type = "unknown"
    best_strength = "low"
    replacements: list[dict[str, Any]] = []
    row_ids: list[str] = []

    for hit in table_hits:
        if hit.get("migration_signal") or str(hit.get("semantic_kind", "")) == "source_compatible_semantic_migration":
            migration = True
        ct = str(hit.get("change_type_guess", "unknown"))
        if best_change_type == "unknown" and ct != "unknown":
            best_change_type = ct
        st = str(hit.get("strength", "low"))
        if _strength_rank(st) > _strength_rank(best_strength):
            best_strength = st
        replacements.extend(list(hit.get("replacement_candidates", []) or []))
        rid = str(hit.get("row_id", "")).strip()
        if rid:
            row_ids.append(rid)

    return migration, best_change_type, replacements, dedupe_list(row_ids), best_strength


def _confidence_with_table(base_conf: str, table_hits: list[dict[str, Any]], table_strength: str) -> str:
    out = base_conf
    if any(bool(h.get("definition_changed")) for h in table_hits):
        out = _max_conf(out, "medium")
    if _strength_rank(table_strength) >= _strength_rank("medium"):
        out = _max_conf(out, "medium")
    if _strength_rank(table_strength) >= _strength_rank("strong"):
        out = _max_conf(out, "high")
    return out


def run_kernel_diff_builder(
    accessor: RepositoryAccessor,
    extracted_dir,
    locations_payload: dict[str, Any],
    module_path: str,
    *,
    collect_commits: bool = False,
    affected_func_signal_mode: str = "kernel_only",
    old_ref: str | None = None,
    new_ref: str | None = None,
    semantic_obligation_table: str | None = None,
) -> dict[str, Any]:
    if affected_func_signal_mode not in {"kernel_only", "kernel_plus_other_drivers"}:
        raise ValueError(
            f"unsupported affected_func_signal_mode={affected_func_signal_mode!r}; "
            "expected one of: kernel_only, kernel_plus_other_drivers"
        )
    if (
        affected_func_signal_mode == "kernel_plus_other_drivers"
        and os.environ.get("DRIVER_AGENT_ALLOW_DRIVER_EVIDENCE") != "1"
    ):
        raise ValueError(
            "kernel_plus_other_drivers is disabled by policy; "
            "set DRIVER_AGENT_ALLOW_DRIVER_EVIDENCE=1 only for private/debug experiments."
        )

    exclude_prefixes: list[str] = [module_path]
    if affected_func_signal_mode == "kernel_only":
        # Strict mode: do not use any driver-tree migration evidence.
        exclude_prefixes.append("drivers")
        semantic_obligation_table = None

    semantic_table = _load_semantic_obligation_table(semantic_obligation_table)
    items_out = []
    patch_chunks = []

    for item in locations_payload.get("items", []):
        symbol = item.get("symbol")
        kind = item.get("kind", "unknown")
        context_lines = _context_for_kind(kind)
        table_hits = _lookup_table_rows(
            table=semantic_table,
            old_ref=old_ref,
            new_ref=new_ref,
            symbol=str(symbol or ""),
            kind=str(kind or "unknown"),
        )

        old_loc = _choose_location(item.get("old_locations", []), module_path)
        new_loc = _choose_location(item.get("new_locations", []), module_path)

        old_def = ""
        new_def = ""

        if old_loc:
            old_def = accessor.file_excerpt("old", old_loc["path"], int(old_loc.get("line", 1)), context=context_lines)
        if new_loc:
            new_def = accessor.file_excerpt("new", new_loc["path"], int(new_loc.get("line", 1)), context=context_lines)

        old_def_cmp = _strip_lineno_prefix(old_def)
        new_def_cmp = _strip_lineno_prefix(new_def)
        old_match_cmp = _norm_signature_line(str((old_loc or {}).get("match", "") or ""))
        new_match_cmp = _norm_signature_line(str((new_loc or {}).get("match", "") or ""))

        old_exists = bool(item.get("old_locations"))
        new_exists = bool(item.get("new_locations"))

        definition_changed = False
        if old_exists and not new_exists:
            definition_changed = True
        elif old_match_cmp and new_match_cmp and kind in {"function", "macro", "enum", "global", "unknown"}:
            # Avoid false positives from unrelated nearby context changes in
            # broad excerpts. For single-line symbols, the locator's matched
            # declaration/definition line is the reliable comparison unit.
            definition_changed = old_match_cmp != new_match_cmp
        elif old_def and new_def:
            definition_changed = _norm(old_def_cmp) != _norm(new_def_cmp)
        elif kind == "include" and old_exists != new_exists:
            definition_changed = True

        diff_text = ""
        if old_def or new_def:
            diff_lines = list(
                difflib.unified_diff(
                    old_def_cmp.splitlines(),
                    new_def_cmp.splitlines(),
                    fromfile=f"old:{symbol}",
                    tofile=f"new:{symbol}",
                    lineterm="",
                )
            )
            diff_text = "\n".join(diff_lines)

        commits = []
        should_collect_commits = bool(
            collect_commits
            and (
                definition_changed
                or (old_exists and not new_exists)
                or _likely_migration_symbol(symbol or "", kind)
            )
        )
        if should_collect_commits:
            commit_records = accessor.log_s(
                symbol,
                max_count=6,
                exclude_path_prefixes=exclude_prefixes,
            )
            for rec in commit_records:
                commits.append(accessor.commit_details(rec.commit, exclude_path_prefixes=exclude_prefixes))

        migration_signal, replacement_candidates, migration_reasons = _detect_migration_signal(
            symbol=symbol or "",
            kind=kind,
            definition_changed=definition_changed,
            old_exists=old_exists,
            new_exists=new_exists,
            commits=commits,
        )
        table_migration, table_change_type, table_repls, table_row_ids, table_strength = _table_row_votes(table_hits)
        migration_signal = bool(migration_signal or table_migration)
        replacement_candidates = _merge_replacement_candidates(replacement_candidates, table_repls)

        commit_blob = "\n".join([c.get("subject", "") + "\n" + c.get("message", "") for c in commits])
        change_type = _guess_change_type(kind, definition_changed, old_exists, new_exists, diff_text)
        # Conservative: table should mainly reinforce migration semantics (replacement signals),
        # not broaden generic change-type noise.
        if change_type == "unknown" and table_change_type != "unknown" and (table_migration or bool(table_repls)):
            change_type = table_change_type
        if migration_signal and change_type == "unknown":
            change_type = "api_migration_signal"

        open_questions = []
        if old_exists and not new_exists:
            open_questions.append("old symbol found but missing in new_ref; replacement symbol likely needed")
        if migration_signal:
            open_questions.extend(migration_reasons)
            if replacement_candidates:
                repl = ", ".join(c["symbol"] for c in replacement_candidates[:5])
                open_questions.append(f"candidate replacement symbols from in-tree migrations: {repl}")
        if table_row_ids:
            open_questions.append(f"semantic obligation table hits: {', '.join(table_row_ids[:8])}")

        for kw in KEYWORDS_PRIORITY:
            if kw in f"{diff_text}\n{commit_blob}".lower():
                open_questions.append(f"high-priority keyword observed: {kw}")

        confidence = _confidence(old_def, new_def, old_exists, new_exists, change_type)
        if table_migration or table_repls:
            confidence = _confidence_with_table(confidence, table_hits, table_strength)

        out_item = {
            "symbol": symbol,
            "kind": kind,
            "used_by_driver": item.get("used_by_driver", []),
            "old_definition": old_def,
            "new_definition": new_def,
            "definition_changed": definition_changed,
            "definition_diff": truncate_text(diff_text, max_chars=12000),
            "change_type_guess": change_type,
            "commits": commits,
            "migration_signal": migration_signal,
            "replacement_candidates": replacement_candidates,
            "semantic_table_hit_rows": table_row_ids,
            "confidence": confidence,
            "open_questions": dedupe_list(open_questions),
        }
        items_out.append(out_item)

        if definition_changed or (old_exists and not new_exists):
            chunk = [
                f"### symbol: {symbol} ({kind})",
                f"change_type_guess: {change_type}",
                "",
                out_item["definition_diff"] or "(no textual diff excerpt)",
                "",
            ]
            patch_chunks.append("\n".join(chunk))

    payload = {
        "affected_func_signal_mode": affected_func_signal_mode,
        "collect_commit_info": bool(collect_commits),
        "commit_info_policy": (
            "enabled_explicitly_with_excluded_paths" if collect_commits else "disabled_by_default"
        ),
        "commit_exclude_prefixes": exclude_prefixes,
        "semantic_obligation_table": semantic_obligation_table,
        "items": items_out,
    }
    write_json(extracted_dir / "kernel_definition_diffs.json", payload)
    write_text(extracted_dir / "kernel_relevant_diff.patch", "\n\n".join(patch_chunks) + ("\n" if patch_chunks else ""))
    return payload


def dedupe_list(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _context_for_kind(kind: str) -> int:
    if kind == "function":
        return 4
    if kind in {"macro", "enum", "field", "struct", "global"}:
        return 8
    if kind == "include":
        return 20
    return 8


def _likely_migration_symbol(symbol: str, kind: str) -> bool:
    if kind not in {"function", "macro", "global", "unknown"}:
        return False
    low = (symbol or "").lower()
    if "alloc" in low:
        return True
    if "bridge" in low:
        return True
    if "workqueue" in low or low.endswith("_wq") or "_wq_" in low:
        return True
    if low in {"system_wq", "system_freezable_wq", "system_highpri_wq"}:
        return True
    if low.endswith("_wq"):
        return True
    if low in SYMBOL_REPLACEMENT_HINTS:
        return True
    return False


def _allow_synthetic_replacement_hints() -> bool:
    return os.environ.get("DRIVER_AGENT_ALLOW_SYNTHETIC_REPLACEMENT_HINTS") == "1"


def _is_plausible_replacement(old_symbol: str, new_symbol: str) -> bool:
    if not old_symbol or not new_symbol or old_symbol == new_symbol:
        return False
    old = old_symbol.lower()
    new = new_symbol.lower()

    hints = SYMBOL_REPLACEMENT_HINTS.get(old, [])
    if new in hints:
        return True

    if old in new or new in old:
        return True

    # allocator-family migration (e.g., kcalloc -> kzalloc_objs)
    if "alloc" in old and "alloc" in new:
        if new.endswith(("_obj", "_objs", "_flex")):
            return True

    old_tokens = [t for t in old.split("_") if t]
    new_tokens = [t for t in new.split("_") if t]
    if not old_tokens or not new_tokens:
        return False

    overlap = len(set(old_tokens) & set(new_tokens))
    min_len = min(len(old_tokens), len(new_tokens))
    if overlap >= max(1, min_len - 1):
        return True

    # Prefix/suffix family relation (e.g., system_wq -> system_percpu_wq).
    if old_tokens[0] == new_tokens[0]:
        return True
    if old_tokens[-1] == new_tokens[-1]:
        return True
    return False


def _collect_replacement_candidates(symbol: str, commits: list[dict[str, Any]], max_candidates: int = 6) -> list[dict[str, Any]]:
    old_pat = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}
    skip_tokens = {"if", "for", "while", "switch", "return", "sizeof"}

    for commit in commits:
        diff_text = commit.get("diff_excerpt", "")
        if not diff_text:
            continue
        lines = diff_text.splitlines()
        for i, line in enumerate(lines):
            if _is_diff_metadata_line(line):
                continue
            if not line.startswith("-"):
                continue
            old_line = line[1:]
            if not old_pat.search(old_line):
                continue

            # Search nearby added lines in the same hunk for replacement calls.
            start = max(0, i - 4)
            end = min(len(lines), i + 5)
            for cand_line in lines[start:end]:
                if _is_diff_metadata_line(cand_line):
                    continue
                if not cand_line.startswith("+"):
                    continue
                text = cand_line[1:]
                for m in _CALL_RE.finditer(text):
                    cand = m.group(1)
                    if cand == symbol or cand in skip_tokens:
                        continue
                    if not _is_plausible_replacement(symbol, cand):
                        continue
                    counts[cand] += 1
                    evidence.setdefault(cand, []).append(
                        f"{commit.get('hash', '')[:12]}:{text.strip()[:180]}"
                    )

    out: list[dict[str, Any]] = []
    for cand, score in counts.most_common(max_candidates):
        out.append(
            {
                "symbol": cand,
                "score": int(score),
                "evidence": evidence.get(cand, [])[:6],
            }
        )
    return out


def _collect_noncall_replacement_candidates(symbol: str, commits: list[dict[str, Any]], max_candidates: int = 6) -> list[dict[str, Any]]:
    old_tok = re.compile(rf"\b{re.escape(symbol)}\b")
    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}
    skip_tokens = {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "struct",
        "static",
        "const",
        "unsigned",
        "int",
        "long",
        "void",
    }

    for commit in commits:
        diff_text = commit.get("diff_excerpt", "")
        if not diff_text:
            continue
        lines = diff_text.splitlines()
        for i, line in enumerate(lines):
            if _is_diff_metadata_line(line):
                continue
            if not line.startswith("-"):
                continue
            old_line = line[1:]
            if not old_tok.search(old_line):
                continue

            start = max(0, i - 6)
            end = min(len(lines), i + 7)
            for cand_line in lines[start:end]:
                if _is_diff_metadata_line(cand_line):
                    continue
                if not cand_line.startswith("+"):
                    continue
                text = cand_line[1:]
                for cand in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text):
                    if cand == symbol or cand in skip_tokens:
                        continue
                    if not _is_plausible_replacement(symbol, cand):
                        continue
                    counts[cand] += 1
                    evidence.setdefault(cand, []).append(f"{commit.get('hash', '')[:12]}:{text.strip()[:180]}")

    out: list[dict[str, Any]] = []
    for cand, score in counts.most_common(max_candidates):
        out.append(
            {
                "symbol": cand,
                "score": int(score),
                "evidence": evidence.get(cand, [])[:6],
            }
        )
    return out


def _add_synthetic_allocator_candidates(
    *,
    candidates: list[dict[str, Any]],
    symbol: str,
    commit: dict[str, Any],
) -> list[dict[str, Any]]:
    low_sym = (symbol or "").strip().lower()
    repls = ALLOC_FAMILY_REPLACEMENTS.get(low_sym, [])
    if not repls:
        return candidates

    existing = {str(c.get("symbol", "")).strip() for c in candidates if str(c.get("symbol", "")).strip()}
    out = list(candidates)
    for repl in repls:
        if repl in existing:
            continue
        out.append(
            {
                "symbol": repl,
                "score": 1,
                "evidence": [
                    f"{commit.get('hash', '')[:12]}:{(commit.get('subject', '') or '').strip()[:180]}",
                    "synthetic_candidate_from_kernel_side_coccinelle_allocator_migration",
                ],
            }
        )
    return out


def _add_symbol_hint_candidates(
    *,
    candidates: list[dict[str, Any]],
    symbol: str,
    commits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    low_sym = (symbol or "").strip().lower()
    hints = SYMBOL_REPLACEMENT_HINTS.get(low_sym, [])
    if not hints:
        return candidates

    blob = "\n".join(
        f"{c.get('subject', '')}\n{c.get('message', '')}\n{c.get('diff_excerpt', '')}" for c in commits
    ).lower()
    if low_sym not in blob:
        return candidates

    existing = {str(c.get("symbol", "")).strip() for c in candidates if str(c.get("symbol", "")).strip()}
    out = list(candidates)
    for repl in hints:
        if repl in existing:
            continue
        if repl.lower() not in blob:
            continue
        out.append(
            {
                "symbol": repl,
                "score": 1,
                "evidence": [
                    "synthetic_candidate_from_symbol_replacement_hints",
                    f"symbol_hint:{low_sym}->{repl}",
                ],
            }
        )
    return out


def _detect_migration_signal(
    *,
    symbol: str,
    kind: str,
    definition_changed: bool,
    old_exists: bool,
    new_exists: bool,
    commits: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    if definition_changed or (old_exists and not new_exists):
        return False, [], []
    if kind not in {"function", "macro", "global", "unknown"}:
        return False, [], []
    if not commits:
        return False, [], []

    reasons: list[str] = []
    subject_blob = " ".join((c.get("subject", "") or "").lower() for c in commits)
    for hint in MIGRATION_SUBJECT_HINTS:
        if hint in subject_blob:
            reasons.append(f"commit subject hint: {hint}")

    candidates = _collect_replacement_candidates(symbol, commits)
    noncall_candidates = _collect_noncall_replacement_candidates(symbol, commits)
    candidates = _merge_replacement_candidates(candidates, noncall_candidates, second_source="base")

    # Synthetic replacement hints are private/debug only. They are not part of
    # the default paper-facing evidence boundary.
    low_sym = (symbol or "").strip().lower()
    if _allow_synthetic_replacement_hints() and low_sym in ALLOC_FAMILY_REPLACEMENTS:
        for c in commits:
            text = f"{c.get('subject', '')}\n{c.get('message', '')}\n{c.get('diff_excerpt', '')}".lower()
            if "kmalloc_objs.cocci" in text or "alloc_obj" in text or "alloc_objs" in text:
                candidates = _add_synthetic_allocator_candidates(candidates=candidates, symbol=low_sym, commit=c)
                reasons.append("allocator-family migration hinted by kernel-side coccinelle script evidence")
                break

    if _allow_synthetic_replacement_hints():
        candidates = _add_symbol_hint_candidates(candidates=candidates, symbol=symbol, commits=commits)

    if candidates:
        reasons.append("replacement-like call additions observed near removed old callsites")

    # Keep this conservative: require concrete replacement candidate evidence.
    signal = bool(candidates)
    if signal and reasons:
        # retain textual context when signal is already grounded by replacements
        pass
    elif not signal:
        reasons = []
    return signal, candidates, reasons
