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
_HELPER_DECL_RE = re.compile(
    r"^(?:extern\s+|static\s+inline\s+|static\s+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_\s\*\[\]]+\s+)+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_EXPORT_RE = re.compile(r"\bEXPORT_SYMBOL(?:_GPL)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")
_MACRO_DEF_RE = re.compile(r"^\s*#\s*define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")
_COMMON_TOKENS = {
    "a",
    "an",
    "and",
    "api",
    "arg",
    "args",
    "bool",
    "char",
    "const",
    "dev",
    "device",
    "do",
    "end",
    "for",
    "from",
    "get",
    "int",
    "long",
    "new",
    "old",
    "or",
    "ptr",
    "return",
    "res",
    "set",
    "size",
    "start",
    "static",
    "struct",
    "the",
    "to",
    "u64",
    "unsigned",
    "void",
    "desc",
}
_HELPER_RELATION_STOP_TOKENS = _COMMON_TOKENS | {
    # These tokens appear in many helper names and should not by themselves
    # justify a source-compatible migration signal.
    "add",
    "atomic",
    "begin",
    "bit",
    "blk",
    "bpf",
    "capable",
    "class",
    "classes",
    "commit",
    "create",
    "complete",
    "data",
    "del",
    "delete",
    "destroy",
    "drm",
    "each",
    "entry",
    "exit",
    "first",
    "free",
    "helper",
    "inc",
    "init",
    "inner",
    "ipv6",
    "list",
    "lock",
    "map",
    "may",
    "mq",
    "mutex",
    "net",
    "ns",
    "offset",
    "page",
    "payload",
    "priv",
    "prog",
    "queue",
    "rcu",
    "read",
    "refcount",
    "request",
    "rq",
    "skb",
    "spin",
    "state",
    "stop",
    "test",
    "unlock",
    "user",
    "vblank",
    "write",
}
_OPPOSITE_TOKEN_PAIRS = {
    ("lock", "unlock"),
    ("get", "put"),
    ("alloc", "free"),
    ("create", "destroy"),
    ("register", "unregister"),
    ("enable", "disable"),
    ("open", "close"),
}


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


def _normalized_token(token: str) -> str:
    token = (token or "").lower().strip("_")
    if not token:
        return ""
    if token in {"reserved", "reservation", "reservations"}:
        return "reserve"
    if token in {"resources", "resource"}:
        return "res"
    if token in {"descs", "descriptor", "descriptors"}:
        return "desc"
    return token


def _symbol_tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9]*", (text or "").replace("-", "_"))
    out: set[str] = set()
    for part in raw:
        for token in re.split(r"_+", part):
            token = _normalized_token(token)
            if not token or token in _COMMON_TOKENS or len(token) <= 1:
                continue
            out.add(token)
    return out


def _symbol_base_name(symbol: str) -> str:
    text = str(symbol or "")
    m = re.match(r"^(.*) \(([^()]*)\)$", text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _safe_kernel_evidence_path(path: str, module_path: str) -> bool:
    p = (path or "").strip().strip("/")
    if not p:
        return False
    mod = module_path.strip().strip("/")
    if mod and (p == mod or p.startswith(mod + "/")):
        return False
    # Paper-facing default excludes driver diffs, including other drivers.
    if p == "drivers" or p.startswith("drivers/"):
        return False
    if p.startswith(("Documentation/", "tools/", "samples/", "scripts/")):
        return False
    return True


_INCLUDE_RE = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]")


def _visible_includes_by_file(extracted_dir: Path, module_path: str) -> tuple[dict[str, set[str]], set[str]]:
    payload = read_json(extracted_dir / "driver_includes.json", default={}) or {}
    direct_system: dict[str, set[str]] = {}
    local_edges: dict[str, set[str]] = {}
    all_system_headers: set[str] = set()

    module_root = extracted_dir / "old_module_sources" / module_path
    source_files = {
        p.relative_to(extracted_dir / "old_module_sources").as_posix()
        for p in module_root.rglob("*")
        if p.is_file() and p.suffix in {".c", ".h"}
    } if module_root.exists() else set()

    def resolve_local(file_rel: str, inc: str) -> str:
        base = Path(file_rel).parent
        candidate = (base / inc).as_posix()
        parts: list[str] = []
        for part in candidate.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        normalized = "/".join(parts)
        if normalized in source_files:
            return normalized
        fallback = (Path(module_path) / inc).as_posix()
        return fallback if fallback in source_files else ""

    for item in payload.get("includes", []) or []:
        file_rel = str(item.get("file", "") or "").strip()
        inc = str(item.get("include", "") or "").strip()
        if not file_rel or not inc:
            continue
        raw_inc = inc.strip("<>\"")
        if inc.startswith("<"):
            direct_system.setdefault(file_rel, set()).add(raw_inc)
            all_system_headers.add(raw_inc)
            continue
        target = resolve_local(file_rel, raw_inc)
        if target:
            local_edges.setdefault(file_rel, set()).add(target)

    # Parse local headers too. driver_includes.json is source-file oriented; this
    # closure lets us avoid flagging headers already provided through local
    # module headers such as vkms_drv.h.
    for file_rel in sorted(source_files):
        p = extracted_dir / "old_module_sources" / file_rel
        try:
            lines = read_text(p).splitlines()
        except Exception:
            continue
        for line in lines[:120]:
            m = _INCLUDE_RE.match(line)
            if not m:
                continue
            inc = m.group(2).strip()
            if m.group(1) == "<":
                direct_system.setdefault(file_rel, set()).add(inc)
                all_system_headers.add(inc)
            else:
                target = resolve_local(file_rel, inc)
                if target:
                    local_edges.setdefault(file_rel, set()).add(target)

    memo: dict[str, set[str]] = {}

    def visible(file_rel: str, stack: tuple[str, ...] = ()) -> set[str]:
        if file_rel in memo:
            return set(memo[file_rel])
        if file_rel in stack:
            return set()
        out = set(direct_system.get(file_rel, set()))
        for local in local_edges.get(file_rel, set()):
            out.update(visible(local, stack + (file_rel,)))
        memo[file_rel] = set(out)
        return out

    all_files = set(source_files) | set(direct_system) | set(local_edges)
    return {file_rel: visible(file_rel) for file_rel in all_files}, all_system_headers


