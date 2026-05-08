from __future__ import annotations

import difflib
import re
from typing import Any

from .repo_utils import RepositoryAccessor
from .utils import truncate_text, write_json, write_text


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


def run_kernel_diff_builder(
    accessor: RepositoryAccessor,
    extracted_dir,
    locations_payload: dict[str, Any],
    module_path: str,
) -> dict[str, Any]:
    items_out = []
    patch_chunks = []

    for item in locations_payload.get("items", []):
        symbol = item.get("symbol")
        kind = item.get("kind", "unknown")
        context_lines = _context_for_kind(kind)

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

        old_exists = bool(item.get("old_locations"))
        new_exists = bool(item.get("new_locations"))

        definition_changed = False
        if old_exists and not new_exists:
            definition_changed = True
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
        should_collect_commits = bool(definition_changed or (old_exists and not new_exists))
        if should_collect_commits:
            commit_records = accessor.log_s(symbol, max_count=6)
            for rec in commit_records:
                commits.append(accessor.commit_details(rec.commit))

        commit_blob = "\n".join([c.get("subject", "") + "\n" + c.get("message", "") for c in commits])
        blob = "\n".join([diff_text, commit_blob])
        change_type = _guess_change_type(kind, definition_changed, old_exists, new_exists, blob)

        open_questions = []
        if old_exists and not new_exists:
            open_questions.append("old symbol found but missing in new_ref; replacement symbol likely needed")

        for kw in KEYWORDS_PRIORITY:
            if kw in blob.lower():
                open_questions.append(f"high-priority keyword observed: {kw}")

        confidence = _confidence(old_def, new_def, old_exists, new_exists, change_type)

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

    payload = {"items": items_out}
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
