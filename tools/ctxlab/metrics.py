"""Context-value metrics: the expressibility gate and PPL-of-reference-edit.

`score_context(context_text, case, backend, cache=None)` is the one entry
point method zoo and harnesses call: it assembles the migration prompt, runs
the expressibility gate (deterministic, no ML) and — if a backend is given —
the teacher-forced NLL of the reference edit, returning both under a single
dict. Backends live in ctxlab.backends; the gate needs none.
"""

from __future__ import annotations

from typing import Any

from .backends import ProviderFailure, cached_score
from .textutil import identifiers, strip_comments_and_strings

PROMPT_TEMPLATE = (
    "You are migrating a Linux kernel driver from an old kernel version to a "
    "new one. Using the kernel-side evidence below (if any) and the old driver "
    "source, produce the unified diff that adapts the driver to the new kernel.\n\n"
    "=== KERNEL-SIDE EVIDENCE ===\n{context}\n\n"
    "=== OLD DRIVER SOURCE ===\n{old_source}\n\n"
    "=== MIGRATION DIFF ===\n"
)


def added_lines(reference_diff: str) -> list[str]:
    return [line[1:] for line in reference_diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def expressibility_check(context_text: str, old_source_text: str,
                         reference_diff: str) -> dict[str, Any]:
    """Can the reference edit be expressed given (context + old source)?

    Returns identifier coverage of the edit's added lines. An identifier is
    "covered" if it appears anywhere in the candidate context or in the old
    source. This is deliberately a *mention* check, not a full declaration
    check: it is monotone, deterministic and cheap, and its per-condition
    pass-rate profile (gold ~1.0, none ~0.0) is validated by the experiment
    itself.
    """
    needed = identifiers(strip_comments_and_strings("\n".join(added_lines(reference_diff))))
    available = identifiers(old_source_text) | identifiers(context_text)
    missing = sorted(needed - available)
    coverage = 1.0 if not needed else (len(needed) - len(missing)) / len(needed)
    return {
        "needed_identifiers": len(needed),
        "missing_identifiers": missing,
        "coverage": round(coverage, 4),
        "gate_pass": not missing,
    }


def build_prompt(context_text: str, old_source: str) -> str:
    return PROMPT_TEMPLATE.format(context=context_text or "(none)", old_source=old_source)


def score_context(context_text: str, case: dict[str, Any], backend=None,
                  cache=None, old_source_budget: int = 12000) -> dict[str, Any]:
    """Score a candidate context for one case.

    case must carry `reference_diff` and `old_source` (as produced by
    ctxlab.dataset.load_case). Returns {gate, ppl, status}; ppl is None when
    no backend is passed, and status carries a provider_failure string when a
    backend errored (the gate is always computed).
    """
    old_source = case["old_source"][:old_source_budget]
    result: dict[str, Any] = {
        "gate": expressibility_check(context_text, case["old_source"], case["reference_diff"]),
        "ppl": None,
        "status": "ok",
    }
    if backend is not None:
        prompt = build_prompt(context_text, old_source)
        try:
            if cache is not None:
                result["ppl"] = cached_score(backend, cache, prompt, case["reference_diff"])
            else:
                score = backend.score(prompt, case["reference_diff"])
                score["cache_hit"] = False
                result["ppl"] = score
        except ProviderFailure as exc:
            result["status"] = f"provider_failure: {exc}"
    return result


def token_count(text: str) -> int:
    """Cheap, deterministic token proxy used for budgets and reporting."""
    return len(identifiers(text)) if text else 0
