from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .module_source_loader import ModuleSourceResult
from .utils import dedupe_keep_order, ensure_dir, read_text, write_json


C_LIKE_SUFFIXES = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".S", ".s"}
RUST_SUFFIXES = {".rs"}

CONTROL_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "typeof",
    "alignof",
    "case",
    "do",
    "else",
    "goto",
}

DRIVER_ROOT_STRUCT_KIND = {
    "platform_driver": "platform_driver",
    "pci_driver": "pci_driver",
    "usb_driver": "usb_driver",
    "i2c_driver": "i2c_driver",
    "spi_driver": "spi_driver",
    "file_operations": "file_ops",
    "net_device_ops": "netdev_ops",
    "dev_pm_ops": "pm",
}

FILE_SCOPE_MACRO_HINTS = {
    "module_param_named",
    "module_init",
    "module_exit",
    "device_initcall",
    "subsys_initcall",
    "arch_initcall",
    "late_initcall",
    "fs_initcall",
    "EXPORT_SYMBOL",
    "EXPORT_SYMBOL_GPL",
    "IS_ENABLED",
    "IS_MODULE",
}

GLOBAL_SYMBOL_HINTS = {
    "system_wq",
    "system_percpu_wq",
    "system_unbound_wq",
    "system_freezable_wq",
    "system_highpri_wq",
}

@dataclass
class FunctionDef:
    name: str
    file: str
    start_line: int
    end_line: int
    signature: str
    body: str
    is_static: bool



def _line_number_from_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _strip_comments(code: str) -> str:
    # Fast and approximate; enough for regex scans.
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"//.*", "", code)
    return code


def _strip_comments_and_strings(code: str) -> str:
    code = _strip_comments(code)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code, flags=re.DOTALL)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code, flags=re.DOTALL)
    return code


def _find_matching_brace(text: str, open_pos: int) -> int:
    depth = 0
    i = open_pos
    in_str = False
    in_chr = False
    in_line_comment = False
    in_block_comment = False
    escape = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if in_chr:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_chr = False
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "'":
            in_chr = True
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return len(text) - 1


def _extract_c_like_functions(text: str, rel_file: str) -> list[FunctionDef]:
    pattern = re.compile(
        r"(?P<sig>(?:^|\n)\s*(?:[A-Za-z_][\w\s\*\n\t]*?)\b(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*)\{",
        re.MULTILINE,
    )
    out: list[FunctionDef] = []
    for m in pattern.finditer(text):
        name = m.group("name")
        if name in CONTROL_KEYWORDS:
            continue
        sig = m.group("sig")
        sig_line = sig.strip()
        # Skip declaration-like macros.
        if sig_line.startswith("#"):
            continue
        if re.search(r"\btypedef\b", sig_line):
            continue

        body_start = m.end() - 1
        body_end = _find_matching_brace(text, body_start)
        block = text[m.start() : body_end + 1]

        start_line = _line_number_from_offset(text, m.start())
        end_line = _line_number_from_offset(text, body_end)

        is_static = bool(re.search(r"\bstatic\b", sig_line))
        out.append(
            FunctionDef(
                name=name,
                file=rel_file,
                start_line=start_line,
                end_line=end_line,
                signature=sig_line,
                body=block,
                is_static=is_static,
            )
        )

    dedup = {}
    for f in out:
        key = (f.file, f.name, f.start_line)
        dedup[key] = f
    return list(dedup.values())


def _extract_rust_functions(text: str, rel_file: str) -> list[FunctionDef]:
    pattern = re.compile(r"(?m)^\s*(?:pub\s+)?(?:unsafe\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*\([^\)]*\)\s*(?:->[^\{]+)?\{")
    out: list[FunctionDef] = []
    for m in pattern.finditer(text):
        name = m.group("name")
        body_start = m.end() - 1
        body_end = _find_matching_brace(text, body_start)
        start_line = _line_number_from_offset(text, m.start())
        end_line = _line_number_from_offset(text, body_end)
        block = text[m.start() : body_end + 1]
        signature = m.group(0).strip()
        out.append(
            FunctionDef(
                name=name,
                file=rel_file,
                start_line=start_line,
                end_line=end_line,
                signature=signature,
                body=block,
                is_static=False,
            )
        )
    return out


