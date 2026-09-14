"""Reference-edit conditional-NLL density oracle (evaluation-only adaptation)."""

from __future__ import annotations

from .base import Method


class LongCodeZip(Method):
    name = "longcodezip"
    uses_reference = True

    def rank_evidence(self, case, chunks):
        base = self.nll(case, [])
        scored = []
        for c in chunks:
            if not self.fits([c]):
                continue
            value = base - self.nll(case, [c])
            if value > 0:
                scored.append((value / max(c.tokens, 1), c))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [c for _, c in scored]
