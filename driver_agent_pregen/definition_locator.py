from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .repo_utils import RepositoryAccessor
from .utils import write_json


def _symbol_pattern(name: str, kind: str) -> tuple[str, bool, bool]:
    esc = re.escape(name)
    if kind == "function":
        # Broad match first; declaration/definition filtering happens in post-processing.
        return rf"\b{esc}[[:space:]]*\(", True, False
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
    exclude_driver_paths: bool = False,
    jobs: int = 1,
) -> dict[str, Any]:
    symbols = driver_used_symbols.get("symbols", [])

    grouped = defaultdict(list)
    for sym in symbols:
        # Only kernel-side candidates.
        if sym.get("is_driver_local"):
            continue
        key = (sym.get("name"), sym.get("kind", "unknown"))
        grouped[key].append(sym)

    def locate_one(name: str, kind: str, uses: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not name:
            return None
        notes = []
        if kind == "include":
            old_locations = _locate_include_header(accessor, "old", name)
            new_locations = _locate_include_header(accessor, "new", name)
        elif kind == "field":
            owner_structs = sorted(
                {
                    str(u.get("owner_struct", "")).strip()
                    for u in uses
                    if str(u.get("owner_struct", "")).strip()
                }
            )
            if owner_structs:
                old_locations = []
                new_locations = []
                for owner in owner_structs[:4]:
                    old_locations.extend(
                        _locate_struct_definition(
                            accessor,
                            "old",
                            owner,
                            field_name=name,
                            module_path=module_path,
                            exclude_driver_paths=exclude_driver_paths,
                        )
                    )
                    new_locations.extend(
                        _locate_struct_definition(
                            accessor,
                            "new",
                            owner,
                            field_name=name,
                            module_path=module_path,
                            exclude_driver_paths=exclude_driver_paths,
                        )
                    )
            else:
                old_locations = []
                new_locations = []
                notes.append(
                    "field owner struct unknown; skipped unconstrained field definition search"
                )
        else:
            pattern, regex, fixed = _symbol_pattern(name, kind)
            if kind == "function":
                include_paths = ["include/*", "arch/*/include/*"]
                old_macro_hits = accessor.search(
                    "old",
                    rf"^\s*#\s*define\s+{re.escape(name)}\b",
                    regex=True,
                    fixed=False,
                    pathspecs=include_paths,
                    max_hits=40,
                )
                new_macro_hits = accessor.search(
                    "new",
                    rf"^\s*#\s*define\s+{re.escape(name)}\b",
                    regex=True,
                    fixed=False,
                    pathspecs=include_paths,
                    max_hits=40,
                )
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
                old_hits = _dedupe_hits(
                    old_macro_hits + _filter_function_like_hits(accessor, "old", old_hits, name)
                )
                new_hits = _dedupe_hits(
                    new_macro_hits + _filter_function_like_hits(accessor, "new", new_hits, name)
                )
                if not old_hits:
                    old_hits = accessor.search(
                        "old",
                        pattern,
                        regex=regex,
                        fixed=fixed,
                        pathspecs=_search_pathspecs_for_kind(kind, module_path, exclude_driver_paths=exclude_driver_paths),
                        max_hits=180,
                    )
                    old_hits = _filter_function_like_hits(accessor, "old", old_hits, name)
                if not new_hits:
                    new_hits = accessor.search(
                        "new",
                        pattern,
                        regex=regex,
                        fixed=fixed,
                        pathspecs=_search_pathspecs_for_kind(kind, module_path, exclude_driver_paths=exclude_driver_paths),
                        max_hits=180,
                    )
                    new_hits = _filter_function_like_hits(accessor, "new", new_hits, name)
            else:
                search_paths = _search_pathspecs_for_kind(kind, module_path, exclude_driver_paths=exclude_driver_paths)
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
                if kind in {"macro", "enum"}:
                    old_hits = _filter_constant_like_hits(old_hits, name)
                    new_hits = _filter_constant_like_hits(new_hits, name)

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
        if exclude_driver_paths:
            old_locations = [loc for loc in old_locations if not _is_driver_tree_path(str(loc.get("path", "")))]
            new_locations = [loc for loc in new_locations if not _is_driver_tree_path(str(loc.get("path", "")))]

        used_by_driver = [
            {
                "file": u.get("file"),
                "line": u.get("line"),
                "enclosing_function": u.get("enclosing_function"),
                "code": u.get("code"),
                "owner_struct": u.get("owner_struct"),
            }
            for u in uses
        ]

        if not old_locations:
            notes.append("symbol not found in old kernel-side search")
        if not new_locations:
            notes.append("symbol not found in new kernel-side search")
        if kind == "include":
            notes.append("include provider may be indirect; locations are best-effort")
        if exclude_driver_paths:
            notes.append("strict kernel-only locator mode excluded drivers/* definition candidates")

        return {
            "symbol": name,
            "kind": kind,
            "used_by_driver": used_by_driver,
            "old_locations": old_locations,
            "new_locations": new_locations,
            "classification_confidence": _confidence(kind, old_locations, new_locations),
            "notes": notes,
        }

    keys = [(name, kind, uses) for (name, kind), uses in sorted(grouped.items()) if name]
    items = []
    workers = max(1, int(jobs or 1))
    if workers == 1 or len(keys) <= 1:
        for name, kind, uses in keys:
            item = locate_one(str(name), str(kind), uses)
            if item:
                items.append(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(locate_one, str(name), str(kind), uses): (str(name), str(kind))
                for name, kind, uses in keys
            }
            by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for fut in as_completed(futs):
                key = futs[fut]
                item = fut.result()
                if item:
                    by_key[key] = item
            for name, kind, _uses in keys:
                item = by_key.get((str(name), str(kind)))
                if item:
                    items.append(item)

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


def _locate_struct_definition(
    accessor: RepositoryAccessor,
    side: str,
    struct_name: str,
    *,
    field_name: str | None = None,
    module_path: str | None = None,
    exclude_driver_paths: bool = False,
) -> list[dict]:
    pattern = rf"\bstruct\s+{re.escape(struct_name)}\s*\{{"
    hits = accessor.search(
        side,
        pattern,
        regex=True,
        fixed=False,
        pathspecs=["include/*", "arch/*/include/*"]
        + (
            [_module_family_pathspec(module_path)]
            if _module_family_pathspec(module_path) and not exclude_driver_paths
            else []
        ),
        max_hits=24,
    )
    out = []
    for h in hits:
        loc_line = h.line
        loc_match = h.text.strip()[:500]
        field_found = field_name is None
        if field_name:
            try:
                text = accessor.read_file(side, h.path)
                lines = text.splitlines()
                start_idx = max(0, int(h.line or 1) - 1)
                brace_depth = 0
                saw_open = False
                for idx in range(start_idx, min(len(lines), start_idx + 400)):
                    line = lines[idx]
                    if "{" in line:
                        saw_open = True
                        brace_depth += line.count("{")
                    if saw_open and re.search(rf"\b{re.escape(field_name)}\b", line):
                        loc_line = idx + 1
                        decl_lines = [line.rstrip()]
                        for j in range(idx + 1, min(len(lines), idx + 16)):
                            decl_lines.append(lines[j].rstrip())
                            if ";" in lines[j]:
                                break
                        loc_match = "\n".join(x.strip() for x in decl_lines).strip()[:500]
                        field_found = True
                        break
                    if saw_open and "}" in line:
                        brace_depth -= line.count("}")
                        if brace_depth <= 0:
                            break
            except Exception:
                pass
        if field_name and not field_found:
            continue
        out.append(
            {
                "path": h.path,
                "line": loc_line,
                "match": (
                    f"struct {struct_name} definition"
                    + (f" provider for field .{field_name}" if field_name else "")
                    + f": {loc_match[:360]}"
                ),
                "locator": h.locator,
                "owner_struct": struct_name,
                "field": field_name,
            }
        )
    return out


def _search_pathspecs_for_kind(
    kind: str,
    module_path: str | None = None,
    *,
    exclude_driver_paths: bool = False,
) -> list[str]:
    module_family = _module_family_pathspec(module_path)
    if kind in {"enum", "macro", "field", "struct", "global", "unknown"}:
        out = ["include/*", "arch/*/include/*"]
        if module_family and not exclude_driver_paths:
            out.append(module_family)
        return out
    if kind == "function":
        out = ["kernel/*", "arch/*", "fs/*", "mm/*", "net/*", "lib/*", "security/*"]
        if module_family and not exclude_driver_paths:
            out.append(module_family)
        return out
    return []


def _is_driver_tree_path(path: str) -> bool:
    return path == "drivers" or path.startswith("drivers/")


def _filter_function_like_hits(accessor: RepositoryAccessor, side: str, hits, symbol_name: str):
    out = []
    for h in hits:
        if _looks_like_decl_or_def(h.text, symbol_name):
            out.append(h)
            continue
        if _looks_like_multiline_decl_or_def(accessor, side, h.path, h.line, h.text, symbol_name):
            out.append(h)
    return out


def _dedupe_hits(hits):
    out = []
    seen = set()
    for h in hits:
        key = (h.path, h.line, h.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _filter_constant_like_hits(hits, symbol_name: str):
    out = []
    for h in hits:
        text = h.text.strip()
        if re.search(rf"^\s*#\s*define\s+{re.escape(symbol_name)}\b", text):
            out.append(h)
            continue
        if re.search(rf"^\s*{re.escape(symbol_name)}\s*(?:=|,)", text):
            out.append(h)
            continue
        if re.search(rf"[{{,]\s*{re.escape(symbol_name)}\s*(?:=|,)", text):
            out.append(h)
            continue
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
    before = text[idx - 1] if idx > 0 else ""
    after_idx = idx + len(symbol_name)
    after = text[after_idx] if after_idx < len(text) else ""
    if (before and (before.isalnum() or before == "_")) or (after and (after.isalnum() or after == "_")):
        return False
    prefix = text[:idx]

    # Reject common call-site shapes.
    if any(prefix.rstrip().endswith(k) for k in ("return", "if", "for", "while", "switch", "sizeof")):
        return False
    if re.search(r"\b(if|for|while|switch|return)\s*\([^;{}]*$", prefix):
        return False
    if "->" in prefix or "." in prefix or "=" in prefix:
        return False
    if text.startswith(symbol_name + "("):
        return False
    decl_prefix = _strip_kernel_decl_attributes(prefix)
    if "," in decl_prefix and "(" in decl_prefix:
        return False

    # Heuristic: declaration/definition lines typically carry type qualifiers.
    tokens = set(decl_prefix.replace("*", " ").split())
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

    return False


def _strip_kernel_decl_attributes(prefix: str) -> str:
    """Remove leading/inline kernel annotation macros from a declaration prefix.

    Kernel prototypes commonly use attributes before the return type, e.g.
    ``__printf(2, 3) int dev_set_name(...)``.  The comma in the attribute
    otherwise looks like a function-call argument list and causes the locator
    to reject a real declaration as a callsite.
    """

    out = prefix
    attr_pat = re.compile(r"\b__[A-Za-z_][A-Za-z0-9_]*\s*(?:\([^)]*\))?")
    # Apply repeatedly because declarations often stack multiple annotations.
    prev = None
    while prev != out:
        prev = out
        out = attr_pat.sub(" ", out)
    return out


def _looks_like_multiline_decl_or_def(
    accessor: RepositoryAccessor,
    side: str,
    path: str,
    line_no: int,
    line: str,
    symbol_name: str,
) -> bool:
    """Accept split declarations such as:

    static inline void *
    pci_alloc_consistent(...)

    A plain line starting with the symbol is normally rejected as a call site.
    For kernel headers many inline wrappers put the return type on the
    previous line, so inspect a small backward window in the same file.
    """

    text = (line or "").strip()
    if not re.match(rf"^{re.escape(symbol_name)}\s*\(", text):
        return False
    if not path.startswith(("include/", "arch/")):
        return False
    try:
        src = accessor.read_file(side, path)
    except Exception:
        return False
    lines = src.splitlines()
    idx = max(0, int(line_no or 1) - 1)
    prefix_lines: list[str] = []
    for j in range(idx - 1, max(-1, idx - 5), -1):
        prev = lines[j].strip()
        if not prev:
            continue
        if prev.startswith(("//", "/*", "*")):
            continue
        if any(tok in prev for tok in (";", "{", "}", "=")):
            break
        prefix_lines.insert(0, prev)
    if not prefix_lines:
        return False
    prefix = " ".join(prefix_lines)
    tokens = set(prefix.replace("*", " ").split())
    typeish = {
        "static",
        "inline",
        "__always_inline",
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
        "dma_addr_t",
    }
    return bool(tokens & typeish)


def _module_family_pathspec(module_path: str | None) -> str | None:
    if not module_path:
        return None
    parts = [p for p in module_path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "drivers":
        return f"drivers/{parts[1]}/*"
    if len(parts) >= 1 and parts[0] in {"fs", "net", "mm", "kernel", "arch", "lib"}:
        return f"{parts[0]}/*"
    return None