def _extract_includes(text: str, rel_file: str) -> list[dict]:
    out = []
    for idx, line in enumerate(text.splitlines(), start=1):
        m = re.match(r"\s*#\s*include\s*([<\"].*[>\"])", line)
        if not m:
            continue
        token = m.group(1)
        out.append(
            {
                "file": rel_file,
                "line": idx,
                "include": token,
                "is_system": token.startswith("<"),
                "code": line.strip(),
            }
        )
    return out


def _extract_local_macros(text: str) -> set[str]:
    names = set()
    for m in re.finditer(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", text):
        names.add(m.group(1))
    return names


def _extract_local_struct_defs(text: str) -> set[str]:
    names = set()
    for m in re.finditer(r"(?m)^\s*struct\s+([A-Za-z_]\w*)\s*\{", text):
        names.add(m.group(1))
    return names


def _extract_calls_from_body(body: str) -> list[str]:
    clean = _strip_comments(body)
    names = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", clean):
        name = m.group(1)
        if name in CONTROL_KEYWORDS:
            continue
        names.append(name)
    return names


def _extract_fields_from_body(body: str) -> list[str]:
    clean = _strip_comments(body)
    out = []
    for m in re.finditer(r"(?:->|\.)\s*([A-Za-z_]\w*)", clean):
        out.append(m.group(1))
    return out


def _extract_struct_types_from_body(body: str) -> list[str]:
    clean = _strip_comments(body)
    return [m.group(1) for m in re.finditer(r"\bstruct\s+([A-Za-z_]\w*)\b", clean)]


def _extract_enum_constants_from_body(body: str) -> list[str]:
    clean = _strip_comments_and_strings(body)
    out = []
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{2,})\b", clean):
        token = m.group(1)
        if token in {"NULL", "TRUE", "FALSE"}:
            continue
        out.append(token)
    return out


def _extract_function_body_only(full_block: str) -> str:
    start = full_block.find("{")
    end = full_block.rfind("}")
    if start < 0 or end <= start:
        return full_block
    return full_block[start + 1 : end]


def _extract_parameter_identifiers(signature: str) -> set[str]:
    # Best-effort parameter name extraction for C-like signatures.
    out = set()
    l = signature.find("(")
    r = signature.rfind(")")
    if l < 0 or r <= l:
        return out
    params = signature[l + 1 : r]
    if "void" == params.strip():
        return out
    for raw in params.split(","):
        part = raw.strip()
        if not part:
            continue
        # Function pointer parameter: int (*fn)(...)
        m_fp = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", part)
        if m_fp:
            out.add(m_fp.group(1))
            continue
        # Normal parameter: const struct foo *bar
        m = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", part)
        if m:
            out.add(m.group(1))
    return out


def _extract_fnptr_arg_refs(body: str, local_function_names: set[str]) -> list[str]:
    """
    Find local function names passed as callback/function-pointer arguments.
    Example: walk_hmem_resources(..., hmem_register_device)
    """
    clean = _strip_comments(body)
    refs = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^;]*?)\)", clean, flags=re.DOTALL):
        args = m.group(2)
        for fn in local_function_names:
            if re.search(rf"\b{re.escape(fn)}\b", args):
                refs.append(fn)
    return refs


