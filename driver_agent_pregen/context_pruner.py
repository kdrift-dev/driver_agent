"""Obligation-anchored context pruner (pipeline-side, default-OFF).

Boundary-safe by construction: this module never reads reference diffs, gold
labels, or any evaluation artifact. Its only value signal is overlap with the
machine-derived obligation symbols already present in the patch-generation
payload (compatibility_obligations). It is imported by the patch-generation
shrink step *only* when the DRIVER_AGENT_CONTEXT_PRUNER env flag is set; with
the flag unset the pipeline does not import or call anything here.

The research method ctxlab.methods.ours reuses `marginal_gain` from here so
the paper-facing selector and the pipeline hook share one scoring formula.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_COEFFS_PATH = Path(__file__).with_name("context_pruner_coeffs.json")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_coeffs(path=None) -> dict[str, Any]:
    return json.loads(Path(path or _COEFFS_PATH).read_text())


def marginal_gain(chunk_symbols, obl_remaining, locus_symbols, tokens, coeffs) -> float:
    """Submodular marginal value of adding a chunk given what is already covered."""
    cover = len(set(chunk_symbols) & set(obl_remaining))
    locus_cover = len(set(chunk_symbols) & set(locus_symbols))
    return (coeffs["w_obligation_cover"] * cover
            + coeffs["w_locus_cover"] * locus_cover
            - coeffs["w_size_penalty"] * tokens)


def obligation_symbols_from_payload(obligations_payload) -> set[str]:
    """Symbols the migration must handle, from compatibility_obligations.

    Reads only obligation records (machine-derived); accepts the list or the
    {"obligations": [...]} wrapper. Never touches target driver new code.
    """
    if isinstance(obligations_payload, dict):
        records = list(obligations_payload.get("obligations") or [])
        records += list(obligations_payload.get("semantic_obligations") or [])
    else:
        records = obligations_payload or []
    symbols: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key in ("affected_kernel_symbol", "symbol", "required_added_symbol", "removed_symbol"):
            symbols.update(_symbol_identifiers(rec.get(key)))
        for key in ("symbols", "required_added_symbols", "expected_removed_patterns"):
            values = rec.get(key) or []
            if isinstance(values, str):
                values = [values]
            for val in values:
                symbols.update(_symbol_identifiers(val))
        for candidate in rec.get("replacement_candidates") or []:
            val = candidate.get("symbol") if isinstance(candidate, dict) else candidate
            symbols.update(_symbol_identifiers(val))
    return symbols


def _symbol_identifiers(value) -> set[str]:
    if not isinstance(value, str):
        return set()
    # Native keys are e.g. "old_api (function)" or "ops.member (struct_field)".
    # Do not let the kind suffix or call arguments become relevance signals.
    name = value.split("(", 1)[0].strip()
    return set(_IDENT_RE.findall(name)) - {"struct", "union", "enum"}


def _block_symbols(item: dict[str, Any]) -> set[str]:
    return set(_IDENT_RE.findall(str(item.get("content", "") or "")))


def is_mandatory_source(item: dict[str, Any]) -> bool:
    """Editable slices and full-module fallback are never optional context."""
    return (bool(item.get("editable", False))
            or str(item.get("edit_scope", "")) in ("edit_required", "edit_candidate")
            or str(item.get("source_item_kind", "")) in (
                "full_file", "diagnostic_failure_function", "diagnostic_failure_window",
                "diagnostic_failure_file"))


def prune_old_module_sources(sources: list[dict[str, Any]], obligation_symbols,
                             coeffs=None) -> list[dict[str, Any]]:
    """Reorder read-only source blocks by obligation coverage (editable kept first).

    Non-destructive ranking helper; the flag-on patch shrink selects complete
    read-only blocks from this order under its rendered prompt budget. The
    size penalty uses source characters (not a claimed tokenizer count).
    """
    coeffs = coeffs or load_coeffs()
    obl = set(obligation_symbols)

    editable = [it for it in sources if is_mandatory_source(it)]
    readonly = [it for it in sources if not is_mandatory_source(it)]
    scored = sorted(
        readonly,
        key=lambda it: marginal_gain(_block_symbols(it), obl, (), len(str(it.get("content", ""))), coeffs),
        reverse=True,
    )
    return editable + scored
