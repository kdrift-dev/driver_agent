"""Context-value metrics: the expressibility gate and PPL-of-reference-edit.

`score_context(context_text, case, backend, cache=None)` is the one entry
point method zoo and harnesses call: it assembles the migration prompt, runs
the expressibility gate (deterministic, no ML) and — if a backend is given —
the teacher-forced NLL of the reference edit, returning both under a single
dict. Backends live in ctxlab.backends; the gate needs none.
"""

from __future__ import annotations

from typing import Any

from .backends import ContextWindowExceeded, ProviderFailure, cached_score, lexical_token_count
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


def render_context(chunks, arm="full", guidance="") -> str:
    """Single renderer shared by packing, oracle ranking and final scoring."""
    if arm == "drop":
        return ""
    if arm == "skeleton":
        return "\n".join(c.text.splitlines()[0] if c.text else "" for c in chunks)
    if arm not in ("full", "guidance"):
        raise ValueError(f"unknown arm: {arm}")
    text = "\n\n".join(c.text for c in chunks)
    return (text + "\n\n" + guidance).strip() if arm == "guidance" and guidance else text


def token_count(text: str, backend=None) -> int:
    """Use the model tokenizer, or a labeled occurrence proxy offline."""
    if backend is None:
        return lexical_token_count(text)
    if not hasattr(backend, "count_tokens"):
        raise ValueError("comparison requires a backend with a local count_tokens tokenizer")
    return backend.count_tokens(text)


def token_count_kind(backend=None) -> str:
    return getattr(backend, "token_count_kind", "non_model_lexical_proxy" if backend is None else "unknown")


def strict_score(backend, prompt, target, cache=None):
    """Reject model-window overflow and any provider-side silent truncation."""
    prompt_tokens = token_count(prompt, backend)
    target_tokens = token_count(target, backend)
    window = getattr(backend, "max_input_tokens", None)
    if window is not None and prompt_tokens + target_tokens > window:
        raise ContextWindowExceeded(
            f"prompt ({prompt_tokens}) + target ({target_tokens}) exceeds window ({window})")
    scored = cached_score(backend, cache, prompt, target) if cache is not None else backend.score(prompt, target)
    if scored.get("prompt_truncated"):
        raise ContextWindowExceeded("backend truncated the scored prompt")
    reported = scored.get("n_prompt_tokens")
    if reported is not None and reported != prompt_tokens:
        raise ContextWindowExceeded(f"backend prompt token count changed: {prompt_tokens} -> {reported}")
    scored.setdefault("cache_hit", False)
    return scored


def score_context(context_text: str, case: dict[str, Any], backend=None,
                  cache=None, old_source_budget: int = 12000, *,
                  old_source_text: str = None, strict_prompt: bool = False) -> dict[str, Any]:
    """Historical behavior is retained unless an explicit source is supplied.

    v1 uses the first old_source_budget characters for scoring and the full
    old source for the gate. v2 callers supply old_source_text: this exact
    source reaches both gate and scorer, without clipping. strict_prompt
    rejects model-window overflow instead of accepting a truncated score.
    """
    explicit_source = old_source_text is not None
    old_source = old_source_text if explicit_source else case["old_source"][:old_source_budget]
    gate_source = old_source if explicit_source else case["old_source"]
    prompt = build_prompt(context_text, old_source)
    result: dict[str, Any] = {
        "gate": expressibility_check(context_text, gate_source, case["reference_diff"]),
        "ppl": None, "status": "ok", "prompt": prompt,
        "old_source_text": old_source, "context_text": context_text,
        "scoring_protocol": "explicit_source_v2" if explicit_source else "legacy_v1",
    }
    if backend is not None:
        try:
            if strict_prompt:
                result["ppl"] = strict_score(backend, prompt, case["reference_diff"], cache)
            elif cache is not None:
                result["ppl"] = cached_score(backend, cache, prompt, case["reference_diff"])
            else:
                result["ppl"] = backend.score(prompt, case["reference_diff"])
                result["ppl"]["cache_hit"] = False
        except ProviderFailure as exc:
            result["status"] = f"provider_failure: {exc}"
        except ContextWindowExceeded as exc:
            result["status"] = f"context_window_exceeded: {exc}"
    return result
