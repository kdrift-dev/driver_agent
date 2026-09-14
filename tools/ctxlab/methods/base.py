"""Common selection contract: preserve source and pack the exact rendered prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..backends import ContextWindowExceeded, ProviderFailure
from ..chunks import (Chunk, allowed_obligation_symbols, edit_locus_slices,
                      evidence_chunks, obligation_symbols, selector_case)
from ..metrics import build_prompt, render_context, strict_score, token_count, token_count_kind
from ..textutil import identifiers


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
        return self.meta["prompt_tokens"]

    def context_text(self) -> str:
        return render_context(self.evidence, self.meta.get("arm", "full"), self.meta.get("guidance", ""))

    def old_source_text(self) -> str:
        return "\n\n".join(c.text for c in self.locus)

    def prompt_text(self) -> str:
        return build_prompt(self.context_text(), self.old_source_text())


class Method:
    name = "base"
    uses_reference = False
    oracle_obligations = False

    def __init__(self, backend=None, seed=0, locus_source="old_source", cache=None):
        if self.uses_reference and backend is None:
            raise ValueError(f"{self.name} requires an explicit selection backend (oracle evaluation-only)")
        self.backend = backend
        self.seed = seed
        self.locus_source = locus_source
        self.cache = cache

    def rank_evidence(self, case: dict[str, Any], chunks: list[Chunk]) -> list[Chunk]:
        raise NotImplementedError

    def context(self, chunks):
        return render_context(chunks, self.arm, self.guidance)

    def prompt(self, chunks):
        return build_prompt(self.context(chunks), self.source)

    def fits(self, chunks):
        return token_count(self.prompt(chunks), self.backend) <= self.budget

    def nll(self, case, chunks):
        return strict_score(self.backend, self.prompt(chunks), case["reference_diff"], self.cache)["nll_sum"]

    def pick_evidence(self, case, candidates):
        picked = []
        for chunk in self.rank_evidence(case, candidates):
            if self.fits(picked + [chunk]):
                picked.append(chunk)
        return picked

    def select(self, case: dict[str, Any], budget: int, *, arm="full") -> Selection:
        if budget < 0:
            raise ValueError("budget must be nonnegative")
        # Extract oracle locus outside the selector input boundary, with provenance.
        locus = edit_locus_slices(case, self.locus_source, self.backend)
        clean = selector_case(case, reference=self.uses_reference,
                              oracle_obligations=self.oracle_obligations)
        self.source = "\n\n".join(c.text for c in locus)
        self.budget, self.arm = budget, arm
        self.obligations = (obligation_symbols(clean) if self.oracle_obligations
                            else allowed_obligation_symbols(clean))
        self.guidance = ("GENERATED GUIDANCE: inspect old driver uses of these changed kernel symbols: "
                         + ", ".join(sorted(self.obligations)) + ".") if self.obligations else ""
        candidates = evidence_chunks(clean, self.backend)
        if arm == "skeleton":
            # Ranking must use the same representation as packing/scoring.
            # Hidden bodies provide neither symbol coverage nor token cost.
            candidates = [Chunk(c.id, render_context([c], "skeleton"), c.kind,
                                frozenset(identifiers(render_context([c], "skeleton"))),
                                tokens=token_count(render_context([c], "skeleton"), self.backend))
                          for c in candidates]
        minimum = token_count(self.prompt([]), self.backend)
        status = "ok" if minimum <= budget else "insufficient_budget"
        # Do not call oracle backends when mandatory source already exceeds budget.
        picked = []
        if status == "ok" and arm != "drop":
            try:
                picked = self.pick_evidence(clean, candidates)
            except (ContextWindowExceeded, ProviderFailure) as exc:
                prefix = "context_window_exceeded" if isinstance(exc, ContextWindowExceeded) else "provider_failure"
                status = f"selection_{prefix}: {exc}"
        meta = {"arm": arm, "guidance": self.guidance,
                "status": status, "minimum_prompt_tokens": minimum,
                "prompt_tokens": token_count(self.prompt(picked), self.backend),
                "locus_cost": token_count(self.source, self.backend),
                "context_tokens": token_count(self.context(picked), self.backend),
                "n_candidates": len(candidates), "token_count_kind": token_count_kind(self.backend),
                "selection_backend": type(self.backend).__name__ if self.uses_reference else "none",
                "selection_model": self.backend.model_name if self.uses_reference else None,
                "selection_backend_config": ({"class": type(self.backend).__name__,
                                              "model": self.backend.model_name,
                                              "max_input_tokens": getattr(self.backend, "max_input_tokens", None)}
                                             if self.uses_reference else None),
                "selection_parameters": {"seed": self.seed,
                                         "coefficients": getattr(self, "coeffs", None),
                                         "keepdrop_tolerance": getattr(self, "tol", None)},
                "selection_signal": ("reference_edit_oracle" if self.uses_reference else
                                     "reference_gold_oracle" if self.oracle_obligations else "allowed_inputs"),
                "evaluation_only_oracle": self.uses_reference or self.oracle_obligations or self.locus_source == "oracle_reference",
                "locus_source": self.locus_source,
                "locus_whole_source_fallback": self.locus_source == "oracle_reference" and locus[0].id == "locus:whole",
                "locus_file_fallbacks": [c.id for c in locus if c.id.startswith("locus:file:")],
                "obligations_source": "reference_gold_oracle" if self.oracle_obligations else "old_uses_kernel_changes_lexical",
                "obligation_symbols": sorted(self.obligations),
                "candidate_pool": "evaluation_casepack_mined_pool",
                "candidate_order": "content_sha256", "seed": self.seed}
        return Selection(self.name, locus, picked, budget, meta)
