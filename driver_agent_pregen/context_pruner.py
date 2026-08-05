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
        records = obligations_payload.get("obligations") or []
    else:
        records = obligations_payload or []
    symbols: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key in ("symbol", "required_added_symbol", "removed_symbol"):
            val = rec.get(key)
            if isinstance(val, str) and val:
                symbols.add(val)
        for key in ("symbols", "required_added_symbols", "expected_removed_patterns"):
            for val in rec.get(key) or []:
                if isinstance(val, str) and val:
                    symbols.add(val.rstrip("(").split("(")[0])
    return symbols


def _block_symbols(item: dict[str, Any]) -> set[str]:
    return set(_IDENT_RE.findall(str(item.get("content", "") or "")))


def prune_old_module_sources(sources: list[dict[str, Any]], obligation_symbols,
                             coeffs=None) -> list[dict[str, Any]]:
    """Reorder read-only source blocks by obligation coverage (editable kept first).

    Non-destructive: returns a reordered list, never drops blocks — the
    existing budget shrink still decides what fits. Editable blocks retain
    their leading position (the edit locus is never demoted); read-only blocks
    are ranked by obligation-symbol overlap so the budget keeps the useful
    ones. Pure and deterministic.
    """
    coeffs = coeffs or load_coeffs()
    obl = set(obligation_symbols)

    def is_editable(item: dict[str, Any]) -> bool:
        return bool(item.get("editable", False)) or \
            str(item.get("edit_scope", "")) in ("edit_required", "edit_candidate")

    editable = [it for it in sources if is_editable(it)]
    readonly = [it for it in sources if not is_editable(it)]
    scored = sorted(
        readonly,
        key=lambda it: marginal_gain(_block_symbols(it), obl, (), len(str(it.get("content", ""))), coeffs),
        reverse=True,
    )
    return editable + scored
