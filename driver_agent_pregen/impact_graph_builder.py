from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .module_source_loader import ModuleSourceResult
from .utils import ensure_dir, read_text, safe_filename, write_json, write_text


def _read_line_slice(path: Path, start_line: int, end_line: int) -> str:
    text = read_text(path)
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, start_line)
    end = min(len(lines), end_line)
    out = []
    for i in range(start, end + 1):
        out.append(f"{i:5d}: {lines[i - 1]}")
    return "\n".join(out)


def _build_reachability(roots: list[dict], edges: list[dict]) -> dict[str, set[str]]:
    adj = defaultdict(set)
    for e in edges:
        adj[e.get("caller")].add(e.get("callee"))

    out: dict[str, set[str]] = {}
    for cb in roots:
        root = cb.get("root_function")
        if not root:
            continue
        seen = set([root])
        q = deque([root])
        while q:
            cur = q.popleft()
            for nxt in adj.get(cur, set()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                q.append(nxt)
        out[root] = seen
    return out


def _migration_callsite_gate(local_slice: str, symbol_name: str, symbol_meta: dict[str, Any]) -> bool:
    if not symbol_meta.get("migration_signal"):
        return True
    candidates = [c.get("symbol", "") for c in symbol_meta.get("replacement_candidates", []) if c.get("symbol")]
    if not candidates:
        return False

    old = symbol_name
    text = local_slice or ""
    # Slices are persisted with "<lineno>: <code>" prefixes; strip them before
    # call-shape regex checks to avoid false negatives on wrapped callsites.
    text = re.sub(r"^\s*\d+:\s?", "", text, flags=re.M)
    # For non-call symbols (e.g., system_wq), gate by token presence.
    old_token_seen = bool(re.search(rf"\b{re.escape(old)}\b", text))
    old_call_seen = bool(re.search(rf"\b{re.escape(old)}\s*\(", text))
    if not old_token_seen:
        return False

    cand_objs = any(c.endswith("_objs") for c in candidates)
    cand_obj = any(c.endswith("_obj") for c in candidates)
    cand_flex = any(c.endswith("_flex") for c in candidates)

    # Non-allocator migration signals: token-level hit is enough once replacement
    # candidates are evidenced by kernel-side commits.
    if old.lower() not in ALLOC_FAMILY:
        return True

    if not old_call_seen:
        return False

    if cand_flex and re.search(rf"\b{re.escape(old)}\s*\(\s*struct_size\s*\(", text):
        return True

    if cand_objs:
        if re.search(rf"\b{re.escape(old)}\s*\(\s*[^,\n]+,\s*sizeof\s*\(", text, flags=re.S):
            return True

    if cand_obj:
        if re.search(rf"\b{re.escape(old)}\s*\(\s*sizeof\s*\(", text):
            return True

    return False


TIER_ORDER = {"context": 0, "semantic": 1, "hard": 2}
EDIT_SCOPE_ORDER = {"context_only": 0, "edit_candidate": 1, "edit_required": 2}
ALLOC_FAMILY = {
    "kmalloc",
    "kzalloc",
    "kvmalloc",
    "vzalloc",
    "kmalloc_array",
    "kvmalloc_array",
    "kcalloc",
    "kvcalloc",
}
PAIR_RULES = [
    ("probe", "remove"),
    ("init", "exit"),
    ("init", "deinit"),
    ("create", "destroy"),
    ("register", "unregister"),
    ("alloc", "free"),
    ("get", "put"),
    ("enable", "disable"),
    ("open", "close"),
    ("add", "del"),
]
CLEANUP_HINTS = {"free", "release", "destroy", "remove", "detach", "exit", "unregister", "put", "disable", "close"}
SEMANTIC_CHANGE_TYPES = {"return_value_change", "ownership_change", "locking_change", "api_migration_signal"}


def _max_tier(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if TIER_ORDER.get(a, -1) >= TIER_ORDER.get(b, -1) else b


def _edit_scope_for_seed(tier: str | None) -> str:
    if tier in {"hard", "semantic"}:
        return "edit_required"
    return "context_only"


def _symbol_tier(item: dict[str, Any]) -> str | None:
    if not item:
        return None
    if item.get("kind") in {"enum", "global", "unknown"}:
        # Keep these symbols in extracted evidence, but do not let broad token
        # matches drive affected-function edits. They are too noisy without a
        # precise declaration/provider.
        return None

    ct = str(item.get("change_type_guess", "unknown"))
    old_locations = item.get("old_locations", []) or []
    new_locations = item.get("new_locations", []) or []
    old_definition = str(item.get("old_definition", "") or "").strip()
    new_definition = str(item.get("new_definition", "") or "").strip()
    missing_in_new = bool(old_locations and not new_locations) or any(
        "missing in new_ref" in str(q) for q in item.get("open_questions", []) or []
    )
    if missing_in_new:
        return "hard"

    # A driver mentioning `struct foo *` is not itself evidence that every
    # internal field change in `struct foo` requires a callsite edit. Concrete
    # field accesses are emitted separately as kind=field and can still drive
    # hard obligations.
    if item.get("kind") == "struct":
        return None

    # Kernel-side helper discovery can be useful as future semantic evidence,
    # but it is not enough to edit a target driver by default. It often means
    # "same subsystem added a helper", not "this old callsite is wrong".
    if item.get("kernel_side_helper_signal"):
        return None

    if item.get("migration_signal"):
        return "semantic"

    # Regex fallbacks may attach a broad or nearby kernel hunk to a driver token
    # without locating the actual old/new definition. Do not use such symbols to
    # drive edits unless they are a missing-symbol/include/migration case handled
    # above.
    if (
        not old_locations
        and not new_locations
        and not old_definition
        and not new_definition
        and not item.get("direct_include_requirements")
        and ct not in {"include_dependency"}
    ):
        return None

    if ct == "annotation_only":
        return "context"
    if item.get("definition_changed"):
        if item.get("kind") == "field" and ct == "struct_field_change" and (new_locations or new_definition):
            symbol = str(item.get("symbol", "") or "")
            old_definition = str(item.get("old_definition", "") or "")
            new_definition = str(item.get("new_definition", "") or "")
            if symbol and (f"(*{symbol})" in old_definition or f"(*{symbol})" in new_definition):
                return "hard"
            # The enclosing struct changed, but the concrete field is still
            # present in the new kernel definition. Treat it as context instead
            # of forcing an edit that may invent non-existent accessors.
            return "context"
        if ct == "include_dependency":
            return "hard" if item.get("direct_include_requirements") else "context"
        if ct == "return_value_change":
            return "context"
        if ct in {"ownership_change", "locking_change", "api_migration_signal"}:
            return "semantic"
        if ct in {"signature_change", "struct_field_change", "api_deprecation", "rename"}:
            return "hard"
        if item.get("kind") == "macro" and ct != "unknown":
            return "hard"
        # A changed function body or nearby struct context is evidence for
        # reasoning, but not enough to require editing every old-driver caller.
        # Hard affected functions should correspond to source compatibility
        # risk: removed symbol, signature/field change, include dependency, or
        # explicit semantic contract change.
        if ct == "unknown":
            return "context"
        return "hard"

    if ct == "return_value_change":
        return "context"
    if ct in {"ownership_change", "locking_change", "api_migration_signal"}:
        return "semantic"
    if ct == "include_dependency":
        return "hard" if item.get("direct_include_requirements") else "context"
    return None


def _is_cleanup_like(name: str) -> bool:
    low = (name or "").lower()
    for token in CLEANUP_HINTS:
        if token in low:
            return True
    return False


def _paired_cleanup_candidates(name: str) -> list[str]:
    out: list[str] = []
    low = name.lower()
    for a, b in PAIR_RULES:
        if a in low:
            out.append(low.replace(a, b, 1))
    return out


def _function_alloc_signal(trigger_reasons: list[dict[str, Any]]) -> bool:
    for tr in trigger_reasons:
        sym = str(tr.get("symbol", ""))
        m = re.match(r"^(.*) \(([^()]*)\)$", sym)
        if not m:
            continue
        sym_name = m.group(1).strip().lower()
        if sym_name in ALLOC_FAMILY:
            return True
    return False


def _strip_line_numbers(text: str) -> str:
    return re.sub(r"^\s*\d+:\s?", "", text or "", flags=re.M)


def _return_value_used_at_callsite(local_slice: str, symbol_name: str) -> bool:
    """Conservatively detect callsites that rely on a function return value.

    A return-type change is only an edit-required obligation for a particular
    driver function when the old call's result participates in control/data
    flow. Plain statement calls can remain context-only.
    """
    if not symbol_name:
        return False
    text = _strip_line_numbers(local_slice)
    sym = re.escape(symbol_name)
    call = rf"\b{sym}\s*\("
    if not re.search(call, text):
        return False
    patterns = [
        rf"\breturn\s+{call}",
        rf"\b(?:if|while|switch)\s*\([^;\n{{}}]*{call}",
        rf"{call}[^;\n]*\)\s*(?:[!=<>]=?|&&|\|\|)",
        rf"(?:^|[;\n{{}}]\s*)(?:[A-Za-z_][A-Za-z0-9_\s\*\[\]\.]*\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*{call}",
    ]
    return any(re.search(p, text, flags=re.S) for p in patterns)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _select_function_record(candidates: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    use_sites: list[tuple[str, int]] = []
    for trig in triggers:
        us = trig.get("use_site") if isinstance(trig, dict) else None
        if not isinstance(us, dict):
            continue
        file_rel = str(us.get("file", "")).strip()
        line = _to_int(us.get("line"), default=0)
        if file_rel:
            use_sites.append((file_rel, line))

    def score(rec: dict[str, Any]) -> tuple[int, int]:
        file_rel = str(rec.get("file", "")).strip()
        start = _to_int(rec.get("start_line"), default=1)
        end = _to_int(rec.get("end_line"), default=start)
        span = max(1, end - start + 1)
        ext = Path(file_rel).suffix.lower()

        s = 0
        # Prefer concrete source definitions over inline header stubs when ambiguous.
        if ext in {".c", ".cc", ".cpp", ".cxx", ".rs"}:
            s += 40
        elif ext in {".h", ".hpp", ".hh"}:
            s += 8

        if span <= 6 and ext in {".h", ".hpp", ".hh"}:
            s -= 10

        for uf, ul in use_sites:
            if uf == file_rel:
                s += 100
                if ul > 0 and start <= ul <= end:
                    s += 200

        # Tie-breaker: larger function bodies are more likely real implementations.
        return (s, span)

    return max(candidates, key=score)


def run_impact_graph_builder(
    module_result: ModuleSourceResult,
    extracted_dir: Path,
    scan_payload: dict[str, Any],
    kernel_diffs: dict[str, Any],
) -> dict[str, Any]:
    functions = scan_payload.get("driver_functions", {}).get("functions", [])
    callbacks = scan_payload.get("driver_callbacks", {}).get("callbacks", [])
    call_edges = scan_payload.get("call_graph", {}).get("edges", [])
    used_symbols = scan_payload.get("driver_used_symbols", {}).get("symbols", [])

    changed_symbols: dict[tuple[str, str], set[str]] = {}
    symbol_meta: dict[tuple[str, str], dict[str, Any]] = {}
    symbol_tiers: dict[tuple[str, str], str] = {}
    for item in kernel_diffs.get("items", []):
        key = (item.get("symbol"), item.get("kind"))
        symbol_meta[key] = item
        tier = _symbol_tier(item)
        if not tier:
            continue
        reasons = changed_symbols.setdefault(key, set())
        symbol_tiers[key] = tier
        if item.get("definition_changed"):
            reasons.add("definition_changed")
        if item.get("change_type_guess") == "include_dependency":
            reasons.add("include_dependency")
        if item.get("migration_signal"):
            reasons.add("migration_signal")
        if item.get("change_type_guess"):
            reasons.add(f"change_type:{item.get('change_type_guess')}")

    fn_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in functions:
        name = f.get("name")
        if name:
            fn_index[str(name)].append(f)
    callers_map = defaultdict(list)
    callees_map = defaultdict(list)
    for e in call_edges:
        caller = e.get("caller")
        callee = e.get("callee")
        if caller and callee:
            callers_map[callee].append(caller)
            callees_map[caller].append(callee)

    fn_to_changed = defaultdict(set)
    fn_to_reasons = defaultdict(list)
    fn_to_tier: dict[str, str] = {}
    for sym in used_symbols:
        key = (sym.get("name"), sym.get("kind"))
        fn = sym.get("enclosing_function")
        if fn and key in changed_symbols and changed_symbols[key]:
            tier = symbol_tiers.get(key)
            if not tier:
                continue
            fn_to_changed[fn].add(f"{key[0]} ({key[1]})")
            fn_to_reasons[fn].append(
                {
                    "symbol": f"{key[0]} ({key[1]})",
                    "reasons": sorted(changed_symbols.get(key, set())),
                    "tier": tier,
                    "use_site": {
                        "file": sym.get("file"),
                        "line": sym.get("line"),
                        "code": sym.get("code"),
                    },
                }
            )
            fn_to_tier[fn] = _max_tier(fn_to_tier.get(fn), tier)

    reach = _build_reachability(callbacks, call_edges)

    callback_graph = {
        "roots": callbacks,
        "reachable": {root: sorted(list(nodes)) for root, nodes in reach.items()},
        "edges": call_edges,
    }
    write_json(extracted_dir / "callback_graph.json", callback_graph)

    slice_dir = extracted_dir / "affected_function_slices"
    ensure_dir(slice_dir)
    context_slice_dir = extracted_dir / "context_function_slices"
    ensure_dir(context_slice_dir)

    affected_functions = []
    edit_required_functions = []
    edit_candidate_functions = []
    context_only_functions = []
    excluded_seed_functions = []
    out_edges = []
    fn_records: dict[str, dict[str, Any]] = {}
    callback_roots_by_fn: dict[str, list[dict[str, Any]]] = {}

    for fn_name, symbols in sorted(fn_to_changed.items()):
        f = _select_function_record(fn_index.get(fn_name, []), fn_to_reasons.get(fn_name, []))
        if not f:
            continue

        file_rel = f.get("file")
        start_line = int(f.get("start_line", 1))
        end_line = int(f.get("end_line", start_line))
        src_path = module_result.copied_root / file_rel
        local_slice = _read_line_slice(src_path, start_line, end_line) if src_path.exists() else ""

        slice_filename = safe_filename(f"{fn_name}_{file_rel.replace('/', '_')}", suffix="c")
        slice_path = slice_dir / slice_filename
        write_text(slice_path, local_slice + ("\n" if local_slice else ""))

        # Filter migration-signal-only symbols with callsite-shape checks to reduce noise.
        kept_reasons = []
        kept_symbols = set()
        dropped_reasons = []
        kept_tier: str | None = None
        for trig in fn_to_reasons.get(fn_name, []):
            sym_text = trig.get("symbol", "")
            m = re.match(r"^(.*) \(([^()]*)\)$", sym_text)
            if not m:
                continue
            sym_name, sym_kind = m.group(1), m.group(2)
            meta = symbol_meta.get((sym_name, sym_kind), {})
            reasons = set(r for r in trig.get("reasons", []) if not str(r).startswith("change_type:"))
            if reasons == {"migration_signal"} and not _migration_callsite_gate(local_slice, sym_name, meta):
                dropped_reasons.append(
                    {
                        "symbol": sym_text,
                        "reason": "migration_callsite_gate_failed",
                        "tier": trig.get("tier", "semantic"),
                    }
                )
                continue
            trig = dict(trig)
            if (
                str(meta.get("change_type_guess", "")) == "return_value_change"
                and _return_value_used_at_callsite(local_slice, sym_name)
            ):
                trig["tier"] = "hard"
                trig.setdefault("reasons", [])
                if "return_value_used_at_callsite" not in trig["reasons"]:
                    trig["reasons"] = list(trig["reasons"]) + ["return_value_used_at_callsite"]
            kept_reasons.append(trig)
            kept_symbols.add(sym_text)
            kept_tier = _max_tier(kept_tier, str(trig.get("tier", "context")))

        if not kept_symbols:
            excluded_seed_functions.append(
                {
                    "function": fn_name,
                    "file": file_rel,
                    "reason": "all_symbol_triggers_filtered",
                    "dropped_reasons": dropped_reasons,
                    "candidate_tier": fn_to_tier.get(fn_name, "unknown"),
                }
            )
            continue

        ordinary_callers = sorted(set(callers_map.get(fn_name, [])))
        ordinary_callees = sorted(set(callees_map.get(fn_name, [])))

        cb_roots = []
        for cb in callbacks:
            root_fn = cb.get("root_function")
            if root_fn == fn_name or fn_name in reach.get(root_fn, set()):
                cb_roots.append(
                    {
                        "root_kind": cb.get("root_kind", "unknown"),
                        "root_function": root_fn,
                        "registration_site": cb.get("registration_site"),
                        "evidence": cb.get("evidence"),
                    }
                )
                out_edges.append({"from": root_fn, "to": fn_name, "kind": "callback_path"})

        for callee in ordinary_callees:
            out_edges.append({"from": fn_name, "to": callee, "kind": "calls"})

        for sym in sorted(kept_symbols):
            out_edges.append({"from": fn_name, "to": sym, "kind": "uses_kernel_symbol"})
        edit_scope = _edit_scope_for_seed(kept_tier)
        rec = {
            "function": fn_name,
            "file": file_rel,
            "start_line": start_line,
            "end_line": end_line,
            "tier": kept_tier or "context",
            "edit_scope": edit_scope,
            "uses_changed_kernel_symbols": sorted(kept_symbols),
            "trigger_reasons": kept_reasons,
            "ordinary_callers": ordinary_callers,
            "ordinary_callees": ordinary_callees,
            "callback_roots": cb_roots or [
                {
                    "root_kind": "unknown",
                    "root_function": None,
                    "registration_site": None,
                    "evidence": "callback root not found",
                }
            ],
            "local_slice_path": f"affected_function_slices/{slice_filename}",
        }
        fn_records[fn_name] = rec
        callback_roots_by_fn[fn_name] = cb_roots

    # Split direct seeds into required edits vs context-only seeds.
    edit_required_seed_names = set()
    context_seed_names = set()
    for fn_name, rec in fn_records.items():
        if rec.get("edit_scope") == "edit_required":
            edit_required_seed_names.add(fn_name)
        else:
            context_seed_names.add(fn_name)

    # Expansion maps: function -> reasons/evidence. Candidate functions are
    # allowed edit targets only when the evidence requires cross-function edits;
    # context-only functions are reasoning context and should not be patched.
    candidate_reason_map: dict[str, dict[str, Any]] = {}
    context_reason_map: dict[str, dict[str, Any]] = {}

    def _add_candidate(fn_name: str, reason: str, evidence: str) -> None:
        if fn_name in edit_required_seed_names:
            return
        info = candidate_reason_map.setdefault(fn_name, {"reasons": set(), "evidence": []})
        info["reasons"].add(reason)
        if evidence and len(info["evidence"]) < 8:
            info["evidence"].append(evidence)

    def _add_context(fn_name: str, reason: str, evidence: str) -> None:
        if fn_name in edit_required_seed_names or fn_name in candidate_reason_map:
            return
        info = context_reason_map.setdefault(fn_name, {"reasons": set(), "evidence": []})
        info["reasons"].add(reason)
        if evidence and len(info["evidence"]) < 8:
            info["evidence"].append(evidence)

    # Direct context-symbol seeds stay context-only.
    for fn_name in sorted(context_seed_names):
        _add_context(fn_name, "context_symbol_evidence", "symbol-tier=context")

    # Evidence-backed expansion around required edit seeds.
    for seed in sorted(edit_required_seed_names):
        rec = fn_records.get(seed, {})

        # 1) Direct caller expansion: callers may need return/error handling updates.
        for caller in callers_map.get(seed, []):
            if caller in fn_index:
                _add_candidate(caller, "direct_caller_of_edit_required", f"{caller}->{seed}")
                out_edges.append({"from": caller, "to": seed, "kind": "edit_candidate_direct_caller"})

        # 2) Lifecycle root and same callback chain: context only unless a
        # separate resource/caller-pairing rule promotes a function to candidate.
        cb_roots = callback_roots_by_fn.get(seed, [])
        for cb in cb_roots:
            root_fn = cb.get("root_function")
            if root_fn and root_fn in fn_index and root_fn != seed:
                _add_context(root_fn, "lifecycle_path_root", f"{root_fn} reaches {seed}")
                out_edges.append({"from": root_fn, "to": seed, "kind": "context_lifecycle_root"})
            if not root_fn:
                continue
            reach_set = reach.get(root_fn, set())
            for neigh in set(callers_map.get(seed, [])) | set(callees_map.get(seed, [])):
                if neigh in reach_set and neigh in fn_index:
                    _add_context(neigh, "same_callback_chain", f"root={root_fn};seed={seed};neighbor={neigh}")
                    out_edges.append({"from": seed, "to": neigh, "kind": "context_callback_chain"})

        # 3) Paired cleanup function by naming.
        local_fn_names = set(fn_index.keys())
        for cand_low in _paired_cleanup_candidates(seed):
            for name in local_fn_names:
                if name.lower() == cand_low and name != seed:
                    _add_candidate(name, "paired_cleanup_function", f"seed={seed};pair={name}")
                    out_edges.append({"from": seed, "to": name, "kind": "edit_candidate_paired_cleanup"})

        # 4) Resource pairing expansion (conservative).
        if _function_alloc_signal(rec.get("trigger_reasons", [])):
            near = set(callers_map.get(seed, [])) | set(callees_map.get(seed, []))
            for name in near:
                if name not in fn_index or name == seed:
                    continue
                if not _is_cleanup_like(name):
                    continue
                _add_candidate(name, "resource_pairing", f"allocator-seed={seed};cleanup={name}")
                out_edges.append({"from": seed, "to": name, "kind": "edit_candidate_resource_pairing"})

        # 5) Same driver-state field expansion. Kernel API migrations often
        # require replacing a local state representation (for example an old
        # embedded helper object plus a cached base id). Other functions that
        # access the same object->field are candidate edits, but only as a
        # conservative, evidence-tagged expansion from an edit_required seed.
        seed_text_parts: list[str] = []
        seed_file = str(rec.get("file", "") or "")
        seed_src = module_result.copied_root / seed_file
        trigger_lines = [
            _to_int(((tr.get("use_site") or {}) if isinstance(tr, dict) else {}).get("line"), 0)
            for tr in rec.get("trigger_reasons", []) or []
        ]
        trigger_lines = [ln for ln in trigger_lines if ln > 0]
        if seed_src.exists() and trigger_lines:
            src_lines = read_text(seed_src).splitlines()
            for ln in trigger_lines[:8]:
                start = max(1, ln - 8)
                end = min(len(src_lines), ln + 8)
                seed_text_parts.append("\n".join(src_lines[start - 1 : end]))
        if not seed_text_parts:
            seed_slice = rec.get("local_slice_path")
            if seed_slice:
                seed_path = extracted_dir / str(seed_slice)
                if seed_path.exists():
                    seed_text_parts.append(read_text(seed_path))
        seed_text = "\n".join(seed_text_parts)
        raw_state_fields = [
            (obj, field)
            for obj, field in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->\s*([A-Za-z_][A-Za-z0-9_]*)\b", seed_text)
            if field not in {"dev", "driver", "parent"}
        ]
        trigger_tokens: set[str] = set()
        for tr in rec.get("trigger_reasons", []) or []:
            sym_text = str(tr.get("symbol", "") or "")
            m = re.match(r"^(.*) \(([^()]*)\)$", sym_text)
            if not m:
                continue
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", m.group(1).lower()):
                if tok not in {"iio", "dev", "device", "get", "set", "put", "alloc", "free"} and len(tok) > 2:
                    trigger_tokens.add(tok)
        selected_objects = {
            obj
            for obj, field in raw_state_fields
            if set(re.findall(r"[A-Za-z][A-Za-z0-9]*", field.lower())).intersection(trigger_tokens)
        }
        state_fields = {
            (obj, field)
            for obj, field in raw_state_fields
            if obj in selected_objects
        }
        if state_fields:
            added = 0
            for name, records in fn_index.items():
                if name == seed or name in edit_required_seed_names or added >= 8:
                    continue
                f = _select_function_record(records, [])
                if not f:
                    continue
                file_rel = f.get("file")
                src_path = module_result.copied_root / file_rel
                if not src_path.exists():
                    continue
                fn_text = _read_line_slice(src_path, int(f.get("start_line", 1)), int(f.get("end_line", f.get("start_line", 1))))
                hits = sorted(
                    f"{obj}->{field}"
                    for obj, field in state_fields
                    if re.search(rf"\b{re.escape(obj)}\s*->\s*{re.escape(field)}\b", fn_text)
                )
                if not hits:
                    continue
                _add_candidate(name, "same_driver_state_field", f"seed={seed};fields={','.join(hits[:4])}")
                out_edges.append({"from": seed, "to": name, "kind": "edit_candidate_same_driver_state_field"})
                added += 1

    # Build required edit records.
    for fn_name in sorted(edit_required_seed_names):
        rec = fn_records[fn_name]
        edit_required_functions.append(rec)
        affected_functions.append(rec)

    # Build candidate edit records with slices.
    candidate_slice_dir = extracted_dir / "edit_candidate_function_slices"
    ensure_dir(candidate_slice_dir)
    for fn_name, info in sorted(candidate_reason_map.items()):
        f = _select_function_record(fn_index.get(fn_name, []), fn_to_reasons.get(fn_name, []))
        if not f:
            continue
        file_rel = f.get("file")
        start_line = int(f.get("start_line", 1))
        end_line = int(f.get("end_line", start_line))
        src_path = module_result.copied_root / file_rel
        local_slice = _read_line_slice(src_path, start_line, end_line) if src_path.exists() else ""
        slice_filename = safe_filename(f"{fn_name}_{file_rel.replace('/', '_')}", suffix="c")
        slice_path = candidate_slice_dir / slice_filename
        write_text(slice_path, local_slice + ("\n" if local_slice else ""))

        rec = {
            "function": fn_name,
            "file": file_rel,
            "start_line": start_line,
            "end_line": end_line,
            "tier": "context",
            "edit_scope": "edit_candidate",
            "candidate_reasons": sorted(info["reasons"]),
            "candidate_evidence": info["evidence"],
            "ordinary_callers": sorted(set(callers_map.get(fn_name, []))),
            "ordinary_callees": sorted(set(callees_map.get(fn_name, []))),
            "callback_roots": [
                {
                    "root_kind": cb.get("root_kind", "unknown"),
                    "root_function": cb.get("root_function"),
                    "registration_site": cb.get("registration_site"),
                    "evidence": cb.get("evidence"),
                }
                for cb in callbacks
                if cb.get("root_function") == fn_name or fn_name in reach.get(cb.get("root_function"), set())
            ],
            "local_slice_path": f"edit_candidate_function_slices/{slice_filename}",
        }
        edit_candidate_functions.append(rec)
        affected_functions.append(rec)

    # Build context-only function records with slices.
    for fn_name, info in sorted(context_reason_map.items()):
        if fn_name in candidate_reason_map:
            continue
        f = _select_function_record(fn_index.get(fn_name, []), fn_to_reasons.get(fn_name, []))
        if not f:
            continue
        file_rel = f.get("file")
        start_line = int(f.get("start_line", 1))
        end_line = int(f.get("end_line", start_line))
        src_path = module_result.copied_root / file_rel
        local_slice = _read_line_slice(src_path, start_line, end_line) if src_path.exists() else ""
        slice_filename = safe_filename(f"{fn_name}_{file_rel.replace('/', '_')}", suffix="c")
        slice_path = context_slice_dir / slice_filename
        write_text(slice_path, local_slice + ("\n" if local_slice else ""))

        context_only_functions.append(
            {
                "function": fn_name,
                "file": file_rel,
                "start_line": start_line,
                "end_line": end_line,
                "tier": "context",
                "edit_scope": "context_only",
                "context_reasons": sorted(info["reasons"]),
                "context_evidence": info["evidence"],
                "local_slice_path": f"context_function_slices/{slice_filename}",
            }
        )

    tier_counts = {"hard": 0, "semantic": 0, "context": 0}
    for rec in edit_required_functions:
        tier = rec.get("tier", "context")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    tier_counts["context"] += len(edit_candidate_functions) + len(context_only_functions)

    edit_scope_counts = {
        "edit_required": len(edit_required_functions),
        "edit_candidate": len(edit_candidate_functions),
        "context_only": len(context_only_functions),
    }
    editable_functions = [
        {
            "function": x.get("function"),
            "file": x.get("file"),
            "edit_scope": x.get("edit_scope"),
            "tier": x.get("tier"),
        }
        for x in (edit_required_functions + edit_candidate_functions)
    ]

    payload = {
        "module_path": module_result.module_path,
        "affected_tier_policy": {
            "legacy_tiers": ["hard", "semantic", "context"],
            "edit_scopes": ["edit_required", "edit_candidate", "context_only"],
            "default_edit_scopes": ["edit_required"],
            "conditional_edit_scopes": ["edit_candidate"],
            "context_only_scopes": ["context_only"],
            "policy": (
                "Patch generation may edit edit_required functions. It may edit "
                "edit_candidate functions only when the patch rationale cites their "
                "candidate evidence. context_only functions are reasoning context and "
                "must not be edited by default."
            ),
        },
        "edit_required_functions": edit_required_functions,
        "edit_candidate_functions": edit_candidate_functions,
        "context_only_functions": context_only_functions,
        "editable_functions": editable_functions,
        "context_functions": context_only_functions,
        "excluded_seed_functions": excluded_seed_functions,
        "tier_counts": tier_counts,
        "edit_scope_counts": edit_scope_counts,
        "affected_functions": affected_functions,
        "edges": out_edges,
    }
    write_json(extracted_dir / "impact_graph.json", payload)
    return payload
