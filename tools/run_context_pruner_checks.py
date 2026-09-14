#!/usr/bin/env python3
"""Dependency-free regression checks for the opt-in pipeline context pruner.

Uses synthetic old-driver sources, native obligation/impact schemas, and local
dry generation only. No kernel checkout, model, network, reference patch, or
build toolchain is required.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver_agent_pregen import patch_generation as pg
from driver_agent_pregen.context_pruner import obligation_symbols_from_payload
from driver_agent_pregen.llm_client import LLMClient, LLMConfig


@contextmanager
def pruner_flag(enabled):
    previous = os.environ.pop("DRIVER_AGENT_CONTEXT_PRUNER", None)
    if enabled:
        os.environ["DRIVER_AGENT_CONTEXT_PRUNER"] = "1"
    try:
        yield
    finally:
        os.environ.pop("DRIVER_AGENT_CONTEXT_PRUNER", None)
        if previous is not None:
            os.environ["DRIVER_AGENT_CONTEXT_PRUNER"] = previous


def block(name, content, scope="context_only"):
    return {
        "path": "drivers/demo/" + name + ".c", "function": name,
        "source_item_kind": "context_function_block", "start_line": 1,
        "end_line": 8, "editable": scope != "context_only",
        "edit_scope": scope, "content": content,
    }


def off_fixture():
    return {
        "compatibility_obligations": {"obligations": [{"affected_kernel_symbol": "old_api (function)"}]},
        "old_module_sources": [
            block("z_required", "old_api();\n" * 700, "edit_required"),
            block("b_context", "new_api();"), block("a_context", "unrelated();"),
        ] + [block("helper_%02d" % i, "return 0;", "edit_candidate") for i in range(34)],
    }


def check_off_equivalence():
    # Frozen outputs from 041e60c's shrink implementation with the flag unset.
    # Both ordinary capping/sorting and the >32/>24 fallback branches are covered.
    golden = {
        100000: "9741604421fc76988ed68f377a971fa60235e3347659248ad77b83474dc62829",
        3500: "e68e24094425cf80010be6a06326de281e3ffd255ee75f52efcc3e1eb6ba606b",
    }
    with pruner_flag(False):
        for budget, expected in golden.items():
            fixture = off_fixture()
            original = deepcopy(fixture)
            result = pg._shrink_patch_input_payload(fixture, budget_chars=budget)
            actual = hashlib.sha256(json.dumps(result, ensure_ascii=False).encode()).hexdigest()
            assert actual == expected, (budget, actual)
            assert fixture == original, "source compaction mutated upstream source records"


def check_native_symbols():
    symbols = obligation_symbols_from_payload({
        "obligations": [{
            "affected_kernel_symbol": "old_api (function)",
            "replacement_candidates": [{"symbol": "new_api"}],
            "expected_removed_patterns": ["obsolete("],
        }],
        "semantic_obligations": [{
            "affected_kernel_symbol": "ops.member (struct_field)",
            "replacement_candidates": [{"symbol": "struct new_type"}],
        }],
    })
    assert symbols == {"old_api", "new_api", "obsolete", "ops", "member", "new_type"}, symbols
    assert obligation_symbols_from_payload([{"required_added_symbols": "single_symbol"}]) == {"single_symbol"}


def check_ranked_budget():
    edit = block("z_edit", "old_api();\n" * 1000, "edit_required")
    useful = block("z_useful", "old_api(); new_api();")
    noise = block("a_noise", "unrelated(); unrelated();")
    sources = [noise, edit, useful]
    payload = {"old_module_sources": sources, "compatibility_obligations": {
        "obligations": [{"affected_kernel_symbol": "old_api (function)",
                         "replacement_candidates": [{"symbol": "new_api"}]}],
    }}
    original = deepcopy(payload)
    with pruner_flag(True):
        fit = {**payload, "old_module_sources": [edit, useful]}
        budget = len(json.dumps(fit, indent=2))
        result, info = pg._shrink_patch_input_payload(payload, budget_chars=budget)
        assert result["old_module_sources"] == [edit, useful]
        assert info["final_chars"] == budget
        assert info["status"] == "within_budget"
        assert payload == original
        audit = info["context_pruner"]["blocks"]
        assert audit[0]["action"] == "dropped" and audit[2]["reordered"]
        assert audit[2]["obligation_overlap"] == ["new_api", "old_api"]
        assert result["old_module_sources"][0]["content"] == edit["content"]

        # No old 24/28 editable-count cutoff survives, even if they cannot fit.
        mandatory = [block(str(i), "int keep_%d;" % i, "edit_candidate") for i in range(40)]
        result, info = pg._shrink_patch_input_payload({"old_module_sources": mandatory + [noise]}, budget_chars=1)
        assert result["old_module_sources"] == mandatory
        assert info["status"] == "insufficient_budget" and info["over_budget_after_trim"]
        full = {"path": "drivers/demo/full.c", "source_item_kind": "full_file", "content": "x" * 25000}
        result, info = pg._shrink_patch_input_payload({"old_module_sources": [full]}, budget_chars=1)
        assert result["old_module_sources"] == [full] and info["over_budget_after_trim"]


def graph_fixture():
    def rec(name, scope, symbols=()):
        result = {"function": name, "file": "drivers/demo/demo.c", "start_line": 1,
                  "end_line": 1, "edit_scope": scope, "tier": "hard" if scope == "edit_required" else "context"}
        if symbols:
            result["uses_changed_kernel_symbols"] = list(symbols)
        return result
    seed = rec("seed", "edit_required", ["old_api (function)"])
    other = rec("other_seed", "edit_required", ["other_api (function)"])
    caller = rec("caller", "edit_candidate")
    contexts = [rec(name, "context_only") for name in ("a_noise", "z_useful", "unrelated", "transitive", "ordinary")]
    return {
        "affected_functions": [seed, other, caller],
        "edit_required_functions": [seed, other], "edit_candidate_functions": [caller],
        "context_only_functions": contexts, "context_functions": contexts,
        "editable_functions": [seed, other, caller],
        "edges": [
            {"from": "caller", "to": "seed", "kind": "edit_candidate_direct_caller"},
            {"from": "seed", "to": "z_useful", "kind": "context_callback_chain"},
            {"from": "seed", "to": "a_noise", "kind": "context_callback_chain"},
            {"from": "other_seed", "to": "unrelated", "kind": "context_callback_chain"},
            {"from": "z_useful", "to": "transitive", "kind": "context_callback_chain"},
            {"from": "seed", "to": "ordinary", "kind": "calls"},
            {"from": "seed", "to": "old_api (function)", "kind": "uses_kernel_symbol"},
        ],
    }


def check_connected_expansion():
    graph = graph_fixture()
    original = deepcopy(graph)
    with pruner_flag(True):
        result = pg._filter_impact_graph_for_symbols(graph, {"old_api (function)"})
    assert [item["function"] for item in result["edit_required_functions"]] == ["seed"]
    assert [item["function"] for item in result["edit_candidate_functions"]] == ["caller"]
    assert {item["function"] for item in result["context_only_functions"]} == {"a_noise", "z_useful"}
    assert result["edit_scope_counts"] == {"edit_required": 1, "edit_candidate": 1, "context_only": 2}
    assert graph == original
    with pruner_flag(False):
        result = pg._filter_impact_graph_for_symbols(graph, {"old_api (function)"})
    assert result["edit_candidate_functions"] == [] and result["context_only_functions"] == []


def check_generation():
    with tempfile.TemporaryDirectory(prefix="context-pruner-check-") as temporary, pruner_flag(True):
        root = Path(temporary)
        extracted = root / "extracted"
        path = extracted / "old_module_sources/drivers/demo/demo.c"
        path.parent.mkdir(parents=True)
        lines = ["int seed(void) { return old_api(); }", "int caller(void) { return seed(); }",
                 "int z_useful(void) { return new_api(); }", "int a_noise(void) { return unrelated(); }"]
        path.write_text("\n".join(lines) + "\n")
        graph = graph_fixture()
        for records in [graph[key] for key in ("affected_functions", "edit_required_functions", "edit_candidate_functions", "context_only_functions")]:
            for item in records:
                names = ["seed", "caller", "z_useful", "a_noise"]
                if item["function"] in names:
                    item["start_line"] = item["end_line"] = names.index(item["function"]) + 1
        obligations = {"obligations": [{"id": "OBL-001", "affected_kernel_symbol": "old_api (function)",
            "affected_driver_functions": ["seed"], "hard_checks": ["old_api is removed"],
            "replacement_candidates": [{"symbol": "new_api"}]}]}
        client = LLMClient(LLMConfig(enabled=False))
        kwargs = dict(llm_client=client, llm_calls_dir=root / "llm_calls", run_dir=root,
                      extracted_dir=extracted, case_id="synthetic", module_path="drivers/demo",
                      context_packet={}, pre_reasoning={}, impact_graph=graph,
                      compatibility_obligations=obligations, dry_run=True, source_mode="focused")
        original_budget = pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS
        try:
            pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS = 1000000
            pg.run_patch_generation_agent(**kwargs)
            stage = root / "llm_calls/03_patch_generation"
            saved = json.loads((stage / "input.json").read_text())
            useful_only = deepcopy(saved)
            useful_only["old_module_sources"] = [item for item in saved["old_module_sources"] if item["function"] != "a_noise"]
            render = lambda data: pg.build_patch_generation_prompt(data, source_mode="focused", source_with_line_numbers=True)
            _, fit_info = pg._shrink_patch_input_payload(useful_only, budget_chars=1000000, render_prompt=render)
            pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS = fit_info["final_chars"]
            result = pg.run_patch_generation_agent(**kwargs)
            saved = json.loads((stage / "input.json").read_text())
            assert [item["function"] for item in saved["old_module_sources"]] == ["caller", "seed", "z_useful"]
            assert result["patch_input_budget"]["final_chars"] == len((stage / "prompt.md").read_text())
            assert result["patch_input_budget"]["final_chars"] <= pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS
            assert result["source_payload_summary"]["source_item_count"] == 3
            assert "context_pruner" not in json.dumps(saved), "audit leaked into model input"
            assert json.loads((root / "patch_generation.json").read_text())["patch_input_budget"]["context_pruner"]["dropped_source_count"] == 1

            # Required source + fixed prompt cannot fit: provider is never invoked.
            client.call_json = lambda **unused: (_ for _ in ()).throw(AssertionError("over-budget LLM invocation"))
            pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS = 1
            result = pg.run_patch_generation_agent(**{**kwargs, "dry_run": False})
            assert result["parse_status"] == "input_budget_exceeded"
            assert result["patch_input_budget"]["status"] == "insufficient_budget"
        finally:
            pg.DEFAULT_PATCH_INPUT_BUDGET_CHARS = original_budget


def check_extraction_locus():
    with tempfile.TemporaryDirectory(prefix="context-pruner-locus-") as temporary:
        extracted = Path(temporary)
        path = extracted / "old_module_sources/drivers/demo/demo.c"
        path.parent.mkdir(parents=True)
        content = "int keep(void) { " + "keep(); " * 3500 + "return 0; }"
        path.write_text(content)
        required = [{"function": "keep_%02d" % i, "file": "drivers/demo/demo.c", "start_line": 1, "end_line": 1} for i in range(52)]
        kwargs = dict(extracted_dir=extracted, module_path="drivers/demo", impact_graph={"edit_required_functions": required}, with_line_numbers=False)
        with pruner_flag(True):
            blocks = pg._build_focused_old_sources(**kwargs)
            assert len(blocks) == 52 and all(item["content"] == content for item in blocks)
            assert pg._load_old_module_sources(extracted, "drivers/demo")[0]["content"] == content
        with pruner_flag(False):
            blocks = pg._build_focused_old_sources(**kwargs)
            assert len(blocks) == 48 and all("...[truncated]" in item["content"] for item in blocks)


def main():
    checks = [check_off_equivalence, check_native_symbols, check_ranked_budget,
              check_connected_expansion, check_generation, check_extraction_locus]
    for check in checks:
        check()
        print("PASS", check.__name__)
    print(json.dumps({"status": "passed", "checks": len(checks), "external_calls": 0}))


if __name__ == "__main__":
    main()
