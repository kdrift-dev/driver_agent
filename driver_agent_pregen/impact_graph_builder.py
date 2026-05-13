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
    if not re.search(rf"\b{re.escape(old)}\s*\(", text):
        return False

    cand_objs = any(c.endswith("_objs") for c in candidates)
    cand_obj = any(c.endswith("_obj") for c in candidates)
    cand_flex = any(c.endswith("_flex") for c in candidates)

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


def _symbol_tier(item: dict[str, Any]) -> str | None:
    if not item:
        return None
    if item.get("migration_signal"):
        return "semantic"

    ct = str(item.get("change_type_guess", "unknown"))
    if item.get("definition_changed"):
        if ct == "include_dependency":
            return "context"
        if ct in SEMANTIC_CHANGE_TYPES:
            return "semantic"
        return "hard"

    if ct in SEMANTIC_CHANGE_TYPES:
        return "semantic"
    if ct == "include_dependency":
        return "context"
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

    fn_index = {f["name"]: f for f in functions}
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
    context_functions = []
    excluded_seed_functions = []
    out_edges = []
    fn_records: dict[str, dict[str, Any]] = {}
    callback_roots_by_fn: dict[str, list[dict[str, Any]]] = {}

    for fn_name, symbols in sorted(fn_to_changed.items()):
        f = fn_index.get(fn_name)
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
        rec = {
            "function": fn_name,
            "file": file_rel,
            "start_line": start_line,
            "end_line": end_line,
            "tier": kept_tier or "context",
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

    # Split into editable seeds (hard/semantic) vs context-only seeds.
    editable_seed_names = set()
    context_seed_names = set()
    for fn_name, rec in fn_records.items():
        tier = rec.get("tier", "context")
        if tier in {"hard", "semantic"}:
            editable_seed_names.add(fn_name)
        else:
            context_seed_names.add(fn_name)

    # Context expansion map: function -> reasons/evidence
    context_reason_map: dict[str, dict[str, Any]] = {}

    def _add_context(fn_name: str, reason: str, evidence: str) -> None:
        if fn_name in editable_seed_names:
            return
        info = context_reason_map.setdefault(fn_name, {"reasons": set(), "evidence": []})
        info["reasons"].add(reason)
        if evidence and len(info["evidence"]) < 8:
            info["evidence"].append(evidence)

    # Direct context-symbol seeds stay context-only.
    for fn_name in sorted(context_seed_names):
        _add_context(fn_name, "context_symbol_evidence", "symbol-tier=context")

    # Evidence-backed expansion around editable seeds.
    for seed in sorted(editable_seed_names):
        rec = fn_records.get(seed, {})
        seed_file = rec.get("file")

        # 1) Direct caller expansion.
        for caller in callers_map.get(seed, []):
            if caller in fn_index:
                _add_context(caller, "direct_caller_of_affected_seed", f"{caller}->{seed}")
                out_edges.append({"from": caller, "to": seed, "kind": "context_direct_caller"})

        # 2) Lifecycle root and same callback chain (one-hop neighbors in same root reachability).
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
                    _add_context(name, "paired_cleanup_function", f"seed={seed};pair={name}")
                    out_edges.append({"from": seed, "to": name, "kind": "context_paired_cleanup"})

        # 4) Resource pairing expansion (conservative).
        if _function_alloc_signal(rec.get("trigger_reasons", [])):
            near = set(callers_map.get(seed, [])) | set(callees_map.get(seed, []))
            for name in near:
                if name not in fn_index or name == seed:
                    continue
                if not _is_cleanup_like(name):
                    continue
                _add_context(name, "resource_pairing", f"allocator-seed={seed};cleanup={name}")
                out_edges.append({"from": seed, "to": name, "kind": "context_resource_pairing"})

    # Build editable affected functions.
    for fn_name in sorted(editable_seed_names):
        affected_functions.append(fn_records[fn_name])

    # Build context function records with slices.
    for fn_name, info in sorted(context_reason_map.items()):
        f = fn_index.get(fn_name)
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

        context_functions.append(
            {
                "function": fn_name,
                "file": file_rel,
                "start_line": start_line,
                "end_line": end_line,
                "tier": "context",
                "context_reasons": sorted(info["reasons"]),
                "context_evidence": info["evidence"],
                "local_slice_path": f"context_function_slices/{slice_filename}",
            }
        )

    tier_counts = {"hard": 0, "semantic": 0, "context": 0}
    for rec in affected_functions:
        tier = rec.get("tier", "context")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    tier_counts["context"] += len(context_functions)

    payload = {
        "module_path": module_result.module_path,
        "affected_tier_policy": {
            "tiers": ["hard", "semantic", "context"],
            "editable_tiers": ["hard", "semantic"],
            "context_only_tiers": ["context"],
        },
        "editable_functions": [
            {"function": x.get("function"), "file": x.get("file"), "tier": x.get("tier")} for x in affected_functions
        ],
        "context_functions": context_functions,
        "excluded_seed_functions": excluded_seed_functions,
        "tier_counts": tier_counts,
        "affected_functions": affected_functions,
        "edges": out_edges,
    }
    write_json(extracted_dir / "impact_graph.json", payload)
    return payload
