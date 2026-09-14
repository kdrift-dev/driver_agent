#!/usr/bin/env python3
"""Adversarial contracts for shared rendering, whole-block budgets and dispatch."""
from __future__ import annotations

import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver_agent_pregen.paired_context import (
    MessageCharCounter, digest, prepare_pair, validate_prepared_dispatch,
)


def rejects(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("expected explicit refusal")


class NonAdditiveCounter:
    identity = {"kind": "synthetic_nonadditive", "unit": "test_units", "binding": "verified_test_only"}

    def count(self, request):
        assert request["messages"][0]["role"] == "system"
        assert request["messages"][1]["role"] == "user"
        content = request["messages"][1]["content"]
        a, b = '"id": "a"' in content, '"id": "b"' in content
        return 100 + 30 * a + 40 * b - 20 * (a and b)

    def dispatch_compatible(self, request):
        return request["model"] == "fixture-model"


def main():
    sources = [{"path": "drivers/demo/demo.c", "content": "int old_api(void);\nint demo(void) { return old_api(); }\n"}]
    requirements = [{"symbol": "old_api", "old_target_uses": [{"path": sources[0]["path"], "line": 2, "function": "demo"}]}]
    blocks = [{"id": "a", "symbols": [], "text": "whole unrelated kernel excerpt"},
              {"id": "b", "symbols": ["old_api"], "text": "whole required kernel excerpt"}]
    original = copy.deepcopy([sources, requirements, blocks])
    counter = NonAdditiveCounter()
    args = {"counter": counter, "model": "fixture-model", "max_tokens": 100, "budget": 150}
    result = prepare_pair(sources, requirements, blocks, **args)
    first, ranked = (result["conditions"][name] for name in ("first_fit", "obligation_ranked"))
    assert first["input_count"] == ranked["input_count"] == 150
    assert first["selected_ids"] == ranked["selected_ids"] == ["a", "b"]
    assert ranked["decisions"][0]["id"] == "b"
    assert first["request_body"] == ranked["request_body"]
    assert first["messages_sha256"] == ranked["messages_sha256"]
    assert [sources, requirements, blocks] == original
    for condition in (first, ranked):
        assert condition["input_payload"]["old_module_sources"] == sources
        assert condition["input_payload"]["migration_requirements"] == requirements
        assert condition["request_body"]["max_tokens"] == 100
        assert not any(term in condition["prompt"] for term in ("first_fit", "obligation_ranked", "trial_count", "generation_ready"))
    validate_prepared_dispatch(first, counter=counter, budget=150, request_body=first["request_body"])
    pressure = prepare_pair(sources, requirements, blocks, **{**args, "budget": 140})
    assert pressure["conditions"]["first_fit"]["selected_ids"] == ["a"]
    assert pressure["conditions"]["obligation_ranked"]["selected_ids"] == ["b"]
    print("PASS complete nonadditive message counts, same-set byte identity, intact immutable input and audit isolation")

    denied = prepare_pair(sources, requirements, blocks, **{**args, "budget": 99})
    for condition in denied["conditions"].values():
        assert condition["status"] == "mandatory_input_budget_exceeded"
        assert not condition["selected_ids"] and not condition["generation_ready"]
        assert condition["input_payload"]["old_module_sources"] == sources
        rejects(lambda: validate_prepared_dispatch(condition, counter=counter, budget=99, request_body=condition["request_body"]))
    no_binding = prepare_pair(sources, requirements, blocks, **{**args, "model": "different-model"})
    assert all(not c["generation_ready"] for c in no_binding["conditions"].values())
    changed = copy.deepcopy(first["request_body"])
    changed["messages"][1]["content"] += "later modification"
    rejects(lambda: validate_prepared_dispatch(first, counter=counter, budget=150, request_body=changed))
    rejects(lambda: validate_prepared_dispatch(first, counter=counter, budget=151, request_body=first["request_body"]))
    other_counter = NonAdditiveCounter()
    other_counter.identity = {**counter.identity, "binding": "another"}
    rejects(lambda: validate_prepared_dispatch(first, counter=other_counter, budget=150, request_body=first["request_body"]))
    print("PASS mandatory overflow, provider mismatch, changed request, budget and counter all refuse dispatch")

    for invalid in (True, False, 1.5, "150", 0, -1):
        rejects(lambda: prepare_pair(sources, requirements, blocks, **{**args, "budget": invalid}))
    for invalid in (True, 0, -1, 2.5):
        bad = NonAdditiveCounter()
        bad.count = lambda request, value=invalid: value
        rejects(lambda: prepare_pair(sources, requirements, blocks, **{**args, "counter": bad}))
    duplicate = blocks + [{**blocks[0], "text": "different body, same id"}]
    rejects(lambda: prepare_pair(sources, requirements, duplicate, **args))
    rejects(lambda: prepare_pair(sources + sources, requirements, blocks, **args))
    rejects(lambda: prepare_pair([{**sources[0], "path": "../outside.c"}], requirements, blocks, **args))
    rejects(lambda: prepare_pair(sources, [{**requirements[0], "reference_patch": "forbidden"}], blocks, **args))
    poisoned = copy.deepcopy(requirements)
    poisoned[0]["old_target_uses"][0]["reference_patch"] = "forbidden"
    rejects(lambda: prepare_pair(sources, poisoned, blocks, **args))
    def corrupt_count(request):
        request["messages"][1]["content"] = "removed source"
        return 5
    bad = NonAdditiveCounter()
    bad.count = corrupt_count
    rejects(lambda: prepare_pair(sources, requirements, blocks, **{**args, "counter": bad}))
    def corrupt_compatibility(request):
        request["messages"][1]["content"] = "removed source"
        return True
    bad = NonAdditiveCounter()
    bad.dispatch_compatible = corrupt_compatibility
    rejects(lambda: prepare_pair(sources, requirements, blocks, **{**args, "counter": bad}))
    bad.dispatch_compatible = lambda request: "yes"
    rejects(lambda: prepare_pair(sources, requirements, blocks, **{**args, "counter": bad}))
    chars = prepare_pair(sources, requirements, blocks, counter=MessageCharCounter(), model="fixture-model", max_tokens=100)
    assert all(not c["generation_ready"] for c in chars["conditions"].values())
    assert chars["source_sha256"] == digest(sources)
    print("PASS strict budget/counter types, duplicate IDs, source paths, and diagnostic counters never enable generation")


if __name__ == "__main__":
    main()
