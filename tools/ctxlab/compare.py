"""Comparison harness: methods x budgets x ablation arms over a dataset.

Produces one row per (case, method, budget, arm) via make_method +
evaluate_selection, then aggregates per (method, budget, arm). The ablation
arms model two nulls from the meeting deck:

  - Sam-Bodden transfer (skeleton vs drop): evidence rendered as full text
    ("full"), one-line skeletons ("skeleton"), or dropped ("drop").
  - ETH generated-guidance null (pre_spec/pre_reasoner on/off): a generated
    guidance block restating the obligations is appended ("guidance") or not
    ("full"). Whether that generated block helps or merely costs budget is the
    thing under test.

Backend defaults to the offline mock so the whole comparison runs with no
network and no ML deps; only orderings under the mock are meaningful.
"""

from __future__ import annotations

from typing import Any

from .chunks import obligation_symbols
from .evaluation import evaluate_selection
from .methods import make_method

ARMS = ["full", "skeleton", "drop", "guidance"]


def _guidance_block(case: dict[str, Any]) -> str:
    obl = sorted(obligation_symbols(case))
    if not obl:
        return ""
    return ("GENERATED GUIDANCE: this migration must handle the symbols "
            + ", ".join(obl) + ". Update each caller accordingly.")


def render_arm(selection, case: dict[str, Any], arm: str) -> str:
    """Context text for an ablation arm (recall/precision come from the picks)."""
    if arm == "full":
        return selection.context_text()
    if arm == "drop":
        return ""
    if arm == "skeleton":
        return "\n".join(c.text.splitlines()[0] if c.text else "" for c in selection.evidence)
    if arm == "guidance":
        base = selection.context_text()
        block = _guidance_block(case)
        return (base + "\n\n" + block) if block else base
    raise ValueError(f"unknown arm: {arm}")


def run_comparison(cases: list[dict[str, Any]], methods: list[str], budgets: list[int],
                   backend=None, cache=None, arms: list[str] = ("full",)) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        for method_name in methods:
            method = make_method(method_name)
            for budget in budgets:
                selection = method.select(case, budget)
                for arm in arms:
                    ctx = render_arm(selection, case, arm)
                    ev = evaluate_selection(selection, case, backend, cache, context_text=ctx)
                    ev.update({"case_id": case["case_id"], "arm": arm,
                               "arm_context_chars": len(ctx)})
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
        gate_pass = sum(1 for r in rs if r["gate_pass"]) / len(rs)
        out.append({
            "method": method, "budget": budget, "arm": arm, "n": len(rs),
            "tokens_kept": _mean([r["tokens_kept"] for r in rs]),
            "compression": _mean([r["compression"] for r in rs]),
            "gold_recall": _mean([r["gold_recall"] for r in rs]),
            "gold_precision": _mean([r["gold_precision"] for r in rs]),
            "obligation_recall": _mean([r["obligation_recall"] for r in rs]),
            "gate_pass_rate": round(gate_pass, 4),
            "edit_nll": _mean([r["edit_nll"] for r in rs]),
            "within_budget_rate": round(sum(1 for r in rs if r["within_budget"]) / len(rs), 4),
        })
    return out


def summary_markdown(agg: list[dict[str, Any]]) -> str:
    lines = ["# Method comparison summary", "",
             "Backend orderings only (mock is offline; absolute values are not meaningful).",
             "", "| method | budget | arm | n | tok | compr | gRec | gPrec | oblRec | gate | nll |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in agg:
        def f(x, p="{:.2f}"):
            return p.format(x) if isinstance(x, (int, float)) else "-"
        lines.append(
            f"| {r['method']} | {r['budget']} | {r['arm']} | {r['n']} | "
            f"{f(r['tokens_kept'], '{:.0f}')} | {f(r['compression'])} | {f(r['gold_recall'])} | "
            f"{f(r['gold_precision'])} | {f(r['obligation_recall'])} | {f(r['gate_pass_rate'])} | "
            f"{f(r['edit_nll'], '{:.1f}')} |")
    return "\n".join(lines) + "\n"
