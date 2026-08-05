"""Scoring a method's selection against a case: recall, precision, budget, PPL.

Given a Selection and the case, compute the metrics the comparison table needs:
gold-symbol recall/precision of the chosen evidence, token budget adherence,
compression vs the full evidence set, the expressibility gate, and (with a
backend) the edit-NLL under the selected context. Evaluation-only.
"""

from __future__ import annotations

from typing import Any

from .chunks import evidence_chunks, obligation_symbols
from .metrics import score_context


def evaluate_selection(selection, case: dict[str, Any], backend=None,
                       cache=None) -> dict[str, Any]:
    obligations = set(obligation_symbols(case))
    all_evidence = evidence_chunks(case)
    full_tokens = sum(c.tokens for c in all_evidence) + \
        sum(c.tokens for c in selection.locus)

    picked_gold = {c for c in selection.evidence if c.is_gold}
    gold_total = sum(1 for c in all_evidence if c.is_gold)
    gold_recall = len(picked_gold) / gold_total if gold_total else 1.0
    precision = len(picked_gold) / len(selection.evidence) if selection.evidence else (
        1.0 if not gold_total else 0.0)

    # obligation coverage: how many obligation symbols the kept context carries
    covered = set()
    for c in selection.evidence:
        covered |= set(c.symbols) & obligations
    obl_recall = len(covered) / len(obligations) if obligations else 1.0

    scored = score_context(selection.context_text(), case, backend, cache)
    return {
        "method": selection.method,
        "budget": selection.budget,
        "tokens_kept": selection.tokens,
        "within_budget": selection.tokens <= selection.budget,
        "compression": round(full_tokens / selection.tokens, 4) if selection.tokens else None,
        "n_evidence": len(selection.evidence),
        "gold_recall": round(gold_recall, 4),
        "gold_precision": round(precision, 4),
        "obligation_recall": round(obl_recall, 4),
        "gate_coverage": scored["gate"]["coverage"],
        "gate_pass": scored["gate"]["gate_pass"],
        "edit_nll": scored["ppl"]["nll_sum"] if scored["ppl"] else None,
        "status": scored["status"],
    }
