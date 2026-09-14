"""Method zoo: context-selection approaches under one interface.

Every method implements `select(case, budget) -> Selection` (see base.py).
The registry maps stable names to factories; reference-scoring oracles require
an explicit backend. All methods run over evaluation case packs; `ours` shares
a scoring formula with the default-OFF pipeline context pruner.
"""

from __future__ import annotations

from .base import Method, Selection
from .keepdrop_dd import KeepDropDD
from .longcodezip import LongCodeZip
from .ours import ObligationAnchored, OracleObligations
from .simple import ErrPrune, RandomBudget, ShapleyProbe, StaticEpam, TruncBaseline

_REGISTRY = {
    "trunc_baseline": TruncBaseline,
    "random_budget": RandomBudget,
    "longcodezip": LongCodeZip,
    "keepdrop_dd": KeepDropDD,
    "errprune": ErrPrune,
    "shapley_probe": ShapleyProbe,
    "static_epam": StaticEpam,
    "ours": ObligationAnchored,
    "oracle_obligations": OracleObligations,
}


def method_names() -> list[str]:
    return list(_REGISTRY)


def make_method(name: str, **kwargs) -> Method:
    if name not in _REGISTRY:
        raise ValueError(f"unknown method: {name}; known: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


__all__ = ["Method", "Selection", "method_names", "make_method"]
