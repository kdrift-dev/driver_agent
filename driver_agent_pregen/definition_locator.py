from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .repo_utils import RepositoryAccessor
from .utils import write_json


def _symbol_pattern(name: str, kind: str) -> tuple[str, bool, bool]:
    esc = re.escape(name)
    if kind == "function":
        # Broad match first; declaration/definition filtering happens in post-processing.
        return rf"{esc}[[:space:]]*\(", True, False
    if kind == "macro":
        return rf"\b{esc}\b", True, False
    if kind == "struct":
        return rf"\bstruct\s+{esc}\b", True, False
    if kind == "field":
        return rf"(?:->|\.)\s*{esc}\b", True, False
    if kind == "enum":
        return rf"\b{esc}\b", True, False
    if kind == "global":
        return rf"\b{esc}\b", True, False
    if kind == "include":
        return name, False, True
    return rf"\b{esc}\b", True, False


def _confidence(kind: str, old_locs: list[dict], new_locs: list[dict]) -> str:
    if old_locs and new_locs:
        if kind in {"function", "macro", "struct", "enum", "include"}:
            return "high"
        return "medium"
    if old_locs or new_locs:
        return "medium"
    return "low"


def run_definition_locator(
    accessor: RepositoryAccessor,
    extracted_dir,
    driver_used_symbols: dict[str, Any],
    module_path: str | None = None,
) -> dict[str, Any]:
    symbols = driver_used_symbols.get("symbols", [])

    grouped = defaultdict(list)
    for sym in symbols:
        # Only kernel-side candidates.
        if sym.get("is_driver_local"):
            continue
        key = (sym.get("name"), sym.get("kind", "unknown"))
        grouped[key].append(sym)

    items = []
    for (name, kind), uses in sorted(grouped.items()):
        if not name:
            continue
        if kind == "include":
            old_locations = _locate_include_header(accessor, "old", name)
            new_locations = _locate_include_header(accessor, "new", name)
        else:
            pattern, regex, fixed = _symbol_pattern(name, kind)
            if kind == "function":
                include_paths = ["include/*", "arch/*/include/*"]
                old_hits = accessor.search(
                    "old",
                    pattern,
                    regex=regex,
                    fixed=fixed,
                    pathspecs=include_paths,
                    max_hits=120,
                )
                new_hits = accessor.search(
                    "new",
                    pattern,
                    regex=regex,
                    fixed=fixed,
                    pathspecs=include_paths,
                    max_hits=120,
                )
                old_hits = _filter_function_like_hits(old_hits, name)
                new_hits = _filter_function_like_hits(new_hits, name)

                if not old_hits:
                    old_hits = accessor.search(
                        "old",
                        pattern,
                        regex=regex,
                        fixed=fixed,
                        pathspecs=_search_pathspecs_for_kind(kind, module_path),
                        max_hits=180,
                    )
                    old_hits = _filter_function_like_hits(old_hits, name)
                if not new_hits:
                    new_hits = accessor.search(
                        "new",
                        pattern,
                        regex=regex,
                        fixed=fixed,
                        pathspecs=_search_pathspecs_for_kind(kind, module_path),
                        max_hits=180,
                    )
                    new_hits = _filter_function_like_hits(new_hits, name)
            else:
                search_paths = _search_pathspecs_for_kind(kind, module_path)
                old_hits = accessor.search(
                    "old",
                    pattern,
                    regex=regex,
                    fixed=fixed,
                    pathspecs=search_paths,
                    max_hits=40,
                )
                new_hits = accessor.search(
                    "new",
                    pattern,
                    regex=regex,
                    fixed=fixed,
                    pathspecs=search_paths,
                    max_hits=40,
                )

            old_locations = [
                {
                    "path": h.path,
                    "line": h.line,
                    "match": h.text.strip()[:500],
                    "locator": h.locator,
                }
                for h in old_hits
            ]
            new_locations = [
                {
                    "path": h.path,
                    "line": h.line,
                    "match": h.text.strip()[:500],
                    "locator": h.locator,
                }
                for h in new_hits
            ]

        if module_path:
            prefix = module_path.rstrip("/") + "/"
            old_locations = [loc for loc in old_locations if not str(loc.get("path", "")).startswith(prefix)]
            new_locations = [loc for loc in new_locations if not str(loc.get("path", "")).startswith(prefix)]

        used_by_driver = [
            {
                "file": u.get("file"),
                "line": u.get("line"),
                "enclosing_function": u.get("enclosing_function"),
                "code": u.get("code"),
            }
            for u in uses
        ]

        notes = []
        if not old_locations:
            notes.append("symbol not found in old kernel-side search")
        if not new_locations:
            notes.append("symbol not found in new kernel-side search")
        if kind == "include":
            notes.append("include provider may be indirect; locations are best-effort")

        items.append(
            {
                "symbol": name,
                "kind": kind,
                "used_by_driver": used_by_driver,
                "old_locations": old_locations,
                "new_locations": new_locations,
                "classification_confidence": _confidence(kind, old_locations, new_locations),
                "notes": notes,
            }
        )

    payload = {"items": items}
    write_json(extracted_dir / "kernel_definition_locations.json", payload)
    return payload


