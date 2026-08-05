"""Common interface for the method zoo.

`Method.select(case, budget)` returns a `Selection`: the always-kept edit
locus plus the ranked evidence chunks chosen to fit `budget` (in identifier
tokens). Methods differ only in how they rank/pick evidence chunks; the edit
locus is never dropped and its cost is charged first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..chunks import Chunk, edit_locus_slices, evidence_chunks


@dataclass
class Selection:
    method: str
    locus: list[Chunk]
    evidence: list[Chunk]
    budget: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chunks(self) -> list[Chunk]:
        return self.locus + self.evidence

    @property
    def tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)

    def context_text(self) -> str:
        """Evidence-only context (the locus is passed as old source, not context)."""
        return "\n\n".join(c.text for c in self.evidence)


class Method:
    name = "base"

    def rank_evidence(self, case: dict[str, Any], chunks: list[Chunk]) -> list[Chunk]:
        """Return evidence chunks in preference order (most valuable first)."""
        raise NotImplementedError

    def select(self, case: dict[str, Any], budget: int) -> Selection:
        locus = edit_locus_slices(case)
        locus_cost = sum(c.tokens for c in locus)
        remaining = max(0, budget - locus_cost)
        ranked = self.rank_evidence(case, evidence_chunks(case))
        picked: list[Chunk] = []
        used = 0
        for chunk in ranked:
            if used + chunk.tokens <= remaining:
                picked.append(chunk)
                used += chunk.tokens
        return Selection(method=self.name, locus=locus, evidence=picked, budget=budget,
                         meta={"locus_cost": locus_cost, "evidence_cost": used,
                               "n_candidates": len(ranked)})
