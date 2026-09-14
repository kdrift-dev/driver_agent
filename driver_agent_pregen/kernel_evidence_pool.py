"""Build a reference-free, whole-excerpt pool from production extraction.

This adapter accepts a deliberately narrow production schema. It never reads
case packs, guidance, compatibility-obligation prose, or new driver files.
Metadata is allowlisted and discarded rather than passed through to prompts.
A source_reader can additionally verify every admitted source line against
Git blobs; without it the audit explicitly records extraction-only binding.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping


DOCUMENT_NAMES = (
    "module_source.json", "kernel_definition_locations.json", "kernel_definition_diffs.json",
)
_SCHEMA = "kernel_evidence_pool_v1"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_KINDS = {"enum", "macro", "function", "field", "struct", "include", "global", "unknown", "typedef"}
_KERNEL_ROOTS = {"include", "arch", "kernel", "lib", "mm", "fs", "net", "crypto", "ipc", "security", "block", "init", "virt", "rust", "scripts", "Documentation"}
_MODULE_KEYS = {
    "module_path", "case_scope", "kbuild_target", "kbuild_target_resolution", "old_ref", "new_ref",
    "old_exists", "new_path_exists", "source_files_old", "headers_old", "module_loc_old",
    "local_include_scope_risks", "note",
}
_LOCATION_ITEM_KEYS = {"symbol", "kind", "used_by_driver", "old_locations", "new_locations", "classification_confidence", "notes"}
_LOCATION_KEYS = {"path", "line", "match", "locator", "owner_struct", "field"}
_USE_KEYS = {"file", "line", "enclosing_function", "code", "owner_struct"}
_DIFF_KEYS = {"affected_func_signal_mode", "collect_commit_info", "commit_info_policy", "commit_exclude_prefixes", "semantic_obligation_table", "kernel_side_helper_discovery", "items"}
_DIFF_ITEM_KEYS = {
    "symbol", "kind", "used_by_driver", "old_definition", "new_definition", "definition_changed",
    "definition_diff", "change_type_guess", "commits", "migration_signal", "kernel_side_helper_signal",
    "replacement_candidates", "kernel_side_helper_diff_paths", "direct_include_requirements",
    "semantic_table_hit_rows", "confidence", "open_questions",
}
_REPLACEMENT_KEYS = {"symbol", "score", "evidence", "paths", "new_location", "new_definition_excerpt", "return_type_guess"}


class EvidenceBoundaryError(ValueError):
    """Input cannot be safely represented by this narrow evidence adapter."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapping(value: Any, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise EvidenceBoundaryError(f"{label}: expected an object")
    unknown = set(value) - allowed
    if unknown:
        raise EvidenceBoundaryError(f"{label}: unsupported metadata keys: {sorted(unknown)}")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise EvidenceBoundaryError(f"{label}: expected a list")
    return value


