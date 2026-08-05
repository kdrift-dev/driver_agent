"""Baselines and reference-point methods with no oracle/backend loop.

- trunc_baseline: blind order-preserving clip (reproduces today's behavior).
- random_budget: deterministic random pick under budget (noise floor).
- errprune: keep evidence whose symbols surface in the edit locus, i.e. the
  identifiers a compiler diagnostic would name (SWE-Pruner motive, no trained
  skimmer; deterministic).
- static_epam: mild static prune by identifier overlap with the old source
  (EPAM: identifiers carry migration semantics).
- shapley_probe: per-chunk teacher-forced marginal value (RepoShapley-style).
"""

from __future__ import annotations

import random
from typing import Any

from ..chunks import Chunk
from ..metrics import build_prompt, score_context
from .base import Method


class TruncBaseline(Method):
    name = "trunc_baseline"

    def rank_evidence(self, case, chunks):
        # blind: original evidence order, no value signal
        return list(chunks)


class RandomBudget(Method):
    name = "random_budget"

    def rank_evidence(self, case, chunks):
        seed = case.get("meta", {}).get("commit", case["case_id"])
        rng = random.Random(hash(seed) & 0xFFFFFFFF)
        shuffled = list(chunks)
        rng.shuffle(shuffled)
        return shuffled


class ErrPrune(Method):
    name = "errprune"

    def rank_evidence(self, case, chunks):
        # identifiers a diagnostic would name = those present in the edit locus
        from ..chunks import edit_locus_slices
        locus_syms: set[str] = set()
        for c in edit_locus_slices(case):
            locus_syms |= set(c.symbols)
        keep = [c for c in chunks if c.symbols & locus_syms]
        drop = [c for c in chunks if not (c.symbols & locus_syms)]
        keep.sort(key=lambda c: len(c.symbols & locus_syms), reverse=True)
        return keep + drop


class StaticEpam(Method):
    name = "static_epam"

    def rank_evidence(self, case, chunks):
        src_syms = set(_source_identifiers(case))
        return sorted(chunks, key=lambda c: len(c.symbols & src_syms), reverse=True)


class ShapleyProbe(Method):
    name = "shapley_probe"

    def __init__(self, backend=None):
        self._backend = backend

    def _mk_backend(self):
        if self._backend is None:
            from ..backends import MockBackend
            self._backend = MockBackend()
        return self._backend

    def rank_evidence(self, case, chunks):
        backend = self._mk_backend()
        base = score_context("", case, backend)["ppl"]["nll_sum"]
        scored = []
        for c in chunks:
            solo = _nll_with(backend, case, c.text)
            scored.append((base - solo, c))  # larger drop = more valuable
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [c for _, c in scored]


def _nll_with(backend, case, context_text) -> float:
    prompt = build_prompt(context_text, case["old_source"][:12000])
    return backend.score(prompt, case["reference_diff"])["nll_sum"]


def _source_identifiers(case: dict[str, Any]) -> set:
    from ..textutil import identifiers
    return identifiers(case["old_source"])