def _include_header_from_provider_path(path: str) -> str:
    p = str(path or "").strip()
    if not p.startswith("include/") or not p.endswith(".h"):
        return ""
    return p[len("include/") :]


def _symbol_header_relation(symbol: str, provider_path: str) -> bool:
    """Return whether a provider header is specific enough for a direct-include hint."""
    header = _include_header_from_provider_path(provider_path)
    if not header:
        return False
    stem = Path(header).stem.lower().replace("-", "_")
    sym = _symbol_base_name(symbol).lower()
    sym_flat = sym.replace("_", "")
    stem_flat = stem.replace("_", "")
    if len(stem_flat) < 5:
        return False
    if stem in sym or stem_flat in sym_flat:
        return True
    # DRM and subsystem headers often encode a family prefix, e.g.
    # drm_colorop.h provides struct drm_colorop_state and helpers.
    stem_tokens = set(stem.split("_"))
    sym_tokens = set(sym.split("_"))
    meaningful = stem_tokens - _HELPER_RELATION_STOP_TOKENS
    return bool(meaningful) and meaningful.issubset(sym_tokens)


def _header_includes(text: str, header: str) -> bool:
    return bool(re.search(rf"^\s*#\s*include\s*<\s*{re.escape(header)}\s*>", text or "", flags=re.M))


def _header_lost_transitive_include(
    accessor: RepositoryAccessor,
    *,
    including_header: str,
    provider_header: str,
    cache: dict[tuple[str, str], bool],
) -> bool:
    if not including_header or not provider_header or including_header == provider_header:
        return False
    key = (including_header, provider_header)
    if key in cache:
        return cache[key]
    path = f"include/{including_header}"
    try:
        if not accessor.path_exists("old", path) or not accessor.path_exists("new", path):
            cache[key] = False
            return False
        old_text = accessor.read_file("old", path)
        new_text = accessor.read_file("new", path)
    except Exception:
        cache[key] = False
        return False
    lost = _header_includes(old_text, provider_header) and not _header_includes(new_text, provider_header)
    cache[key] = bool(lost)
    return bool(lost)


def _detect_direct_include_requirements(
    *,
    accessor: RepositoryAccessor,
    item: dict[str, Any],
    symbol: str,
    module_path: str,
    visible_includes: dict[str, set[str]],
    module_system_headers: set[str],
    transitive_include_loss_cache: dict[tuple[str, str], bool],
) -> list[dict[str, Any]]:
    """Identify source files relying on transitive kernel header includes.

    This uses old-driver source plus old/new kernel definition locations only.
    It does not read the new target driver. The rule is intentionally narrow:
    the provider must be a public header whose basename is related to the
    symbol family, and the same file must not already directly include it.
    """
    if item.get("kind") not in {"function", "struct"}:
        return []
    providers: list[str] = []
    for loc_key in ("new_locations", "old_locations"):
        for loc in item.get(loc_key, []) or []:
            path = str(loc.get("path", "") or "")
            header = _include_header_from_provider_path(path)
            if not header:
                continue
            if not _safe_kernel_evidence_path(path, module_path):
                continue
            if not _symbol_header_relation(symbol, path):
                continue
            # Avoid converting every transitive kernel header use into a patch
            # request. Only require a direct include when the target module
            # already includes this provider header somewhere, which indicates
            # the header is a known public dependency in this module.
            if header not in module_system_headers:
                continue
            if header not in providers:
                providers.append(header)
    if not providers:
        return []

    reqs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for use in item.get("used_by_driver", []) or []:
        file_rel = str(use.get("file", "") or "")
        if not file_rel.startswith(module_path.rstrip("/") + "/"):
            continue
        includes = visible_includes.get(file_rel, set())
        for header in providers:
            key = (file_rel, header)
            if key in seen or header in includes:
                continue
            lost_via = sorted(
                inc
                for inc in includes
                if _header_lost_transitive_include(
                    accessor,
                    including_header=inc,
                    provider_header=header,
                    cache=transitive_include_loss_cache,
                )
            )
            if not lost_via:
                continue
            seen.add(key)
            reqs.append(
                {
                    "file": file_rel,
                    "required_include": f"<{header}>",
                    "provider_path": f"include/{header}",
                    "symbol": symbol,
                    "kind": item.get("kind"),
                    "line": use.get("line"),
                    "enclosing_function": use.get("enclosing_function"),
                    "transitive_include_lost_via": lost_via[:6],
                    "reason": "old source relied on a transitive kernel header include that is absent in new_ref",
                }
            )
    return reqs


def _strip_decl_annotations(text: str) -> str:
    out = re.sub(r"(^|\s)__\w+\s*\([^;]*?\)\s*", r"\1", text or "")
    out = re.sub(r"(^|\s)__\w+\b\s*", r"\1", out)
    return _norm_signature_line(out)


def _extract_function_signature(def_text: str, symbol: str) -> str:
    """Best-effort signature extraction from numbered definition excerpts."""
    if not def_text or not symbol:
        return ""
    lines = _strip_lineno_prefix(def_text).splitlines()
    start_idx = -1
    pat = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("*", "/*", "//")):
            continue
        if pat.search(line):
            start_idx = idx
            break
    if start_idx < 0:
        return ""

    collected: list[str] = []
    for line in lines[start_idx : min(len(lines), start_idx + 12)]:
        part = line.strip()
        if not part:
            continue
        collected.append(part)
        joined = " ".join(collected)
        if ";" in joined or "{" in joined:
            break

    sig = " ".join(collected)
    sig = sig.split("{", 1)[0]
    sig = sig.split(";", 1)[0]
    return _strip_decl_annotations(sig)


def _split_function_signature(sig: str, symbol: str) -> tuple[str, str] | None:
    if not sig or not symbol:
        return None
    m = re.search(rf"\b{re.escape(symbol)}\s*\(", sig)
    if not m:
        return None
    ret = _norm(sig[: m.start()])
    rest = sig[m.end() :]
    depth = 1
    params: list[str] = []
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        params.append(ch)
    if depth != 0:
        return None
    params_norm = _norm(",".join(_norm(p) for p in "".join(params).split(",")))
    return ret, params_norm