def _path(value: Any, *, kernel: bool = False) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_./+@-]+", value):
        raise EvidenceBoundaryError("invalid source path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in {".", ".."} for p in path.parts):
        raise EvidenceBoundaryError(f"noncanonical source path: {value}")
    if kernel and (path.parts[0] not in _KERNEL_ROOTS or "drivers" in path.parts):
        raise EvidenceBoundaryError(f"forbidden kernel evidence path: {value}")
    return value


def _symbol(value: Any, kind: str) -> str:
    if kind not in _KINDS:
        raise EvidenceBoundaryError(f"unsupported symbol kind: {kind}")
    if kind == "include":
        return _path(value)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceBoundaryError("invalid symbol")
    return value


def _line(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvidenceBoundaryError("invalid source line")
    return value


def _parse_excerpt(text: Any) -> list[tuple[int, str]]:
    if not isinstance(text, str):
        raise EvidenceBoundaryError("definition excerpt must be text")
    out = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(\d+): ?(.*)", line)
        if not match:
            raise EvidenceBoundaryError("definition must contain intact numbered source lines")
        number = _line(int(match[1]))
        if out and number != out[-1][0] + 1:
            raise EvidenceBoundaryError("definition source lines are not contiguous")
        out.append((number, match[2]))
    return out


def _uses(value: Any, old_sources: Mapping[str, str]) -> list[dict]:
    result = {}
    for raw in _list(value, "used_by_driver"):
        use = _mapping(raw, _USE_KEYS, "used_by_driver item")
        path = _path(use.get("file"))
        if path not in old_sources:
            raise EvidenceBoundaryError(f"use outside resolved old target: {path}")
        line = _line(use.get("line"))
        source_lines = old_sources[path].splitlines()
        if line > len(source_lines):
            raise EvidenceBoundaryError("old target use is outside source")
        function = use.get("enclosing_function")
        if function is not None and (not isinstance(function, str) or not _IDENTIFIER.fullmatch(function)):
            raise EvidenceBoundaryError("invalid old target function")
        if function is not None and not re.search(r"\b" + re.escape(function) + r"\b", old_sources[path]):
            raise EvidenceBoundaryError("function signal does not occur in old target")
        # Scanner code may be a field fragment rather than the entire source line.
        code = use.get("code", "")
        if not isinstance(code, str) or (code and code not in old_sources[path]):
            raise EvidenceBoundaryError("old target use text is not present in old source")
        record = {"path": path, "line": line, "function": function}
        result[_canonical(record)] = record
    return [result[key] for key in sorted(result)]


def _locations(value: Any) -> list[dict]:
    result = []
    for raw in _list(value, "locations"):
        loc = _mapping(raw, _LOCATION_KEYS, "location")
        path = _path(loc.get("path"), kernel=True)
        line = _line(loc.get("line"))
        if not isinstance(loc.get("match"), str):
            raise EvidenceBoundaryError("location match must be text")
        result.append({"path": path, "line": line, "match": loc["match"]})
    unique = {_canonical(item): item for item in result}
    return [unique[key] for key in sorted(unique)]


def _excerpt_binding(text: str, locations: list[dict], *, ref: str,
                     source_reader: Callable[[str, str], str] | None) -> dict | None:
    lines = _parse_excerpt(text)
    if not lines:
        if locations:
            raise EvidenceBoundaryError("located definition has no source excerpt")
        return None
    # The production builder takes its first location. Find its path without
    # depending on list order, using range and literal locator matches where possible.
    eligible = [loc for loc in locations if lines[0][0] <= loc["line"] <= lines[-1][0]]
    exact = [loc for loc in eligible if dict(lines)[loc["line"]].strip() == loc["match"].strip()]
    candidates = exact or eligible
    paths = sorted({loc["path"] for loc in candidates})
    if len(paths) != 1:
        raise EvidenceBoundaryError("excerpt path cannot be bound unambiguously")
    path = paths[0]
    if source_reader:
        raw = source_reader(ref, path)
        if not isinstance(raw, str):
            raise EvidenceBoundaryError("source_reader must return source text")
        source_lines = raw.splitlines()
        for number, code in lines:
            if number > len(source_lines) or source_lines[number - 1] != code:
                raise EvidenceBoundaryError(f"Git source mismatch: {ref}:{path}:{number}")
    content = "\n".join(code for _, code in lines)
    return {
        "ref": ref, "path": path, "start_line": lines[0][0], "end_line": lines[-1][0],
        "content": content, "content_sha256": _sha(content),
    }


def _validate_discarded_annotations(item: dict) -> None:
    # These fields never enter candidates or signals. Reject foreign evidence
    # sources even when their associated narrative would otherwise be discarded.
    if item.get("commits") or item.get("semantic_table_hit_rows"):
        raise EvidenceBoundaryError("commit or semantic-table annotations are forbidden")
    for path in _list(item.get("kernel_side_helper_diff_paths", []), "helper diff paths"):
        _path(path, kernel=True)
    if item.get("direct_include_requirements"):
        raise EvidenceBoundaryError("direct-include annotations are unsupported by this pool version")
    for raw in _list(item.get("replacement_candidates", []), "replacement candidates"):
        candidate = _mapping(raw, _REPLACEMENT_KEYS, "discarded replacement candidate")
        for path in _list(candidate.get("paths", []), "replacement paths"):
            _path(path, kernel=True)
        if candidate.get("new_location"):
            _locations([candidate["new_location"]])


def _focused_diff(old: dict | None, new: dict | None, old_label: str, new_label: str) -> str:
    return "\n".join(difflib.unified_diff(
        (old or {}).get("content", "").splitlines(), (new or {}).get("content", "").splitlines(),
        fromfile=old_label, tofile=new_label, lineterm="",
    ))


def _render(symbols: list[str], old: dict | None, new: dict | None, diff: str) -> str:
    parts = ["Kernel evidence for: " + ", ".join(symbols)]
    same = old and new and all(old[key] == new[key] for key in ("path", "start_line", "end_line", "content"))
    sides = [("old and new", old)] if same else [("old", old), ("new", new)]
    for label, excerpt in sides:
        if excerpt is None:
            parts.append(f"{label}: no located definition")
            continue
        parts.append(f"{label}: {excerpt['path']}:{excerpt['start_line']}-{excerpt['end_line']}")
        parts.append("\n".join(f"{line}: {code}" for line, code in enumerate(excerpt["content"].split("\n"), excerpt["start_line"])))
    if diff:
        parts.extend(["Focused kernel-side diff:", diff])
    return "\n\n".join(parts)


def build_kernel_evidence_pool(
    extracted_dir: str | Path | None = None, *, documents: Mapping[str, dict] | None = None,
    old_sources: Mapping[str, str] | None = None,
    source_reader: Callable[[str, str], str] | None = None,
) -> dict:
    """Return ``candidates``, minimal ``obligation_signal``, and non-prompt ``audit``.

    Supply either an extracted directory or exactly the three DOCUMENT_NAMES
    plus an old-source mapping. Candidate order is canonical path/range/symbol
    order; IDs bind refs, source ranges, excerpt bytes and recomputed diff.
    Candidates are immutable selection units: no excerpt is truncated here.
    ``source_reader(ref, path)`` may read only the requested permitted Git blob.
    """
    if (extracted_dir is None) == (documents is None):
        raise EvidenceBoundaryError("supply extracted_dir or documents, exclusively")
    if extracted_dir is not None:
        root = Path(extracted_dir)
        documents = {name: json.loads((root / name).read_text(encoding="utf-8")) for name in DOCUMENT_NAMES}
    assert documents is not None
    if set(documents) != set(DOCUMENT_NAMES):
        raise EvidenceBoundaryError("unsupported extraction documents")
    module = _mapping(documents["module_source.json"], _MODULE_KEYS, "module source")
    module_path = _path(module.get("module_path"))
    refs = {side: module.get(side + "_ref") for side in ("old", "new")}
    if any(not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs.values()):
        raise EvidenceBoundaryError("pool requires full immutable Git commit IDs")
    inventories = {}
    for key in ("source_files_old", "headers_old"):
        entries = [_path(path) for path in _list(module.get(key, []), key)]
        if len(entries) != len(set(entries)):
            raise EvidenceBoundaryError("duplicate old source inventory path")
        inventories[key] = set(entries)
    # Production source_files_old contains every loaded file, including headers;
    # Kbuild resolution.source_files contains only its owning translation units.
    if not inventories["headers_old"] <= inventories["source_files_old"]:
        raise EvidenceBoundaryError("old headers must belong to the loaded source inventory")
    paths = sorted(inventories["source_files_old"])
    if not paths or any(not path.startswith(module_path + "/") for path in paths):
        raise EvidenceBoundaryError("old source paths leave target module boundary")
    resolution = module.get("kbuild_target_resolution", {})
    if module.get("case_scope") != "kbuild_target" or not resolution:
        raise EvidenceBoundaryError("pool requires production Kbuild target ownership")
    if resolution:
        _mapping(resolution, {"module_path", "kbuild_target", "normalized_target", "makefile_path", "object_files", "source_files", "headers", "files", "warnings"}, "Kbuild resolution")
        if resolution.get("module_path") != module_path:
            raise EvidenceBoundaryError("Kbuild resolution target differs from module source")
        if resolution.get("kbuild_target") != module.get("kbuild_target"):
            raise EvidenceBoundaryError("Kbuild target differs from module source")
        expected_inventories = {
            "source_files": set(paths) - inventories["headers_old"],
            "headers": inventories["headers_old"], "files": set(paths),
        }
        for key, expected in expected_inventories.items():
            entries = [_path(path) for path in _list(resolution.get(key, []), "Kbuild " + key)]
            if len(entries) != len(set(entries)) or set(entries) != expected:
                raise EvidenceBoundaryError("Kbuild resolution and old source inventory differ")
        if resolution.get("warnings"):
            raise EvidenceBoundaryError("unresolved Kbuild ownership is unsupported")
    if module.get("local_include_scope_risks"):
        raise EvidenceBoundaryError("unresolved local include scope is unsupported")
    if old_sources is None and extracted_dir is not None:
        source_root = Path(extracted_dir) / "old_module_sources"
        old_sources = {}
        for path in paths:
            location = source_root / path
            if location.resolve() != source_root.resolve() / path:
                raise EvidenceBoundaryError("old source symlink leaves extraction boundary")
            old_sources[path] = location.read_text(encoding="utf-8")
    if old_sources is None or set(old_sources) != set(paths) or any(not isinstance(raw, str) for raw in old_sources.values()):
        raise EvidenceBoundaryError("old sources must exactly match the resolved target paths")
    if source_reader:
        for path, raw in old_sources.items():
            if source_reader(refs["old"], path) != raw:
                raise EvidenceBoundaryError(f"old target Git source mismatch: {path}")

    locations_doc = _mapping(documents["kernel_definition_locations.json"], {"items"}, "definition locations")
    diffs_doc = _mapping(documents["kernel_definition_diffs.json"], _DIFF_KEYS, "definition diffs")
    if diffs_doc.get("affected_func_signal_mode") != "kernel_only" or diffs_doc.get("collect_commit_info") is not False:
        raise EvidenceBoundaryError("requires kernel-only extraction with commit information disabled")
    if diffs_doc.get("semantic_obligation_table"):
        raise EvidenceBoundaryError("semantic-obligation tables are forbidden")
    helper = diffs_doc.get("kernel_side_helper_discovery", {})
    if helper:
        _mapping(helper, {"enabled", "diff_paths", "candidate_count", "policy"}, "helper discovery")
        if helper.get("policy") != "old_driver_usage_plus_kernel_side_diffs_only":
            raise EvidenceBoundaryError("unsupported helper discovery policy")
        for path in _list(helper.get("diff_paths", []), "helper paths"):
            _path(path, kernel=True)
    locations = {}
    for raw in _list(locations_doc.get("items"), "location items"):
        item = _mapping(raw, _LOCATION_ITEM_KEYS, "location item")
        key = (_symbol(item.get("symbol"), item.get("kind")), item["kind"])
        value = {
            "uses": _uses(item.get("used_by_driver", []), old_sources),
            "old": _locations(item.get("old_locations", [])), "new": _locations(item.get("new_locations", [])),
        }
        if key in locations and _canonical(locations[key]) != _canonical(value):
            raise EvidenceBoundaryError("conflicting duplicate location row")
        locations[key] = value

    groups, signals, seen, skipped = {}, {}, {}, set()
    discarded_helper_count = 0
    items = _list(diffs_doc.get("items"), "definition diff items")
    for raw in items:
        item = _mapping(raw, _DIFF_ITEM_KEYS, "definition diff item")
        symbol, kind = _symbol(item.get("symbol"), item.get("kind")), item["kind"]
        key = (symbol, kind)
        if key not in locations:
            raise EvidenceBoundaryError("definition lacks locator provenance")
        _validate_discarded_annotations(item)
        uses = _uses(item.get("used_by_driver", []), old_sources)
        if uses != locations[key]["uses"]:
            raise EvidenceBoundaryError("definition and locator disagree about old target uses")
        if not uses:
            raise EvidenceBoundaryError("kernel evidence lacks old target usage")
        # Bind symbol usage independently of scanner-provided snippets.
        if not any(re.search(r"(?<![A-Za-z_0-9])" + re.escape(symbol) + r"(?![A-Za-z_0-9])", old_sources[use["path"]]) for use in uses):
            raise EvidenceBoundaryError("symbol does not occur in old target source")
        old, new = [
            _excerpt_binding(item.get(side + "_definition", ""), locations[key][side], ref=refs[side], source_reader=source_reader)
            for side in ("old", "new")
        ]
        production_diff = _focused_diff(old, new, f"old:{symbol}", f"new:{symbol}")
        if item.get("definition_diff", "") != production_diff:
            raise EvidenceBoundaryError("definition diff differs from its bound source excerpts")
        changed = item.get("definition_changed")
        if not isinstance(changed, bool) or (changed and not production_diff):
            raise EvidenceBoundaryError("unsupported definition-change signal")
        record = {"old": old, "new": new, "uses": uses, "changed": changed}
        if key in seen and seen[key] != record:
            raise EvidenceBoundaryError("conflicting duplicate definition row")
        if key in seen:
            continue
        seen[key] = record
        discarded_helper_count += bool(item.get("replacement_candidates"))
        if not old and not new:
            skipped.add(f"{symbol} ({kind})")
            continue
        if changed:
            signal = signals.setdefault(symbol, {"symbol": symbol, "old_target_uses": {}})
            for use in uses:
                signal["old_target_uses"][_canonical(use)] = use
        # Different scanner kinds for the same excerpt are one candidate.
        identity = {"old": old, "new": new}
        group = groups.setdefault(_canonical(identity), {**identity, "symbols": set(), "kinds": set()})
        group["symbols"].add(symbol)
        group["kinds"].add(kind)

    if set(seen) != set(locations):
        raise EvidenceBoundaryError("locator and definition symbol inventories differ")
    obligation_signal = [
        {"symbol": symbol, "old_target_uses": [signals[symbol]["old_target_uses"][key] for key in sorted(signals[symbol]["old_target_uses"])]}
        for symbol in sorted(signals)
    ]
    candidates = []
    for group in groups.values():
        old, new = group["old"], group["new"]
        symbols, kinds = sorted(group["symbols"]), sorted(group["kinds"])
        old_label = f"old:{old['path']}" if old else "old:/dev/null"
        new_label = f"new:{new['path']}" if new else "new:/dev/null"
        diff = _focused_diff(old, new, old_label, new_label)
        identity = {"schema": _SCHEMA, "symbols": symbols, "old": old, "new": new, "diff": diff}
        content = _render(symbols, old, new, diff)
        candidates.append({
            "id": "kernel-" + _sha(_canonical(identity)), "symbols": symbols, "kinds": kinds,
            "text": content, "content_sha256": _sha(content), "chars": len(content),
            "provenance": {"old": old, "new": new, "focused_diff": diff},
        })
    candidates.sort(key=lambda row: (
        tuple((row["provenance"][side] or {}).get("path", "") for side in ("old", "new")),
        tuple((row["provenance"][side] or {}).get("start_line", 0) for side in ("old", "new")),
        row["symbols"], row["id"],
    ))
    audit = {
        "schema": _SCHEMA, "module_path": module_path, "kbuild_target": module["kbuild_target"],
        "old_ref": refs["old"], "new_ref": refs["new"],
        "source_verification": "git_blob_lines_verified" if source_reader else "extraction_bound_not_git_verified",
        "input_document_sha256": {name: _sha(_canonical(documents[name])) for name in DOCUMENT_NAMES},
        "old_target_source_sha256": {path: _sha(old_sources[path]) for path in paths},
        "unique_symbol_kind_rows": len(seen), "candidate_count": len(candidates),
        "rows_without_located_definition": sorted(skipped),
        "deduplicated_excerpt_rows": len(seen) - len(skipped) - len(candidates),
        "discarded_helper_annotation_rows": discarded_helper_count,
        "candidate_order": [row["id"] for row in candidates],
        "obligation_symbols": sorted(signals),
        "obligation_signal_sha256": _sha(_canonical(obligation_signal)),
        "candidate_symbol_overlap": {row["id"]: sorted(set(row["symbols"]) & set(signals)) for row in candidates},
        "pool_sha256": _sha(_canonical(candidates)),
        "block_policy": "whole production excerpt pair; identical old/new excerpts rendered once; source-line diff recomputed",
        "guidance_policy": "only changed-symbol identifiers and old target locations; no obligation prose, replacements, references or new-driver data",
        "eligibility_note": "locator excerpts may be incomplete or approximate definitions; no semantic quality claim",
    }
    return {"candidates": candidates, "obligation_signal": obligation_signal, "audit": audit}


def git_source_reader(repo: str | Path) -> Callable[[str, str], str]:
    """Return a cached, read-only blob reader suitable for the pool builder."""
    repository = str(Path(repo).resolve())

    @lru_cache(maxsize=None)
    def read(ref: str, path: str) -> str:
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise EvidenceBoundaryError("Git reader requires a full commit ID")
        _path(path)
        result = subprocess.run(
            ["git", "-C", repository, "show", f"{ref}:{path}"],
            capture_output=True, check=False, timeout=60,
        )
        if result.returncode:
            raise EvidenceBoundaryError(f"could not read Git blob: {ref}:{path}")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceBoundaryError("Git source is not UTF-8") from exc

    return read


def verify_pool_against_git(pool: dict, repo: str | Path) -> dict:
    """Independently validate pool identities and all source bytes against Git.

    This does not re-run the heuristic locator or prove definition relevance.
    No new target or sibling-driver blob is read. The only driver blobs read
    are old-ref paths already bound in the old-target source hash inventory.
    """
    read = git_source_reader(repo)
    audit = pool["audit"]
    refs = {side: audit[side + "_ref"] for side in ("old", "new")}
    sources = audit["old_target_source_sha256"]
    for path, expected in sources.items():
        _path(path)
        if _sha(read(refs["old"], path)) != expected:
            raise EvidenceBoundaryError(f"old target hash differs from Git: {path}")
    if _sha(_canonical(pool["obligation_signal"])) != audit["obligation_signal_sha256"]:
        raise EvidenceBoundaryError("obligation signal digest mismatch")
    candidates = pool["candidates"]
    if _sha(_canonical(candidates)) != audit["pool_sha256"]:
        raise EvidenceBoundaryError("pool content digest mismatch")
    ids = [row["id"] for row in candidates]
    if ids != audit["candidate_order"] or len(set(ids)) != len(ids):
        raise EvidenceBoundaryError("candidate identity/order mismatch")
    checked_lines, checked_excerpts, blobs = 0, 0, set()
    for row in candidates:
        old, new = [row["provenance"][side] for side in ("old", "new")]
        for side, excerpt in (("old", old), ("new", new)):
            if excerpt is None:
                continue
            path = _path(excerpt["path"], kernel=True)
            if excerpt["ref"] != refs[side]:
                raise EvidenceBoundaryError("excerpt ref differs from pool ref")
            start, end = _line(excerpt["start_line"]), _line(excerpt["end_line"])
            raw = read(refs[side], path)
            source_lines = raw.splitlines()
            if end < start or end > len(source_lines):
                raise EvidenceBoundaryError("excerpt range outside Git source")
            expected = "\n".join(source_lines[start - 1:end])
            if expected != excerpt["content"] or _sha(expected) != excerpt["content_sha256"]:
                raise EvidenceBoundaryError("excerpt bytes differ from Git")
            checked_lines += end - start + 1
            checked_excerpts += 1
            blobs.add((refs[side], path))
        diff = _focused_diff(old, new, f"old:{old['path']}" if old else "old:/dev/null", f"new:{new['path']}" if new else "new:/dev/null")
        if row["provenance"]["focused_diff"] != diff:
            raise EvidenceBoundaryError("focused diff mismatch")
        identity = {"schema": _SCHEMA, "symbols": row["symbols"], "old": old, "new": new, "diff": diff}
        if row["id"] != "kernel-" + _sha(_canonical(identity)):
            raise EvidenceBoundaryError("candidate ID mismatch")
        rendered = _render(row["symbols"], old, new, diff)
        if row["text"] != rendered or row["chars"] != len(rendered) or row["content_sha256"] != _sha(rendered):
            raise EvidenceBoundaryError("candidate rendering mismatch")
    return {
        "status": "PASS", "pool_sha256": audit["pool_sha256"],
        "candidate_count": len(candidates), "checked_kernel_excerpts": checked_excerpts,
        "checked_kernel_source_lines": checked_lines, "checked_kernel_blobs": len(blobs),
        "checked_old_target_blobs": len(sources), "new_ref_driver_blobs_read": 0,
        "kernel_evidence_driver_blobs_read": 0,
        "scope": "source provenance and content integrity; no semantic relevance claim",
    }
