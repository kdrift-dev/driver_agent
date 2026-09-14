"""Comparison harness: methods x budgets x ablation arms over a dataset.

Produces one row per (case, method, budget, arm) via make_method +
evaluate_selection, then aggregates per (method, budget, arm). The ablation
arms model two nulls from the meeting deck:

  - Sam-Bodden transfer (skeleton vs drop): evidence rendered as full text
    ("full"), one-line skeletons ("skeleton"), or dropped ("drop").
  - ETH generated-guidance null (pre_spec/pre_reasoner on/off): a deterministic
    guidance block restating the allowed-input obligation approximation is appended ("guidance") or not
    ("full"). Whether that generated block helps or merely costs budget is the
    thing under test.

Backend defaults to the offline mock so the whole comparison runs with no
network and no ML deps; only orderings under the mock are meaningful.
"""

from __future__ import annotations

from typing import Any

from .backends import MockBackend
from .evaluation import evaluate_selection
from .methods import make_method

ARMS = ["full", "skeleton", "drop", "guidance"]


def run_comparison(cases: list[dict[str, Any]], methods: list[str], budgets: list[int],
                   backend=None, cache=None, arms: list[str] = ("full",), *,
                   seed=0, locus_source="old_source") -> list[dict[str, Any]]:
    backend = backend if backend is not None else MockBackend()
    rows: list[dict[str, Any]] = []
    for case in cases:
        for method_name in methods:
            # The same explicit backend/tokenizer is used for selection and scoring.
            method = make_method(method_name, backend=backend, seed=seed,
                                 locus_source=locus_source, cache=cache)
            for budget in budgets:
                for arm in arms:
                    # Rendering/guidance overhead affects packing, so select per arm.
                    selection = method.select(case, budget, arm=arm)
                    ev = evaluate_selection(selection, case, backend, cache)
                    ev.update({"case_id": case["case_id"], "arm": arm,
                               "arm_context_chars": len(ev["context_text"])})
                    rows.append(ev)
    return rows


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["method"], r["budget"], r["arm"]), []).append(r)
    out = []
    for (method, budget, arm), rs in sorted(groups.items()):
        valid = [r for r in rs if r["status"] == "ok"]
        gate_pass = _mean([float(r["gate_pass"]) for r in valid])
        out.append({
            "method": method, "budget": budget, "arm": arm, "n": len(rs),
            "n_valid": len(valid), "n_excluded": len(rs) - len(valid),
            "token_count_kind": sorted({r["token_count_kind"] for r in rs}),
            "selection_signal": sorted({r["selection_signal"] for r in rs}),
            "locus_source": sorted({r["locus_source"] for r in rs}),
            "selection_backend": sorted({r["selection_backend"] for r in rs}),
            "scoring_backend": sorted({r["scoring_backend"] for r in rs}),
            "scoring_model": sorted({str(r["scoring_model"]) for r in rs}),
            "excluded_statuses": sorted({r["status"] for r in rs if r["status"] != "ok"}),
            "tokens_kept": _mean([r["tokens_kept"] for r in valid]),
            "compression": _mean([r["compression"] for r in valid]),
            "gold_recall": _mean([r["gold_recall"] for r in valid]),
            "gold_precision": _mean([r["gold_precision"] for r in valid]),
            "obligation_recall": _mean([r["obligation_recall"] for r in valid]),
            "gate_pass_rate": round(gate_pass, 4) if gate_pass is not None else None,
            "edit_nll": _mean([r["edit_nll"] for r in valid]),
            "within_budget_rate": round(sum(1 for r in rs if r["within_budget"]) / len(rs), 4),
        })
    return out


def summary_markdown(agg: list[dict[str, Any]]) -> str:
    lines = ["# Method comparison summary", "",
             "v2: full rendered prompt budgets; invalid rows excluded from quality means. Mock counts are non-model lexical proxies. Case-pack pools remain evaluation-only; oracle rows use reference signals.",
             "", "| method | signal | budget | arm | valid/total | tok | compr | gRec | gPrec | oblRec | gate | nll |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in agg:
        def f(x, p="{:.2f}"):
            return p.format(x) if isinstance(x, (int, float)) else "-"
        lines.append(
            f"| {r['method']} | {', '.join(r['selection_signal'])} | {r['budget']} | {r['arm']} | {r['n_valid']}/{r['n']} | "
            f"{f(r['tokens_kept'], '{:.0f}')} | {f(r['compression'])} | {f(r['gold_recall'])} | "
            f"{f(r['gold_precision'])} | {f(r['obligation_recall'])} | {f(r['gate_pass_rate'])} | "
            f"{f(r['edit_nll'], '{:.1f}')} |")
    return "\n".join(lines) + "\n"
