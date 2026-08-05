"""Keep/drop via delta debugging (OCD / SWEzze style).

Find a minimal sufficient context: start from all evidence chunks, greedily
drop any chunk whose removal does not hurt the oracle proxy, then enforce the
budget. The real oracle is an incremental kernel build; offline the proxy is
the scoring backend's edit-NLL (a chunk is droppable if removing it does not
raise NLL beyond a tolerance). Distilling this into a cheap classifier is
future work; here it is the reference minimal-context method.
"""

from __future__ import annotations

from .base import Method, Selection
from ..chunks import edit_locus_slices, evidence_chunks
from ..metrics import build_prompt


class KeepDropDD(Method):
    name = "keepdrop_dd"

    def __init__(self, backend=None, tol: float = 1e-6):
        self._backend = backend
        self.tol = tol

    def _mk_backend(self):
        if self._backend is None:
            from ..backends import MockBackend
            self._backend = MockBackend()
        return self._backend

    def _nll(self, backend, case, kept):
        text = "\n\n".join(c.text for c in kept)
        return backend.score(build_prompt(text, case["old_source"][:12000]),
                             case["reference_diff"])["nll_sum"]

    def select(self, case, budget: int) -> Selection:
        backend = self._mk_backend()
        kept = list(evidence_chunks(case))
        full_nll = self._nll(backend, case, kept)

        # greedy delta-debug: drop a chunk if the full-set NLL is unchanged
        # without it (i.e. it contributes no oracle value)
        changed = True
        while changed and kept:
            changed = False
            for c in list(kept):
                trial = [x for x in kept if x is not c]
                if self._nll(backend, case, trial) <= full_nll + self.tol:
                    kept = trial
                    changed = True
                    break

        locus = edit_locus_slices(case)
        remaining = max(0, budget - sum(x.tokens for x in locus))
        # keep the survivors that fit; smallest first maximizes retained count
        kept.sort(key=lambda c: c.tokens)
        picked, used = [], 0
        for c in kept:
            if used + c.tokens <= remaining:
                picked.append(c)
                used += c.tokens
        return Selection(method=self.name, locus=locus, evidence=picked, budget=budget,
                         meta={"minimal_set": len(kept), "evidence_cost": used,
                               "full_nll": round(full_nll, 4)})