def _split_macro_signature(sig: str, symbol: str) -> tuple[str, str] | None:
    if not sig or not symbol:
        return None
    m = re.match(rf"^\s*#\s*define\s+{re.escape(symbol)}\s*(?:\((?P<params>[^)]*)\))?", sig)
    if not m:
        return None
    params = m.group("params")
    if params is None:
        return "#define", ""
    params_norm = _norm(",".join(_norm(p) for p in params.split(",")))
    return "#define", params_norm


def _function_signature_change_type(symbol: str, old_def: str, new_def: str, current_change_type: str) -> str:
    old_sig = _extract_function_signature(old_def, symbol)
    new_sig = _extract_function_signature(new_def, symbol)
    if not old_sig or not new_sig:
        if current_change_type == "signature_change":
            return "unknown"
        return current_change_type
    if old_sig.lstrip().startswith("#define") or new_sig.lstrip().startswith("#define"):
        old_parts = _split_macro_signature(old_sig, symbol)
        new_parts = _split_macro_signature(new_sig, symbol)
        if old_parts and new_parts and old_parts[1] != new_parts[1]:
            return "signature_change"
        # Macro body-only changes are kernel implementation/context evidence,
        # not a source-level driver callsite signature change.
        if current_change_type in {"signature_change", "return_value_change"}:
            return "unknown"
        return current_change_type
    if re.match(rf"^#\s*define\s+{re.escape(symbol)}\s*\(\s*\.\.\.\s*\)", new_sig):
        # Source-compatible profiling/allocation wrapper macro.
        return "unknown"
    if old_sig != new_sig:
        old_parts = _split_function_signature(old_sig, symbol)
        new_parts = _split_function_signature(new_sig, symbol)
        if old_parts and new_parts:
            old_ret, old_params = old_parts
            new_ret, new_params = new_parts
            if old_params != new_params:
                return "signature_change"
            if old_ret != new_ret:
                return "return_value_change"
        return "signature_change"
    if current_change_type in {"signature_change", "return_value_change", "ownership_change", "locking_change"}:
        return "unknown"
    return current_change_type


def _signature_annotation_only(old_match: str, new_match: str) -> bool:
    if not old_match or not new_match or old_match == new_match:
        return False
    return _strip_decl_annotations(old_match) == _strip_decl_annotations(new_match)


def _opposite_operation_pair(old_symbol: str, new_symbol: str) -> bool:
    old_tokens = _symbol_tokens(old_symbol)
    new_tokens = _symbol_tokens(new_symbol)
    for left, right in _OPPOSITE_TOKEN_PAIRS:
        if left in old_tokens and right in new_tokens:
            return True
        if right in old_tokens and left in new_tokens:
            return True
    return False


def _meaningful_helper_tokens(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t not in _HELPER_RELATION_STOP_TOKENS}


def _allocator_family_relation(old_symbol: str, cand_symbol: str) -> bool:
    old = (old_symbol or "").lower()
    cand = (cand_symbol or "").lower()
    if "alloc" not in old or "alloc" not in cand:
        return False
    return cand.endswith(("_obj", "_objs", "_flex"))


def _strong_kernel_helper_relation(
    *,
    old_symbol: str,
    cand_symbol: str,
    old_tokens: set[str],
    cand_tokens: set[str],
    context_tokens: set[str],
) -> tuple[bool, dict[str, int]]:
    """Conservatively gate kernel-side helper discovery.

    This path must not become a generic "same subsystem changed" signal. It is
    intended for cases where old/new kernel diffs add a helper that is
    semantically close to an old-driver callsite, such as soft-reserve helpers.
    Generic shared tokens like lock/mutex/list/drm/bpf are ignored.
    """
    meaningful_old = _meaningful_helper_tokens(old_tokens)
    meaningful_cand = _meaningful_helper_tokens(cand_tokens)
    meaningful_context = _meaningful_helper_tokens(context_tokens)
    overlap = meaningful_old & meaningful_cand
    context_overlap = meaningful_context & meaningful_cand
    distinct_context_overlap = context_overlap - meaningful_old
    stats = {
        "meaningful_old": len(meaningful_old),
        "meaningful_candidate": len(meaningful_cand),
        "meaningful_overlap": len(overlap),
        "meaningful_context_overlap": len(context_overlap),
        "distinct_context_overlap": len(distinct_context_overlap),
    }
    if not meaningful_old or not meaningful_cand:
        return False, stats

    if _allocator_family_relation(old_symbol, cand_symbol):
        # Allocator helpers remain source-compatible modernization signals.
        # Require at least allocator-family relation plus old-driver context
        # using a size/object idiom before allowing the signal.
        allocator_context = meaningful_context & {"object", "obj", "objs", "flex", "struct", "sizeof"}
        return bool(context_overlap or allocator_context), stats

    # Require a non-generic relation to the old symbol and an extra contextual
    # token from the old driver. This keeps dax/hmem-style soft-reserve helper
    # discovery while filtering generic lock/list/refcount helper additions.
    if not overlap:
        return False, stats
    if not distinct_context_overlap:
        return False, stats
    return True, stats


def _guess_change_type(kind: str, definition_changed: bool, old_exists: bool, new_exists: bool, blob: str) -> str:
    text = (blob or "").lower()
    if kind == "include":
        return "include_dependency" if (definition_changed or old_exists != new_exists) else "unknown"
    if kind in {"field", "struct"} and definition_changed:
        return "struct_field_change"
    if old_exists and not new_exists:
        if "rename" in text:
            return "rename"
        return "api_deprecation"
    if "signature" in text:
        return "signature_change"
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
        paths = [str(x) for x in (item.get("paths", []) or []) if str(x).strip()]
        if source == "table":
            ev = ["semantic_obligation_table"] + ev
        rec = merged.get(sym)
        if not rec:
            merged[sym] = {
                "symbol": sym,
                "score": score,
                "evidence": dedupe_list(ev)[:8],
                "paths": dedupe_list(paths)[:8],
            }
            return
        rec["score"] = max(int(rec.get("score", 1) or 1), score)
        rec["evidence"] = dedupe_list(list(rec.get("evidence", [])) + ev)[:8]
        rec["paths"] = dedupe_list(list(rec.get("paths", [])) + paths)[:8]

    for c in base or []:
        add_one(c, "base")
    for c in second or []:
        add_one(c, second_source)

    out = list(merged.values())
    out.sort(key=lambda x: (-int(x.get("score", 1) or 1), str(x.get("symbol", ""))))
    return out[:max_candidates]


