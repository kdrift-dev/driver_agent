"""Obligation-overlap selector; shares coefficients with the pipeline pruner.

Default signal is a labeled lexical approximation from old uses and kernel
changes. oracle_obligations is a separate evaluation-only gold-label variant.
Neither variant turns the mined candidate pool into native pipeline retrieval.
"""

from __future__ import annotations

from .base import Method
from ..textutil import identifiers
from driver_agent_pregen.context_pruner import load_coeffs, marginal_gain


class ObligationAnchored(Method):
    name = "ours"

    def __init__(self, coeffs=None, **kwargs):
        super().__init__(**kwargs)
        self.coeffs = coeffs or load_coeffs()

    def pick_evidence(self, case, candidates):
        remaining = set(self.obligations)
        locus_syms = identifiers(self.source)
        picked = []
        candidates = list(candidates)
        while candidates:
            best, best_gain = None, None
            for c in candidates:
                if not self.fits(picked + [c]):
                    continue
                gain = marginal_gain(c.symbols, remaining, locus_syms, c.tokens, self.coeffs)
                if best_gain is None or gain > best_gain:
                    best, best_gain = c, gain
            if best is None or best_gain <= 0:
                break
            picked.append(best)
            remaining -= set(best.symbols)
            candidates.remove(best)
        return picked


class OracleObligations(ObligationAnchored):
    name = "oracle_obligations"
    oracle_obligations = True