def _locate_include_header(accessor: RepositoryAccessor, side: str, include_name: str) -> list[dict]:
    token = include_name.strip().strip("<>\"")
    candidates = [token]
    if not token.startswith(("include/", "arch/")):
        candidates.extend([f"include/{token}", f"uapi/{token}"])

    locations = []
    for path in candidates:
        if accessor.path_exists(side, path):
            locations.append(
                {
                    "path": path,
                    "line": 1,
                    "match": f"header exists: {path}",
                    "locator": "regex",
                }
            )
            break

    if locations:
        return locations

    # Fallback to constrained search in include trees only.
    pat = rf"#\s*include\s*[<\"]{re.escape(token)}[>\"]"
    hits = accessor.search(side, pat, regex=True, fixed=False, pathspecs=["include/*", "arch/*/include/*"], max_hits=8)
    for h in hits:
        locations.append(
            {
                "path": h.path,
                "line": h.line,
                "match": h.text.strip()[:500],
                "locator": h.locator,
            }
        )
    return locations


def _search_pathspecs_for_kind(kind: str, module_path: str | None = None) -> list[str]:
    module_family = _module_family_pathspec(module_path)
    if kind in {"enum", "macro", "field", "struct", "global", "unknown"}:
        out = ["include/*", "arch/*/include/*"]
        if module_family:
            out.append(module_family)
        return out
    if kind == "function":
        out = ["kernel/*", "arch/*", "fs/*", "mm/*", "net/*", "lib/*", "security/*"]
        if module_family:
            out.append(module_family)
        return out
    return []


def _filter_function_like_hits(hits, symbol_name: str):
    out = []
    for h in hits:
        if _looks_like_decl_or_def(h.text, symbol_name):
            out.append(h)
    return out


def _looks_like_decl_or_def(line: str, symbol_name: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if text.startswith("#define"):
        return bool(re.search(rf"^#define\s+{re.escape(symbol_name)}\s*\(", text))
    if text.startswith("#"):
        return False
    if symbol_name not in text:
        return False
    idx = text.find(symbol_name)
    if idx < 0:
        return False
    prefix = text[:idx]

    # Reject common call-site shapes.
    if any(prefix.rstrip().endswith(k) for k in ("return", "if", "for", "while", "switch", "sizeof")):
        return False
    if "->" in prefix or "." in prefix or "=" in prefix:
        return False
    if text.startswith(symbol_name + "("):
        return False
    if "," in prefix and "(" in prefix:
        return False

    # Heuristic: declaration/definition lines typically carry type qualifiers.
    tokens = set(prefix.replace("*", " ").split())
    typeish = {
        "static",
        "inline",
        "extern",
        "const",
        "unsigned",
        "signed",
        "long",
        "short",
        "void",
        "int",
        "char",
        "bool",
        "u8",
        "u16",
        "u32",
        "u64",
        "s8",
        "s16",
        "s32",
        "s64",
        "size_t",
        "ssize_t",
        "struct",
        "__init",
        "__exit",
    }
    if tokens & typeish:
        return True

    # Accept if it still looks like prototype/definition tail.
    return text.endswith(";") or text.endswith("{")


def _module_family_pathspec(module_path: str | None) -> str | None:
    if not module_path:
        return None
    parts = [p for p in module_path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "drivers":
        return f"drivers/{parts[1]}/*"
    if len(parts) >= 1 and parts[0] in {"fs", "net", "mm", "kernel", "arch", "lib"}:
        return f"{parts[0]}/*"
    return None