def _extract_high_value_global_refs(
    body: str,
    *,
    local_function_names: set[str],
    param_names: set[str],
) -> list[str]:
    """
    Extract a conservative set of global-like identifiers used in function bodies.
    We intentionally focus on high-value migration families (e.g., *_wq) to
    avoid polluting symbol sets with arbitrary local variables.
    """
    clean = _strip_comments_and_strings(body)
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\b", clean):
        tok = m.group(1)
        if tok in CONTROL_KEYWORDS:
            continue
        if tok in local_function_names or tok in param_names:
            continue
        if tok in GLOBAL_SYMBOL_HINTS or tok.endswith("_wq"):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _extract_ops_table_callbacks(text: str, rel_file: str) -> tuple[list[dict], list[dict]]:
    callbacks = []
    symbols = []

    init_pat = re.compile(r"\bstruct\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*=\s*\{")
    for m in init_pat.finditer(text):
        struct_type = m.group(1)
        var_name = m.group(2)

        open_pos = m.end() - 1
        close_pos = _find_matching_brace(text, open_pos)
        block = text[open_pos : close_pos + 1]

        root_kind = DRIVER_ROOT_STRUCT_KIND.get(struct_type)

        for assign in re.finditer(r"\.\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)", block):
            field = assign.group(1)
            func = assign.group(2)
            line = _line_number_from_offset(text, m.start() + assign.start())

            symbols.append(
                {
                    "name": field,
                    "kind": "field",
                    "file": rel_file,
                    "line": line,
                    "code": assign.group(0),
                    "enclosing_function": None,
                    "is_driver_local": False,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

            if root_kind:
                callbacks.append(
                    {
                        "root_kind": root_kind,
                        "root_struct_type": struct_type,
                        "root_variable": var_name,
                        "root_function": func,
                        "callback_field": field,
                        "file": rel_file,
                        "line": line,
                        "registration_site": f"{rel_file}:{line}",
                        "evidence": assign.group(0),
                        "locator": "regex",
                    }
                )

    return callbacks, symbols


def _extract_irq_callbacks(functions: list[FunctionDef]) -> list[dict]:
    out = []
    irq_api = {"request_irq", "request_threaded_irq", "devm_request_irq", "devm_request_threaded_irq"}
    for fn in functions:
        for api in irq_api:
            for m in re.finditer(rf"\b{api}\s*\(([^;]+)\)", fn.body, re.DOTALL):
                call_txt = m.group(0)
                # Best-effort: detect handler argument tokens.
                cands = re.findall(r"\b([A-Za-z_]\w*)\b", m.group(1))
                handler = None
                for token in cands:
                    if token in {api, "irq", "flags", "dev", "name", "NULL", "0"}:
                        continue
                    handler = token
                    break
                if not handler:
                    continue
                line = fn.start_line + fn.body[: m.start()].count("\n")
                out.append(
                    {
                        "root_kind": "irq",
                        "root_struct_type": None,
                        "root_variable": None,
                        "root_function": handler,
                        "callback_field": api,
                        "file": fn.file,
                        "line": line,
                        "registration_site": f"{fn.file}:{line}",
                        "evidence": call_txt.strip()[:300],
                        "locator": "regex",
                    }
                )
    return out


def _extract_init_exit_callbacks(text: str, rel_file: str) -> list[dict]:
    out = []
    macro_to_kind = {
        "module_init": "init",
        "device_initcall": "init",
        "subsys_initcall": "init",
        "arch_initcall": "init",
        "late_initcall": "init",
        "fs_initcall": "init",
        "module_exit": "exit",
    }
    pat = re.compile(r"\b(" + "|".join(re.escape(k) for k in macro_to_kind.keys()) + r")\s*\(\s*([A-Za-z_]\w*)\s*\)")
    for m in pat.finditer(_strip_comments(text)):
        macro = m.group(1)
        fn = m.group(2)
        line = _line_number_from_offset(text, m.start())
        out.append(
            {
                "root_kind": macro_to_kind.get(macro, "unknown"),
                "root_struct_type": None,
                "root_variable": macro,
                "root_function": fn,
                "callback_field": macro,
                "file": rel_file,
                "line": line,
                "registration_site": f"{rel_file}:{line}",
                "evidence": m.group(0),
                "locator": "regex",
            }
        )
    return out


def _extract_file_scope_macro_invocations(
    text: str,
    rel_file: str,
    functions: list[FunctionDef],
    local_function_names: set[str],
    local_macros: set[str],
) -> list[dict]:
    """
    Capture macro-like invocations outside function bodies.
    This covers module/file-scope forms like:
      - module_param_named(...)
      - DEFINE_MUTEX(...)
      - EXPORT_SYMBOL_GPL(...)
      - MODULE_*(...)
      - IS_MODULE(...), IS_ENABLED(...) in #if blocks
    """
    lines = text.splitlines()
    fn_ranges = [(f.start_line, f.end_line) for f in functions if f.file == rel_file]

    def _line_in_fn(ln: int) -> bool:
        for s, e in fn_ranges:
            if s <= ln <= e:
                return True
        return False

    out = []
    in_block_comment = False
    call_re = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

    force_file_macro_re = re.compile(
        r"\b("
        r"MODULE_[A-Za-z0-9_]+|"
        r"EXPORT_SYMBOL(?:_GPL)?|"
        r"module_(?:init|exit|param_named|param)|"
        r"DEFINE_[A-Za-z0-9_]+|"
        r"IS_(?:ENABLED|MODULE)"
        r")\s*\("
    )

    for line_no, raw in enumerate(lines, start=1):
        if _line_in_fn(line_no):
            # Function-range detection is approximate; allow module/file-scope macro
            # patterns to pass even when boundary parsing is slightly off.
            if not force_file_macro_re.search(raw):
                continue

        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2 :]
            in_block_comment = False

        # Remove block comments inline, preserving non-comment code on the line.
        while True:
            start = line.find("/*")
            if start < 0:
                break
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block_comment = True
                break
            line = line[:start] + line[end + 2 :]

        # Remove line comments.
        line = re.sub(r"//.*", "", line)
        if not line.strip():
            continue

        for m in call_re.finditer(line):
            name = m.group(1)
            if name in CONTROL_KEYWORDS:
                continue

            is_macro_like = (
                (name.upper() == name and len(name) > 2)
                or name in FILE_SCOPE_MACRO_HINTS
                or name.startswith("MODULE_")
                or name.startswith("EXPORT_SYMBOL")
                or name.startswith("DEFINE_")
                or name.startswith("module_")
                or name.startswith("IS_")
            )
            if not is_macro_like:
                continue

            out.append(
                {
                    "name": name,
                    "kind": "macro",
                    "file": rel_file,
                    "line": line_no,
                    "code": raw.strip(),
                    "enclosing_function": None,
                    "is_driver_local": name in local_macros or name in local_function_names,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

    return out


def run_symbol_scanner(module_result: ModuleSourceResult, extracted_dir: Path) -> dict:
    ensure_dir(extracted_dir)

    files = module_result.source_files_old
    all_functions: list[FunctionDef] = []
    includes_payload = []
    all_local_macros: set[str] = set()
    all_local_structs: set[str] = set()

    file_texts: dict[str, str] = {}
    for rel in files:
        src_path = module_result.copied_root / rel
        if not src_path.exists():
            continue
        text = read_text(src_path)
        file_texts[rel] = text

        includes_payload.extend(_extract_includes(text, rel))
        all_local_macros.update(_extract_local_macros(text))
        all_local_structs.update(_extract_local_struct_defs(text))

        suffix = Path(rel).suffix.lower()
        if suffix in C_LIKE_SUFFIXES:
            all_functions.extend(_extract_c_like_functions(text, rel))
        elif suffix in RUST_SUFFIXES:
            all_functions.extend(_extract_rust_functions(text, rel))

    local_function_names = {f.name for f in all_functions}

    used_symbols = []
    call_edges = []
    callbacks = []

    # include symbols
    for inc in includes_payload:
        token = inc["include"].strip('<>"')
        used_symbols.append(
            {
                "name": token,
                "kind": "include",
                "file": inc["file"],
                "line": inc["line"],
                "code": inc["code"],
                "enclosing_function": None,
                "is_driver_local": not token.startswith(("linux/", "asm/", "uapi/", "drm/", "net/")),
                "definition_location_old": None,
                "definition_location_new": None,
                "locator": "regex",
            }
        )

    for fn in all_functions:
        body_only = _extract_function_body_only(fn.body)
        calls = _extract_calls_from_body(body_only)
        fields = _extract_fields_from_body(body_only)
        struct_types = _extract_struct_types_from_body(body_only)
        enums = _extract_enum_constants_from_body(body_only)
        param_names = _extract_parameter_identifiers(fn.signature)
        fnptr_arg_local_refs = _extract_fnptr_arg_refs(body_only, local_function_names)
        global_refs = _extract_high_value_global_refs(
            body_only,
            local_function_names=local_function_names,
            param_names=param_names,
        )

        start_line = fn.start_line

        for call_name in calls:
            if call_name == fn.name:
                # Skip function name from declaration/signature leftovers.
                continue
            if call_name in param_names and call_name not in local_function_names:
                # Local callback parameter variable (e.g. fn(host, ...)); not kernel-side symbol.
                continue
            is_upper = call_name.upper() == call_name and len(call_name) > 2
            kind = "macro" if is_upper else "function"
            is_local = call_name in local_function_names or call_name in all_local_macros
            used_symbols.append(
                {
                    "name": call_name,
                    "kind": kind,
                    "file": fn.file,
                    "line": start_line,
                    "code": fn.signature,
                    "enclosing_function": fn.name,
                    "is_driver_local": is_local,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )
            if call_name in local_function_names:
                call_edges.append({"caller": fn.name, "callee": call_name, "file": fn.file, "line": start_line, "locator": "regex"})

        for ref_name in fnptr_arg_local_refs:
            if ref_name == fn.name:
                continue
            call_edges.append(
                {
                    "caller": fn.name,
                    "callee": ref_name,
                    "file": fn.file,
                    "line": start_line,
                    "locator": "regex",
                    "edge_kind": "fnptr_arg",
                }
            )

        for fld in fields:
            used_symbols.append(
                {
                    "name": fld,
                    "kind": "field",
                    "file": fn.file,
                    "line": start_line,
                    "code": fn.signature,
                    "enclosing_function": fn.name,
                    "is_driver_local": False,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

        for st in struct_types:
            used_symbols.append(
                {
                    "name": st,
                    "kind": "struct",
                    "file": fn.file,
                    "line": start_line,
                    "code": fn.signature,
                    "enclosing_function": fn.name,
                    "is_driver_local": st in all_local_structs,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

        for en in enums:
            used_symbols.append(
                {
                    "name": en,
                    "kind": "enum",
                    "file": fn.file,
                    "line": start_line,
                    "code": fn.signature,
                    "enclosing_function": fn.name,
                    "is_driver_local": False,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

        for g in global_refs:
            used_symbols.append(
                {
                    "name": g,
                    "kind": "global",
                    "file": fn.file,
                    "line": start_line,
                    "code": fn.signature,
                    "enclosing_function": fn.name,
                    "is_driver_local": False,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

    # ops table callbacks.
    for rel, text in file_texts.items():
        cb_items, cb_symbols = _extract_ops_table_callbacks(text, rel)
        callbacks.extend(cb_items)
        used_symbols.extend(cb_symbols)

    callbacks.extend(_extract_irq_callbacks(all_functions))
    for rel, text in file_texts.items():
        callbacks.extend(_extract_init_exit_callbacks(text, rel))
        used_symbols.extend(
            _extract_file_scope_macro_invocations(
                text,
                rel,
                all_functions,
                local_function_names,
                all_local_macros,
            )
        )

    # Unknown tokens we couldn't classify but may be relevant.
    # Extract a small conservative set of identifiers from callback registrations.
    for cb in callbacks:
        token = cb.get("root_function")
        if token and token not in local_function_names:
            used_symbols.append(
                {
                    "name": token,
                    "kind": "unknown",
                    "file": cb["file"],
                    "line": cb["line"],
                    "code": cb.get("evidence", ""),
                    "enclosing_function": None,
                    "is_driver_local": False,
                    "definition_location_old": None,
                    "definition_location_new": None,
                    "locator": "regex",
                }
            )

    # Dedup symbols by name/kind/use site.
    seen = set()
    dedup_symbols = []
    for sym in used_symbols:
        key = (sym["name"], sym["kind"], sym["file"], sym["line"], sym.get("enclosing_function"))
        if key in seen:
            continue
        seen.add(key)
        dedup_symbols.append(sym)

    # Deduplicate call edges and drop self-loops to avoid declaration artifacts.
    dedup_call_edges = []
    seen_edges = set()
    for e in call_edges:
        caller = e.get("caller")
        callee = e.get("callee")
        if not caller or not callee:
            continue
        if caller == callee:
            continue
        key = (caller, callee, e.get("file"), e.get("line"), e.get("edge_kind", "calls"))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        dedup_call_edges.append(e)

    call_graph_payload = {
        "nodes": sorted(dedupe_keep_order([f.name for f in all_functions])),
        "edges": dedup_call_edges,
    }

    functions_payload = {
        "functions": [
            {
                "name": f.name,
                "file": f.file,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "signature": f.signature,
                "is_static": f.is_static,
            }
            for f in all_functions
        ]
    }

    includes_out = {"includes": includes_payload}
    callbacks_out = {"callbacks": callbacks}
    used_symbols_out = {
        "symbols": dedup_symbols,
        "scan_notes": {
            "primary_locator": "regex",
            "driver_local_function_count": len(local_function_names),
            "local_macro_count": len(all_local_macros),
            "local_struct_count": len(all_local_structs),
            "tools": {
                "clangd": False,
                "ctags": False,
                "tree_sitter": False,
                "ripgrep_regex_fallback": True,
            },
        },
    }

    write_json(extracted_dir / "driver_used_symbols.json", used_symbols_out)
    write_json(extracted_dir / "driver_includes.json", includes_out)
    write_json(extracted_dir / "driver_functions.json", functions_payload)
    write_json(extracted_dir / "driver_callbacks.json", callbacks_out)
    write_json(extracted_dir / "call_graph.json", call_graph_payload)

    return {
        "driver_used_symbols": used_symbols_out,
        "driver_includes": includes_out,
        "driver_functions": functions_payload,
        "driver_callbacks": callbacks_out,
        "call_graph": call_graph_payload,
    }
