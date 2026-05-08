from __future__ import annotations

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

    changed_symbols = set()
    for item in kernel_diffs.get("items", []):
        if item.get("definition_changed"):
            changed_symbols.add((item.get("symbol"), item.get("kind")))
        elif item.get("change_type_guess") == "include_dependency":
            changed_symbols.add((item.get("symbol"), item.get("kind")))

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
    for sym in used_symbols:
        key = (sym.get("name"), sym.get("kind"))
        fn = sym.get("enclosing_function")
        if fn and key in changed_symbols:
            fn_to_changed[fn].add(f"{key[0]} ({key[1]})")

    reach = _build_reachability(callbacks, call_edges)

    callback_graph = {
        "roots": callbacks,
        "reachable": {root: sorted(list(nodes)) for root, nodes in reach.items()},
        "edges": call_edges,
    }
    write_json(extracted_dir / "callback_graph.json", callback_graph)

    slice_dir = extracted_dir / "affected_function_slices"
    ensure_dir(slice_dir)

    affected_functions = []
    out_edges = []

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

        for sym in sorted(symbols):
            out_edges.append({"from": fn_name, "to": sym, "kind": "uses_kernel_symbol"})

        affected_functions.append(
            {
                "function": fn_name,
                "file": file_rel,
                "start_line": start_line,
                "end_line": end_line,
                "uses_changed_kernel_symbols": sorted(symbols),
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
        )

    payload = {
        "module_path": module_result.module_path,
        "affected_functions": affected_functions,
        "edges": out_edges,
    }
    write_json(extracted_dir / "impact_graph.json", payload)
    return payload
