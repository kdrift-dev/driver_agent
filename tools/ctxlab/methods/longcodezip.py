"""LongCodeZip (Shi et al 2025): conditional-PPL chunk ranking + knapsack.

Rank each candidate chunk by how much it lowers the conditional perplexity of
the query (here the reference edit) when it is the sole context, then fill the
budget by value-density knapsack. Training-free: the only model is the scoring
backend (mock offline, HF for the real run).
"""

from __future__ import annotations

from .base import Method, Selection
from ..chunks import edit_locus_slices, evidence_chunks
from ..metrics import build_prompt


class LongCodeZip(Method):
    name = "longcodezip"

    def __init__(self, backend=None):
        self._backend = backend

    def _mk_backend(self):
        if self._backend is None:
            from ..backends import MockBackend
            self._backend = MockBackend()
        return self._backend

    def _value(self, backend, case, base, text):
        prompt = build_prompt(text, case["old_source"][:12000])
        solo = backend.score(prompt, case["reference_diff"])["nll_sum"]
        return base - solo  # perplexity reduction attributable to the chunk

    def select(self, case, budget: int) -> Selection:
        backend = self._mk_backend()
        base = backend.score(build_prompt("", case["old_source"][:12000]),
                             case["reference_diff"])["nll_sum"]
        locus = edit_locus_slices(case)
        remaining = max(0, budget - sum(c.tokens for c in locus))

        scored = []
        for c in evidence_chunks(case):
            value = self._value(backend, case, base, c.text)
            density = value / c.tokens if c.tokens else 0.0
            scored.append((density, value, c))
        scored.sort(key=lambda t: t[0], reverse=True)

        picked, used = [], 0
        for density, value, c in scored:
            if value <= 0:
                continue  # a chunk that does not help is not worth budget
            if used + c.tokens <= remaining:
                picked.append(c)
                used += c.tokens
        return Selection(method=self.name, locus=locus, evidence=picked, budget=budget,
                         meta={"locus_cost": budget - remaining, "evidence_cost": used,
                               "base_nll": round(base, 4)})
