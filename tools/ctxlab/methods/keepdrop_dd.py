"""Reference-edit keep/drop NLL oracle (evaluation-only, no build oracle)."""

from __future__ import annotations

from .base import Method


class KeepDropDD(Method):
    name = "keepdrop_dd"
    uses_reference = True

    def __init__(self, backend=None, tol: float = 1e-6, **kwargs):
        super().__init__(backend=backend, **kwargs)
        self.tol = tol

    def rank_evidence(self, case, chunks):
        return list(chunks)

    def pick_evidence(self, case, candidates):
        # Start with an explicitly budgeted neutral-order set: never score a
        # larger unreported prompt, which could be silently window-truncated.
        kept = super().pick_evidence(case, candidates)
        baseline = self.nll(case, kept)
        changed = True
        while changed and kept:
            changed = False
            for c in list(kept):
                trial = [x for x in kept if x is not c]
                if self.nll(case, trial) <= baseline + self.tol:
                    kept = trial
                    changed = True
                    break
        return kept
