"""Ours: obligation-anchored dependency-closure selection.

Selection unit is the edit locus (a function plus what it needs to stay
expressible) kept verbatim, plus evidence chunks chosen by submodular greedy
coverage of the obligation symbols — the machine-derived set the migration
must handle (for mined cases, the gold kernel_symbols). Value blends new
obligation-symbol coverage, edit-locus symbol coverage and a size penalty with
coefficients loaded from an auditable JSON artifact
(driver_agent_pregen/context_pruner_coeffs.json). No trained compressor; the
judge downstream is the build oracle + gate.

The scoring formula (marginal_gain) and coefficients are shared with the
pipeline-side pruner (driver_agent_pregen.context_pruner) so the paper-facing
selector and the default-OFF pipeline hook rank identically.
"""

from __future__ import annotations

from .base import Method, Selection
from ..chunks import edit_locus_slices, evidence_chunks, obligation_symbols
from driver_agent_pregen.context_pruner import load_coeffs, marginal_gain


class ObligationAnchored(Method):
    name = "ours"

    def __init__(self, coeffs=None):
        self.coeffs = coeffs or load_coeffs()

    def _gain(self, chunk, obl_remaining, locus_syms):
        return marginal_gain(chunk.symbols, obl_remaining, locus_syms,
                             chunk.tokens, self.coeffs)

    def select(self, case, budget: int) -> Selection:
        locus = edit_locus_slices(case)
        locus_syms: set[str] = set()
        for c in locus:
            locus_syms |= set(c.symbols)
        remaining_budget = max(0, budget - sum(c.tokens for c in locus))

        candidates = list(evidence_chunks(case))
        obl_remaining = set(obligation_symbols(case))
        picked, used = [], 0
        # submodular greedy: each step take the chunk with the best marginal
        # gain that still fits, then discount the obligations it covered
        while candidates:
            best, best_gain = None, None
            for c in candidates:
                if used + c.tokens > remaining_budget:
                    continue
                g = self._gain(c, obl_remaining, locus_syms)
                if best_gain is None or g > best_gain:
                    best, best_gain = c, g
            if best is None or best_gain is None or best_gain <= 0:
                break
            picked.append(best)
            used += best.tokens
            obl_remaining -= set(best.symbols)
            candidates.remove(best)
        return Selection(method=self.name, locus=locus, evidence=picked, budget=budget,
                         meta={"evidence_cost": used,
                               "obligations_total": len(obligation_symbols(case)),
                               "obligations_uncovered": len(obl_remaining),
                               "coeffs_version": self.coeffs.get("version")})
