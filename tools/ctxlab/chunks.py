"""Label-blind candidates and explicit source/obligation provenance.

The case-pack candidate pool is mined with evaluation labels; removing their
ordering/IDs does not make the pool a native pipeline retrieval experiment.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .dataset import evidence_text
from .metrics import token_count
from .textutil import identifiers, strip_comments_and_strings


@dataclass(eq=False)
class Chunk:
    id: str
    text: str
    kind: str
    symbols: frozenset
    is_gold: bool = False  # populated only in evaluation, never in selectors
    tokens: int = field(default=0)

    def __post_init__(self):
        if not self.tokens:
            self.tokens = token_count(self.text)


def edit_locus_slices(case: dict[str, Any], source="old_source", backend=None) -> list[Chunk]:
    """Keep full old source by default. Reference-localized slicing is oracle-only."""
    src = case["old_source"]
    if source == "old_source":
        return [Chunk("locus:whole", src, "edit_locus", frozenset(identifiers(src)),
                      tokens=token_count(src, backend))]
    if source != "oracle_reference":
        raise ValueError(f"unknown locus source: {source}")
    from driver_agent_pregen.symbol_scanner import _extract_c_like_functions
    from .diffs import parse_diff_changed_lines

    # Diff line numbers are file-local; never apply their union to a
    # concatenated module. Legacy in-memory cases without a source map use an
    # explicitly reported whole-source fallback instead of guessed offsets.
    sources = case.get("old_sources_by_path")
    if not sources:
        return edit_locus_slices(case, backend=backend)
    changed = parse_diff_changed_lines(case["reference_diff"])
    if not changed or any(path not in sources for path in changed):
        return edit_locus_slices(case, backend=backend)
    slices = []
    for path, rec in changed.items():
        text = sources[path]
        old_lines = rec["old"]
        funcs = _extract_c_like_functions(text, path)
        src_lines = text.splitlines()
        touched = [f for f in funcs if any(f.start_line <= ln <= f.end_line for ln in old_lines)]
        covered = {ln for f in touched for ln in old_lines if f.start_line <= ln <= f.end_line}
        if not old_lines or covered != old_lines:
            # Added-only hunks or edits outside recovered functions need the
            # entire file; dropping these edits would make the locus incomplete.
            slices.append(Chunk(f"locus:file:{path}", text, "edit_locus",
                                frozenset(identifiers(text)), tokens=token_count(text, backend)))
            continue
        for f in touched:
            body = "\n".join(src_lines[f.start_line - 1:f.end_line])
            slices.append(Chunk(f"locus:{path}:{f.name}", body, "edit_locus",
                                frozenset(identifiers(body)), tokens=token_count(body, backend)))
    return slices or edit_locus_slices(case, backend=backend)


def evidence_chunks(case: dict[str, Any], backend=None, include_labels=False) -> list[Chunk]:
    """Deduplicated content-hash order, independent of label partition/order."""
    if "candidate_items" in case:
        groups = [(case["candidate_items"], False)]
    else:
        groups = [(case.get("gold_items", []), True), (case.get("distractor_items", []), False)]
    chunks = {}
    for items, is_gold in groups:
        for item in items:
            text = evidence_text(item)
            ident = "evidence:" + hashlib.sha256(text.encode()).hexdigest()
            labeled_gold = bool(include_labels and is_gold)
            if ident in chunks:
                chunks[ident].is_gold |= labeled_gold
                continue
            chunks[ident] = Chunk(ident, text, "evidence",
                                  frozenset([item.get("symbol", "")]) | identifiers(text),
                                  is_gold=labeled_gold, tokens=token_count(text, backend))
    return [chunks[key] for key in sorted(chunks)]


def obligation_symbols(case: dict[str, Any]) -> frozenset:
    """Reference-derived gold obligations for evaluation or explicit oracle arm."""
    return frozenset(case.get("meta", {}).get("kernel_symbols", []))


def allowed_obligation_symbols(case: dict[str, Any]) -> frozenset:
    """Conservative lexical approximation: old uses changed in kernel evidence.

    This is not the pipeline obligation builder and does not infer new symbols
    from the target reference edit. Only removed/added kernel diff lines or
    old/new definition differences contribute a signal.
    """
    used = identifiers(strip_comments_and_strings(case["old_source"]))
    touched = set()
    items = case.get("candidate_items", case.get("gold_items", []) + case.get("distractor_items", []))
    for item in items:
        diff = item.get("definition_diff") or ""
        lines = [ln[1:] for ln in diff.splitlines()
                 if ln[:1] in ("+", "-") and not ln.startswith(("+++", "---"))]
        if lines:
            touched |= identifiers(strip_comments_and_strings("\n".join(lines)))
        else:
            old = item.get("old_definition") or item.get("old_definition_excerpt") or ""
            new = item.get("new_definition") or item.get("new_definition_excerpt") or ""
            if old and new and old != new:
                touched |= identifiers(strip_comments_and_strings(old))
    return frozenset(touched & used)


def selector_case(case: dict[str, Any], *, reference=False, oracle_obligations=False) -> dict[str, Any]:
    """Allowlist selector inputs; no gold flag, gold ID, subject or manual notes."""
    items = case.get("candidate_items", case.get("gold_items", []) + case.get("distractor_items", []))
    allowed_fields = ("symbol", "definition_diff", "old_definition", "new_definition",
                      "old_definition_excerpt", "new_definition_excerpt", "old_locations", "new_locations")
    clean_items = [{k: item[k] for k in allowed_fields if k in item} for item in items]
    clean_items.sort(key=lambda it: hashlib.sha256(evidence_text(it).encode()).hexdigest())
    clean = {"case_id": case["case_id"], "old_source": case["old_source"],
             "candidate_items": clean_items,
             "meta": {"module_file": case.get("meta", {}).get("module_file", "x.c")}}
    if reference:
        clean["reference_diff"] = case["reference_diff"]
    if oracle_obligations:
        clean["meta"]["kernel_symbols"] = sorted(obligation_symbols(case))
    return clean