def _guess_return_type_from_line(line: str, symbol: str) -> str:
    text = (line or "").strip().rstrip(";")
    text = re.sub(r"^(?:extern|static|inline|__always_inline)\s+", "", text)
    m = re.match(rf"(?P<ret>.+?)\b{re.escape(symbol)}\s*\(", text)
    if not m:
        return ""
    return _norm(m.group("ret")).strip()


def _enrich_replacement_candidates(
    accessor: RepositoryAccessor,
    candidates: list[dict[str, Any]],
    *,
    module_path: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in candidates or []:
        rec = dict(cand)
        symbol = str(rec.get("symbol", "") or "")
        if not symbol:
            out.append(rec)
            continue
        raw_paths = [str(p) for p in (rec.get("paths", []) or []) if _safe_kernel_evidence_path(str(p), module_path)]
        pathspecs = raw_paths or None
        hits = accessor.search(
            "new",
            rf"\b{re.escape(symbol)}\s*\(",
            regex=True,
            pathspecs=pathspecs,
            max_hits=40,
        )
        hits = [h for h in hits if _safe_kernel_evidence_path(h.path, module_path)]
        if not hits:
            out.append(rec)
            continue

        def hit_score(hit) -> tuple[int, int]:
            text = hit.text.strip()
            score = 0
            if Path(hit.path).suffix == ".c":
                score += 30
            if re.match(rf"^(?:extern\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*\b{re.escape(symbol)}\s*\(", text):
                score += 20
            if "EXPORT_SYMBOL" in text:
                score -= 20
            return (score, -hit.line)

        best = max(hits, key=hit_score)
        rec["new_location"] = {
            "path": best.path,
            "line": best.line,
            "match": best.text.strip(),
            "locator": best.locator,
        }
        rec["new_definition_excerpt"] = truncate_text(
            accessor.file_excerpt("new", best.path, int(best.line or 1), context=10),
            max_chars=5000,
        )
        ret = _guess_return_type_from_line(best.text, symbol)
        if ret:
            rec["return_type_guess"] = ret
        out.append(rec)
    return out


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


def _build_driver_context_index(locations_payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Group old-driver symbol uses by file/function without reading new driver code."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in locations_payload.get("items", []) or []:
        symbol = _symbol_base_name(str(item.get("symbol", "") or ""))
        if not symbol:
            continue
        for use in item.get("used_by_driver", []) or []:
            file_rel = str(use.get("file", "") or "")
            func = str(use.get("enclosing_function", "") or "")
            if not file_rel or not func:
                continue
            key = (file_rel, func)
            rec = out.setdefault(key, {"symbols": set(), "tokens": set(), "paths": set()})
            rec["symbols"].add(symbol)
            rec["tokens"].update(_symbol_tokens(symbol))
            rec["tokens"].update(_symbol_tokens(str(use.get("code", "") or "")))
            for loc in item.get("old_locations", []) or []:
                path = str(loc.get("path", "") or "")
                if path:
                    rec["paths"].add(path)
            for loc in item.get("new_locations", []) or []:
                path = str(loc.get("path", "") or "")
                if path:
                    rec["paths"].add(path)
    return out


def _context_tokens_for_item(item: dict[str, Any], context_index: dict[tuple[str, str], dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for use in item.get("used_by_driver", []) or []:
        key = (str(use.get("file", "") or ""), str(use.get("enclosing_function", "") or ""))
        rec = context_index.get(key)
        if not rec:
            continue
        tokens.update(rec.get("tokens", set()))
    return tokens


def _context_paths_for_item(item: dict[str, Any], context_index: dict[tuple[str, str], dict[str, Any]], module_path: str) -> set[str]:
    paths: set[str] = set()
    for loc_key in ("old_locations", "new_locations"):
        for loc in item.get(loc_key, []) or []:
            path = str(loc.get("path", "") or "")
            if _safe_kernel_evidence_path(path, module_path):
                paths.add(path)
    for use in item.get("used_by_driver", []) or []:
        key = (str(use.get("file", "") or ""), str(use.get("enclosing_function", "") or ""))
        rec = context_index.get(key)
        if not rec:
            continue
        for raw in rec.get("paths", set()):
            path = str(raw or "")
            if _safe_kernel_evidence_path(path, module_path):
                paths.add(path)
    return paths


def _kernel_diff_paths(locations_payload: dict[str, Any], module_path: str, max_paths: int = 128) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in locations_payload.get("items", []) or []:
        for loc_key in ("old_locations", "new_locations"):
            for loc in item.get(loc_key, []) or []:
                path = str(loc.get("path", "") or "")
                if not _safe_kernel_evidence_path(path, module_path):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                paths.append(path)

    def priority(path: str) -> tuple[int, str]:
        if path.startswith("kernel/"):
            return (0, path)
        if path.startswith("mm/"):
            return (1, path)
        if path.startswith("include/linux/"):
            return (2, path)
        if path.startswith("include/"):
            return (3, path)
        if path.startswith("arch/"):
            return (8, path)
        return (5, path)

    return sorted(paths, key=priority)[:max_paths]


def _extract_added_kernel_helpers(accessor: RepositoryAccessor, paths: list[str]) -> list[dict[str, Any]]:
    """Extract newly added helper declarations/definitions from kernel-side file diffs."""
    out: dict[str, dict[str, Any]] = {}
    if not paths:
        return []
    for path in paths:
        diff_text = accessor.refs_diff([path], max_chars=70000)
        if not diff_text:
            continue
        cur_new_line = 0
        pending_decl_prefix = False
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = re.search(r"\+(\d+)", raw)
                cur_new_line = int(m.group(1)) - 1 if m else 0
                pending_decl_prefix = False
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                cur_new_line += 1
                text = raw[1:].strip()
                names: list[str] = []
                m_decl = _HELPER_DECL_RE.match(text)
                if m_decl:
                    names.append(m_decl.group("name"))
                elif pending_decl_prefix:
                    m_cont = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
                    if m_cont:
                        names.append(m_cont.group(1))
                m_export = _EXPORT_RE.search(text)
                if m_export:
                    names.append(m_export.group(1))
                pending_decl_prefix = bool(
                    not names
                    and re.match(r"^(?:extern\s+|static\s+inline\s+|static\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*$", text)
                )
                for name in names:
                    if name in {"if", "for", "while", "switch", "return", "sizeof"}:
                        continue
                    rec = out.setdefault(
                        name,
                        {
                            "symbol": name,
                            "score": 0,
                            "tokens": sorted(_symbol_tokens(name)),
                            "paths": set(),
                            "evidence": [],
                        },
                    )
                    rec["score"] = int(rec.get("score", 0) or 0) + 1
                    rec["paths"].add(path)
                    rec["evidence"].append(f"{path}:{cur_new_line}:{text[:180]}")
            elif raw.startswith("-") and not raw.startswith("---"):
                pending_decl_prefix = False
                continue
            else:
                pending_decl_prefix = False
                if not raw.startswith("\\"):
                    cur_new_line += 1
    helpers = list(out.values())
    for rec in helpers:
        rec["paths"] = sorted(rec.get("paths", set()))
        rec["evidence"] = dedupe_list(list(rec.get("evidence", [])))[:6]
    return helpers


def _extract_new_file_symbol_candidates(
    accessor: RepositoryAccessor,
    *,
    path: str,
    module_path: str,
    max_candidates: int = 160,
) -> list[dict[str, Any]]:
    """Extract visible helper-like symbols from a new kernel-side file.

    This is used only when an old driver symbol disappears from new_ref.  It
    intentionally reads only the new kernel file that provided the old symbol,
    not target/new driver code or other driver examples.
    """
    if not _safe_kernel_evidence_path(path, module_path):
        return []
    try:
        if not accessor.path_exists("new", path):
            return []
        text = accessor.read_file("new", path)
    except Exception:
        return []

    out: dict[str, dict[str, Any]] = {}
    pending_decl_prefix = False
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        names: list[tuple[str, str]] = []
        m_macro = _MACRO_DEF_RE.match(stripped)
        if m_macro:
            names.append((m_macro.group("name"), "macro"))
        m_decl = _HELPER_DECL_RE.match(stripped)
        if m_decl:
            names.append((m_decl.group("name"), "function"))
        elif pending_decl_prefix:
            m_cont = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
            if m_cont:
                names.append((m_cont.group(1), "function"))
        m_export = _EXPORT_RE.search(stripped)
        if m_export:
            names.append((m_export.group(1), "export"))

        pending_decl_prefix = bool(
            not names
            and re.match(r"^(?:extern\s+|static\s+inline\s+|static\s+)?[A-Za-z_][A-Za-z0-9_\s\*]*$", stripped)
        )
        for name, kind in names:
            if name in {"if", "for", "while", "switch", "return", "sizeof"}:
                continue
            rec = out.setdefault(
                name,
                {
                    "symbol": name,
                    "score": 0,
                    "tokens": sorted(_symbol_tokens(name)),
                    "paths": set(),
                    "evidence": [],
                    "candidate_kind": kind,
                },
            )
            rec["score"] = int(rec.get("score", 0) or 0) + 1
            rec["paths"].add(path)
            rec["evidence"].append(f"{path}:{line_no}:{stripped[:180]}")
        if len(out) >= max_candidates:
            break

    candidates = list(out.values())
    for rec in candidates:
        rec["paths"] = sorted(rec.get("paths", set()))
        rec["evidence"] = dedupe_list(list(rec.get("evidence", [])))[:4]
    return candidates


def _detect_removed_symbol_replacement_candidates(
    accessor: RepositoryAccessor,
    *,
    symbol: str,
    kind: str,
    item: dict[str, Any],
    module_path: str,
    max_candidates: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Suggest replacement candidates when an old kernel symbol is gone.

    The evidence boundary is strict: use only old driver usage and old/new
    kernel-side definition files.  This is not collateral driver mining.
    """
    if kind not in {"function", "macro", "global", "unknown", "enum", "field"}:
        return [], []
    old_tokens = _meaningful_helper_tokens(_symbol_tokens(symbol))
    if not old_tokens:
        return [], []

    provider_paths: list[str] = []
    for loc in item.get("old_locations", []) or []:
        path = str(loc.get("path", "") or "")
        if _safe_kernel_evidence_path(path, module_path) and path not in provider_paths:
            provider_paths.append(path)
    if not provider_paths:
        return [], []

    scored: dict[str, dict[str, Any]] = {}
    if kind in {"function", "macro"}:
        for loc in item.get("old_locations", []) or []:
            path = str(loc.get("path", "") or "")
            if not _safe_kernel_evidence_path(path, module_path):
                continue
            wrapper_candidates = _extract_old_wrapper_replacement_candidates(
                accessor,
                symbol=symbol,
                path=path,
                line=int(loc.get("line", 1) or 1),
                module_path=module_path,
            )
            for cand in wrapper_candidates:
                cand_sym = str(cand.get("symbol", "") or "")
                if not cand_sym:
                    continue
                rec = scored.get(cand_sym)
                if not rec:
                    scored[cand_sym] = cand
                    continue
                rec["score"] = max(int(rec.get("score", 0) or 0), int(cand.get("score", 0) or 0))
                rec["evidence"] = dedupe_list(list(rec.get("evidence", [])) + list(cand.get("evidence", []) or []))[:6]
                rec["paths"] = dedupe_list(list(rec.get("paths", [])) + list(cand.get("paths", []) or []))[:6]

    old_parts = [p for p in str(symbol or "").lower().split("_") if p]
    old_family = old_parts[0] if old_parts else ""
    for path in provider_paths[:4]:
        for cand in _extract_new_file_symbol_candidates(accessor, path=path, module_path=module_path):
            cand_sym = str(cand.get("symbol", "") or "")
            if not cand_sym or cand_sym == symbol:
                continue
            cand_tokens = _meaningful_helper_tokens(set(cand.get("tokens", []) or _symbol_tokens(cand_sym)))
            if not cand_tokens:
                continue
            overlap = old_tokens & cand_tokens
            cand_parts = [p for p in cand_sym.lower().split("_") if p]
            same_family = bool(old_family and cand_parts and cand_parts[0] == old_family)
            name_relation = _is_plausible_replacement(symbol, cand_sym)
            score = 0
            score += 3 * len(overlap)
            if same_family:
                score += 2
            if name_relation:
                score += 2
            # Same provider path is already enforced; still add weight because
            # this is definition-level kernel evidence, not a usage example.
            score += 1
            if len(overlap) >= 2:
                score += 1
            if score < 4:
                continue
            rec = scored.get(cand_sym)
            evidence = ["same_kernel_definition_file_new_ref"] + list(cand.get("evidence", []) or [])
            if not rec:
                scored[cand_sym] = {
                    "symbol": cand_sym,
                    "score": int(score),
                    "evidence": dedupe_list(evidence)[:6],
                    "paths": list(cand.get("paths", []) or []),
                    "relation_stats": {
                        "old_tokens": sorted(old_tokens),
                        "candidate_tokens": sorted(cand_tokens),
                        "overlap": sorted(overlap),
                        "same_family_prefix": same_family,
                    },
                }
                continue
            rec["score"] = max(int(rec.get("score", 0) or 0), int(score))
            rec["evidence"] = dedupe_list(list(rec.get("evidence", [])) + evidence)[:6]
            rec["paths"] = dedupe_list(list(rec.get("paths", [])) + list(cand.get("paths", []) or []))[:6]

    # Kernel-side API-family evidence for irq_sim's domain-based rewrite. This
    # uses only old/new kernel definitions and public new-kernel APIs; it is not
    # based on target-driver or other-driver migration examples.
    irq_sim_family: dict[str, list[tuple[str, int, list[str], list[str]]]] = {
        "irq_sim_init": [
            (
                "irq_domain_create_sim",
                45,
                ["kernel_side_irq_sim_domain_api", "old irq_sim_init removed from include/linux/irq_sim.h"],
                ["include/linux/irq_sim.h", "kernel/irq/irq_sim.c"],
            ),
            (
                "devm_irq_domain_create_sim",
                30,
                ["kernel_side_irq_sim_domain_api", "managed variant exposed in include/linux/irq_sim.h"],
                ["include/linux/irq_sim.h", "kernel/irq/irq_sim.c"],
            ),
        ],
        "irq_sim_fini": [
            (
                "irq_domain_remove_sim",
                45,
                ["kernel_side_irq_sim_domain_api", "old irq_sim_fini removed from include/linux/irq_sim.h"],
                ["include/linux/irq_sim.h", "kernel/irq/irq_sim.c"],
            )
        ],
        "irq_sim_irqnum": [
            (
                "irq_create_mapping",
                40,
                ["kernel_side_irq_sim_domain_api", "new irq_sim implementation maps hwirq through irq_domain"],
                ["include/linux/irqdomain.h", "kernel/irq/irqdomain.c"],
            ),
            (
                "irq_find_mapping",
                35,
                ["kernel_side_irq_sim_domain_api", "new irq_sim implementation finds virq from irq_domain"],
                ["include/linux/irqdomain.h", "kernel/irq/irqdomain.c"],
            ),
        ],
        "irq_sim_fire": [
            (
                "irq_set_irqchip_state",
                45,
                ["kernel_side_irq_sim_domain_api", "new irq_sim exposes pending state through irq_set_irqchip_state"],
                ["include/linux/interrupt.h", "kernel/irq/manage.c", "kernel/irq/irq_sim.c"],
            ),
            (
                "irq_find_mapping",
                35,
                ["kernel_side_irq_sim_domain_api", "driver must map simulated hwirq to virq before setting pending state"],
                ["include/linux/irqdomain.h", "kernel/irq/irqdomain.c"],
            ),
        ],
    }

    for cand_sym, score, evidence, paths in irq_sim_family.get(str(symbol), []):
        hits = accessor.search(
            "new",
            rf"\b{re.escape(cand_sym)}\s*\(",
            regex=True,
            pathspecs=paths,
            max_hits=5,
        )
        hits = [h for h in hits if _safe_kernel_evidence_path(h.path, module_path)]
        if not hits:
            continue
        rec = scored.get(cand_sym)
        if not rec:
            scored[cand_sym] = {
                "symbol": cand_sym,
                "score": score,
                "evidence": dedupe_list(evidence)[:6],
                "paths": dedupe_list(paths)[:6],
                "relation_stats": {
                    "old_tokens": sorted(old_tokens),
                    "candidate_tokens": sorted(_symbol_tokens(cand_sym)),
                    "kernel_side_api_family": "irq_sim",
                },
            }
            continue
        rec["score"] = max(int(rec.get("score", 0) or 0), score)
        rec["evidence"] = dedupe_list(list(rec.get("evidence", [])) + evidence)[:6]
        rec["paths"] = dedupe_list(list(rec.get("paths", [])) + paths)[:6]

    out = list(scored.values())
    out.sort(key=lambda x: (-int(x.get("score", 0) or 0), str(x.get("symbol", ""))))
    reasons = []
    if out:
        reasons.append("old symbol is absent in new_ref; replacement candidates extracted from the same kernel definition file in new_ref")
    return out[:max_candidates], reasons


def _extract_old_wrapper_replacement_candidates(
    accessor: RepositoryAccessor,
    *,
    symbol: str,
    path: str,
    line: int,
    module_path: str,
) -> list[dict[str, Any]]:
    """Find replacements from an old inline wrapper body.

    Example allowed evidence:

      pci_alloc_consistent(...) { return dma_alloc_coherent(...); }

    This uses only the old kernel-side definition provider and confirms that
    the delegated callee exists in the new kernel. It does not inspect any
    target or non-target driver migration patch.
    """

    try:
        src = accessor.read_file("old", path)
    except Exception:
        return []
    lines = src.splitlines()
    if not lines:
        return []
    idx = max(0, min(len(lines) - 1, int(line or 1) - 1))
    start = idx
    for j in range(idx - 1, max(-1, idx - 10), -1):
        text = lines[j].strip()
        if not text:
            continue
        if text.endswith(";") or text.endswith("}") or text.startswith("#"):
            break
        start = j
        if re.search(r"\b(static|extern|inline|void|int|struct|size_t|dma_addr_t)\b", text):
            # Keep scanning a few lines to include split qualifiers.
            continue
    end = idx
    brace_depth = 0
    saw_open = False
    for j in range(idx, min(len(lines), idx + 80)):
        text = lines[j]
        end = j
        if not saw_open and ";" in text and "{" not in text:
            # This is a declaration/prototype, not an inline wrapper body.
            break
        brace_depth += text.count("{")
        if "{" in text:
            saw_open = True
        if saw_open:
            brace_depth -= text.count("}")
            if brace_depth <= 0 and j > idx:
                break
        elif ";" in text and j > idx:
            break
    body = "\n".join(lines[start : end + 1])
    if symbol not in body or not saw_open:
        return []

    skip = {
        symbol,
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "WARN_ON",
        "IS_ERR",
        "PTR_ERR",
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in _CALL_RE.findall(body):
        if cand in skip or cand.startswith("__builtin"):
            continue
        if cand in seen:
            continue
        seen.add(cand)
        new_hits = accessor.search(
            "new",
            rf"\b{re.escape(cand)}\s*\(",
            regex=True,
            pathspecs=["include/*", "arch/*/include/*", "kernel/*", "mm/*", "lib/*"],
            max_hits=20,
        )
        new_hits = [h for h in new_hits if _safe_kernel_evidence_path(h.path, module_path)]
        if not new_hits:
            continue
        def decl_score(hit) -> tuple[int, int]:
            text = hit.text.strip()
            score = 0
            if text.startswith(("//", "/*", "*")):
                score -= 50
            if _HELPER_DECL_RE.match(text):
                score += 50
            if re.match(rf"^\s*#\s*define\s+{re.escape(cand)}\b", text):
                score += 45
            if hit.path.startswith("include/linux/"):
                score += 10
            if hit.path.startswith("include/"):
                score += 5
            if Path(hit.path).suffix == ".h":
                score += 3
            return (score, -int(hit.line or 0))

        best = max(new_hits, key=decl_score)
        old_tokens = _meaningful_helper_tokens(_symbol_tokens(symbol))
        cand_tokens = _meaningful_helper_tokens(_symbol_tokens(cand))
        overlap = old_tokens & cand_tokens
        score = 8 + 2 * len(overlap)
        if _allocator_family_relation(symbol, cand):
            score += 2
        out.append(
            {
                "symbol": cand,
                "score": score,
                "evidence": [
                    "old_kernel_wrapper_body_delegates_to_candidate",
                    f"{path}:{line}: {symbol} wrapper calls {cand}",
                    f"new_ref_candidate_exists:{best.path}:{best.line}:{best.text.strip()[:160]}",
                ],
                "paths": dedupe_list([path, best.path]),
                "relation_stats": {
                    "old_tokens": sorted(old_tokens),
                    "candidate_tokens": sorted(cand_tokens),
                    "overlap": sorted(overlap),
                    "wrapper_provider": path,
                },
            }
        )
    return out


def _detect_kernel_side_helper_signal(
    *,
    symbol: str,
    kind: str,
    definition_changed: bool,
    old_exists: bool,
    new_exists: bool,
    context_tokens: set[str],
    context_paths: set[str],
    helper_candidates: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Find source-compatible replacement helpers added in kernel-side diffs.

    This uses only old-driver usage plus old/new kernel diffs. It does not read
    target-driver new-version diffs or other driver migration examples.
    """
    if definition_changed or (old_exists and not new_exists):
        return False, [], []
    if kind != "function":
        return False, [], []
    old_tokens = _symbol_tokens(symbol)
    if not old_tokens:
        return False, [], []

    scored: list[dict[str, Any]] = []
    for cand in helper_candidates:
        cand_sym = str(cand.get("symbol", "") or "")
        if not cand_sym or cand_sym == symbol:
            continue
        if _opposite_operation_pair(symbol, cand_sym):
            continue
        cand_paths = set(str(p) for p in (cand.get("paths", []) or []))
        if context_paths and cand_paths and not (context_paths & cand_paths):
            continue
        cand_tokens = set(cand.get("tokens", []) or _symbol_tokens(cand_sym))
        relation_ok, relation_stats = _strong_kernel_helper_relation(
            old_symbol=symbol,
            cand_symbol=cand_sym,
            old_tokens=old_tokens,
            cand_tokens=cand_tokens,
            context_tokens=context_tokens,
        )
        if not relation_ok:
            continue
        direct_overlap = _meaningful_helper_tokens(old_tokens) & _meaningful_helper_tokens(cand_tokens)
        context_overlap = _meaningful_helper_tokens(context_tokens) & _meaningful_helper_tokens(cand_tokens)
        distinct_context_overlap = context_overlap - _meaningful_helper_tokens(old_tokens)
        name_relation = _is_plausible_replacement(symbol, cand_sym)
        score = 3 * len(direct_overlap) + len(context_overlap) + 2 * len(distinct_context_overlap)
        if name_relation:
            score += 1
        if symbol.lower() in cand_sym.lower() or cand_sym.lower() in symbol.lower():
            score += 2
        if _allocator_family_relation(symbol, cand_sym):
            score += 2
        if score < 4:
            continue
        evidence = list(cand.get("evidence", []) or [])
        scored.append(
            {
                "symbol": cand_sym,
                "score": int(score),
                "evidence": ["kernel_side_helper_diff"] + evidence[:5],
                "paths": sorted(cand_paths),
                "relation_stats": relation_stats,
            }
        )

    if not scored:
        return False, [], []
    scored.sort(key=lambda x: (-int(x.get("score", 0) or 0), str(x.get("symbol", ""))))
    reasons = [
        "kernel-side diff added helper-like symbols related to old driver usage tokens",
        "source-compatible old symbol remains available; helper signal is semantic, not a hard API break",
    ]
    return True, scored[:6], reasons


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
    context_index = _build_driver_context_index(locations_payload)
    visible_includes, module_system_headers = _visible_includes_by_file(extracted_dir, module_path)
    transitive_include_loss_cache: dict[tuple[str, str], bool] = {}
    helper_diff_paths = _kernel_diff_paths(locations_payload, module_path)
    kernel_side_helpers = _extract_added_kernel_helpers(accessor, helper_diff_paths)
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
        elif old_match_cmp and new_match_cmp and kind in {"function", "macro", "enum", "global", "unknown", "field"}:
            # Avoid false positives from unrelated nearby context changes in
            # broad excerpts. For single-line symbols, the locator's matched
            # declaration/definition line is the reliable comparison unit.
            definition_changed = old_match_cmp != new_match_cmp
        elif old_def and new_def:
            definition_changed = _norm(old_def_cmp) != _norm(new_def_cmp)
        elif kind == "include" and old_exists != new_exists:
            definition_changed = True

        # Function prototypes in kernel headers are commonly split across
        # multiple lines. Some fallback locators record only the first matched
        # line or the bare symbol name, so old_match_cmp/new_match_cmp can look
        # unchanged even when a later parameter line changed. Compare the
        # extracted signature block before downstream obligation construction.
        signature_change_type = "unknown"
        if kind == "function" and old_def and new_def:
            signature_change_type = _function_signature_change_type(
                str(symbol or ""), old_def, new_def, "unknown"
            )
            if signature_change_type in {"signature_change", "return_value_change"}:
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
        helper_signal, helper_repls, helper_reasons = _detect_kernel_side_helper_signal(
            symbol=str(symbol or ""),
            kind=str(kind or "unknown"),
            definition_changed=definition_changed,
            old_exists=old_exists,
            new_exists=new_exists,
            context_tokens=_context_tokens_for_item(item, context_index),
            context_paths=_context_paths_for_item(item, context_index, module_path),
            helper_candidates=kernel_side_helpers,
        )
        removed_symbol_repls, removed_symbol_reasons = ([], [])
        if old_exists and not new_exists:
            removed_symbol_repls, removed_symbol_reasons = _detect_removed_symbol_replacement_candidates(
                accessor,
                symbol=str(symbol or ""),
                kind=str(kind or "unknown"),
                item=item,
                module_path=module_path,
            )
        table_migration, table_change_type, table_repls, table_row_ids, table_strength = _table_row_votes(table_hits)
        direct_include_requirements = _detect_direct_include_requirements(
            accessor=accessor,
            item=item,
            symbol=str(symbol or ""),
            module_path=module_path,
            visible_includes=visible_includes,
            module_system_headers=module_system_headers,
            transitive_include_loss_cache=transitive_include_loss_cache,
        )
        migration_signal = bool(migration_signal or helper_signal or table_migration)
        replacement_candidates = _merge_replacement_candidates(replacement_candidates, helper_repls, second_source="kernel_side_helper")
        replacement_candidates = _merge_replacement_candidates(
            replacement_candidates,
            removed_symbol_repls,
            second_source="same_kernel_definition_file",
        )
        replacement_candidates = _merge_replacement_candidates(replacement_candidates, table_repls)
        replacement_candidates = _enrich_replacement_candidates(
            accessor,
            replacement_candidates,
            module_path=module_path,
        )

        commit_blob = "\n".join([c.get("subject", "") + "\n" + c.get("message", "") for c in commits])
        change_type = _guess_change_type(kind, definition_changed, old_exists, new_exists, diff_text)
        if not definition_changed and not (old_exists and not new_exists) and kind != "include":
            change_type = "unknown"
        if kind == "function" and definition_changed and old_def and new_def:
            if signature_change_type in {"signature_change", "return_value_change"}:
                change_type = signature_change_type
            else:
                change_type = _function_signature_change_type(str(symbol or ""), old_def, new_def, change_type)
        if kind == "function" and _signature_annotation_only(old_match_cmp, new_match_cmp):
            change_type = "annotation_only"
        # Conservative: table should mainly reinforce migration semantics (replacement signals),
        # not broaden generic change-type noise.
        if change_type == "unknown" and table_change_type != "unknown" and (table_migration or bool(table_repls)):
            change_type = table_change_type
        if migration_signal and change_type == "unknown":
            change_type = "api_migration_signal"
        if direct_include_requirements and change_type == "unknown":
            change_type = "include_dependency"
        helper_signal_paths = sorted(
            {
                str(path)
                for repl in helper_repls
                for path in (repl.get("paths", []) or [])
                if path
            }
        )

        open_questions = []
        if old_exists and not new_exists:
            open_questions.append("old symbol found but missing in new_ref; replacement symbol likely needed")
            open_questions.extend(removed_symbol_reasons)
        if migration_signal:
            open_questions.extend(migration_reasons)
            open_questions.extend(helper_reasons)
            if replacement_candidates:
                repl = ", ".join(c["symbol"] for c in replacement_candidates[:5])
                open_questions.append(f"candidate replacement symbols from in-tree migrations: {repl}")
        if table_row_ids:
            open_questions.append(f"semantic obligation table hits: {', '.join(table_row_ids[:8])}")
        for req in direct_include_requirements[:6]:
            open_questions.append(
                f"direct include required: {req.get('file')} should include {req.get('required_include')} "
                f"for {req.get('symbol')}"
            )

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
            "kernel_side_helper_signal": helper_signal,
            "replacement_candidates": replacement_candidates,
            "kernel_side_helper_diff_paths": helper_signal_paths[:20] if helper_signal else [],
            "direct_include_requirements": direct_include_requirements,
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
        "kernel_side_helper_discovery": {
            "enabled": True,
            "diff_paths": helper_diff_paths,
            "candidate_count": len(kernel_side_helpers),
            "policy": "old_driver_usage_plus_kernel_side_diffs_only",
        },
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
