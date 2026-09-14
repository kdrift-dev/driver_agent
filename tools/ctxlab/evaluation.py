"""Evaluate selected evidence, keeping gold labels outside selection inputs."""

from __future__ import annotations

from typing import Any

from .chunks import evidence_chunks, obligation_symbols
from .metrics import build_prompt, render_context, score_context, token_count
from .textutil import identifiers


def evaluate_selection(selection, case: dict[str, Any], backend=None,
                       cache=None, context_text: str = None) -> dict[str, Any]:
    obligations = set(obligation_symbols(case))
    all_evidence = evidence_chunks(case, backend, include_labels=True)
    gold_ids = {c.id for c in all_evidence if c.is_gold}
    picked_ids = {c.id for c in selection.evidence}
    gold_recall = len(picked_ids & gold_ids) / len(gold_ids) if gold_ids else 1.0
    precision = len(picked_ids & gold_ids) / len(picked_ids) if picked_ids else (0.0 if gold_ids else 1.0)
    ctx = selection.context_text() if context_text is None else context_text
    # This is evidence coverage, separate from the source+evidence gate. A
    # skeleton/drop ablation cannot inherit symbols from unrendered bodies.
    covered = identifiers(ctx) & obligations
    obl_recall = len(covered) / len(obligations) if obligations else 1.0
    source = selection.old_source_text()
    prompt = build_prompt(ctx, source)
    actual_tokens = token_count(prompt, backend)
    selection_status = selection.meta.get("status", "ok")
    if actual_tokens > selection.budget and selection_status == "ok":
        selection_status = "insufficient_budget"
    # Compute the diagnostic gate even for rejected prompts, but exclude such
    # rows from gate/NLL aggregate denominators and never send them to a model.
    scored = score_context(ctx, case, backend if selection_status == "ok" else None,
                           cache, old_source_text=source, strict_prompt=True)
    status = selection_status if selection_status != "ok" else scored["status"]
    full_context = render_context(all_evidence, selection.meta.get("arm", "full"),
                                  selection.meta.get("guidance", ""))
    full_tokens = token_count(build_prompt(full_context, source), backend)
    return {
        **selection.meta,
        "method": selection.method, "budget": selection.budget,
        "protocol": "method_comparison_v2",
        "tokens_kept": actual_tokens, "prompt_tokens": actual_tokens,
        "within_budget": actual_tokens <= selection.budget,
        "budget_scope": "entire_rendered_prompt_including_source_and_guidance",
        "compression": round(full_tokens / actual_tokens, 4) if actual_tokens else None,
        "n_evidence": len(selection.evidence), "evidence_ids": [c.id for c in selection.evidence],
        "gold_recall": round(gold_recall, 4), "gold_precision": round(precision, 4),
        "obligation_recall": round(obl_recall, 4),
        "obligation_recall_scope": "rendered_context_only",
        "gate_coverage": scored["gate"]["coverage"], "gate_pass": scored["gate"]["gate_pass"],
        "gate_missing_identifiers": scored["gate"]["missing_identifiers"],
        "edit_nll": scored["ppl"]["nll_sum"] if scored["ppl"] else None,
        "scoring_backend": type(backend).__name__ if backend else "none",
        "scoring_model": backend.model_name if backend else None,
        "scoring": scored["ppl"], "status": status,
        "prompt_text": prompt, "context_text": ctx, "old_source_text": source,
        "context_tokens": token_count(ctx, backend), "locus_tokens": token_count(source, backend),
        "locus_ids": [c.id for c in selection.locus],
    }
