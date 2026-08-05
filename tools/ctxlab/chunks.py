"""Candidate context chunks for the method zoo.

A case's selectable context is its kernel-evidence items (gold + distractors),
one Chunk each. The edit-locus slices (the old-source functions the reference
edit touches) are separated out and always kept verbatim by every method —
they are the one thing the deck says never to drop.

Token cost is the deterministic identifier proxy from ctxlab.metrics
(token_count); budgets are expressed in those units so the offline mock loop
needs no real tokenizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dataset import evidence_text
from .metrics import token_count
from .textutil import identifiers


@dataclass(eq=False)
class Chunk:
    id: str
    text: str
    kind: str  # "evidence" | "edit_locus"
    symbols: frozenset  # kernel symbols this chunk carries
    is_gold: bool = False
    tokens: int = field(default=0)

    def __post_init__(self):
        if not self.tokens:
            self.tokens = token_count(self.text)


def edit_locus_slices(case: dict[str, Any]) -> list[Chunk]:
    """The old-source functions the reference edit touches, kept verbatim.

    Falls back to the whole (budgeted) old source when function boundaries
    cannot be recovered — the locus must never be empty.
    """
    from driver_agent_pregen.symbol_scanner import _extract_c_like_functions
    from .diffs import parse_diff_changed_lines

    meta = case.get("meta", {})
    module_file = meta.get("module_file") or meta.get("module_path", "")
    changed = parse_diff_changed_lines(case["reference_diff"])
    old_paths = {p for p in changed if changed[p]["old"]}
    old_lines = set()
    for rec in changed.values():
        old_lines |= rec["old"]

    slices: list[Chunk] = []
    src = case["old_source"]
    funcs = _extract_c_like_functions(src, module_file or "x.c")
    src_lines = src.splitlines()
    for f in funcs:
        if any(f.start_line <= ln <= f.end_line for ln in old_lines):
            body = "\n".join(src_lines[f.start_line - 1:f.end_line])
            slices.append(Chunk(id=f"locus:{f.name}", text=body, kind="edit_locus",
                                symbols=frozenset(identifiers(body))))
    if not slices:
        slices.append(Chunk(id="locus:whole", text=src[:4000], kind="edit_locus",
                            symbols=frozenset(identifiers(src[:4000]))))
    return slices


def evidence_chunks(case: dict[str, Any]) -> list[Chunk]:
    """One selectable chunk per kernel-evidence item (gold + distractors)."""
    chunks: list[Chunk] = []
    for item in case.get("gold_items", []):
        text = evidence_text(item)
        chunks.append(Chunk(id=f"gold:{item.get('symbol')}", text=text, kind="evidence",
                            symbols=frozenset([item.get("symbol", "")]) | identifiers(text),
                            is_gold=True))
    for item in case.get("distractor_items", []):
        text = evidence_text(item)
        chunks.append(Chunk(id=f"dist:{item.get('symbol')}", text=text, kind="evidence",
                            symbols=frozenset([item.get("symbol", "")]) | identifiers(text),
                            is_gold=False))
    return chunks


def obligation_symbols(case: dict[str, Any]) -> frozenset:
    """Symbols the migration is obliged to handle: the case's gold labels.

    In a real run these come from the deterministic obligation builder; for
    mined cases the gold kernel_symbols are exactly that machine-derived set.
    Evaluation-only (derived from labels), never fed to the agent.
    """
    return frozenset(case.get("meta", {}).get("kernel_symbols", []))
