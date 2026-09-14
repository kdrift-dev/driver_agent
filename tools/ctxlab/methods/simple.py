"""Label-blind baselines and an explicit reference-edit Shapley-style oracle."""

from __future__ import annotations

import hashlib
import random

from ..textutil import identifiers
from .base import Method


class TruncBaseline(Method):
    name = "trunc_baseline"

    def rank_evidence(self, case, chunks):
        return list(chunks)


class RandomBudget(Method):
    name = "random_budget"

    def rank_evidence(self, case, chunks):
        # Python's hash() is salted per process; SHA-256 is stable across hosts.
        key = f"{self.seed}:{case['case_id']}".encode()
        rng = random.Random(int.from_bytes(hashlib.sha256(key).digest(), "big"))
        shuffled = list(chunks)
        rng.shuffle(shuffled)
        return shuffled


class ErrPrune(Method):
    name = "errprune"

    def rank_evidence(self, case, chunks):
        locus_syms = identifiers(self.source)
        return sorted(chunks, key=lambda c: len(c.symbols & locus_syms), reverse=True)


class StaticEpam(Method):
    name = "static_epam"

    def rank_evidence(self, case, chunks):
        src_syms = identifiers(case["old_source"])
        return sorted(chunks, key=lambda c: len(c.symbols & src_syms), reverse=True)


class ShapleyProbe(Method):
    name = "shapley_probe"
    uses_reference = True

    def rank_evidence(self, case, chunks):
        base = self.nll(case, [])
        # Probes are themselves within the same prompt budget.
        feasible = [c for c in chunks if self.fits([c])]
        scored = [(base - self.nll(case, [c]), c) for c in feasible]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [c for _, c in scored]
