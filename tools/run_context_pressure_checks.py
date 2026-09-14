#!/usr/bin/env python3
"""Dependency-free contract checks; synthetic packs are temporary fixtures only."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile

import build_context_pressure_set as pressure
from build_context_pressure_set import (
    DEFAULT_BUDGETS, assign_split, build_artifacts, case_pressure, digest,
    resolve_forced_commits, verify_pressure_set, write_artifacts,
)
from ctxlab.dataset import load_case
from ctxlab.metrics import build_prompt
from ctxlab.provenance import case_index


class FixtureTokenizer:
    token_count_kind = "fixture_char_count_not_model_tokens"
    identity = {"class": "FixtureTokenizer", "purpose": "contract_checks_only"}

    def __init__(self):
        self.calls = []

    def count_tokens(self, text):
        self.calls.append(text)
        return len(text)


def write_fixture(root, case_id, commit, *, source="int f(void) { return old_api(); }", definitions=None):
    path = root / "cases" / case_id
    path.mkdir(parents=True)
    (path / "case.json").write_text(json.dumps({"case_id": case_id, "commit": commit, "kernel_symbols": ["new_api"]}))
    (path / "reference.diff").write_text("--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-old_api();\n+new_api();\n")
    evidence = definitions or [
        {"symbol": "first", "definition_diff": "+ void first(void);\n" * 5},
        {"symbol": "second", "definition_diff": "+ void second(void);\n" * 5},
    ]
    (path / "kernel_evidence.json").write_text(json.dumps({"gold": evidence[:1], "distractors": evidence[1:]}))
    sources = path / "old_module_sources"
    sources.mkdir()
    (sources / "x.c").write_text(source)
    return path


def assert_contract(artifacts):
    inventory = artifacts["inventory.json"]["cases"]
    split = artifacts["split.json"]
    by_commit = {}
    for case in inventory:
        by_commit.setdefault(case["commit"], set()).add(case["split"])
        if case["commit"] in split["forced_calibration_commits"]:
            assert case["split"] == "calibration"
        for row in case["budgets"]:
            if not row["eligible"]:
                assert row["exclusion_reasons"]
                continue
            assert not row["exclusion_reasons"]
            assert case["mandatory_prompt_tokens"] <= row["budget"] < case["full_evidence_prompt_tokens"]
            assert row["budget"] + case["reference_target_tokens"] <= split["config"]["window_tokens"]
            assert case["candidate_count"] >= 2 and row["individually_feasible_blocks"] >= 2
            assert row["feasible_union_prompt_tokens"] > row["budget"]
    assert all(len(values) == 1 for values in by_commit.values())
    for budget in split["config"]["budgets"]:
        calibration = artifacts[f"calibration_{budget}.json"]
        reserved = artifacts[f"reserved_{budget}.json"]
        assert not set(calibration["case_commits"].values()) & set(reserved["case_commits"].values())
        for manifest in (calibration, reserved):
            assert len(manifest["case_ids"]) == manifest["n_selected"]
            assert set(manifest["case_ids"]) == set(manifest["case_content_sha256"])


def check_pressure_contract():
    tokenizer = FixtureTokenizer()
    with tempfile.TemporaryDirectory(prefix="context-pressure-contract-") as tmp:
        path = write_fixture(Path(tmp), "one", "a" * 40)
        case = load_case(path)
        mandatory = len(build_prompt("", case["old_source"]))
        budget = mandatory + 160
        report = case_pressure(case, tokenizer, [budget], window=4096)
        assert report["budgets"][0]["eligible"], report
        window_failure = case_pressure(case, tokenizer, [budget], window=budget)
        assert "budget_plus_reference_target_exceeds_window" in window_failure["budgets"][0]["exclusion_reasons"]
        all_fit = case_pressure(case, tokenizer, [10000], window=20000)
        assert "full_evidence_prompt_fits_budget" in all_fit["budgets"][0]["exclusion_reasons"]
        # An infeasible giant block is not sufficient to make a pressure case.
        giant = deepcopy(case)
        giant["gold_items"] = [{"symbol": "huge", "definition_diff": "x" * 2000}]
        giant["distractor_items"] = [{"symbol": "a"}, {"symbol": "b"}]
        rejected = case_pressure(giant, tokenizer, [budget], window=4096)
        assert "overflow_only_from_infeasible_blocks" in rejected["budgets"][0]["exclusion_reasons"]
        assert rejected["budgets"][0]["oversized_block_count"] == 1
        assert rejected["budgets"][0]["individually_feasible_blocks"] == 2
        # Large mandatory source is explicitly unmeasured beyond this proof of ineligibility.
        tokenizer.calls.clear()
        huge_source = {**case, "old_source": "long_source " * 1000}
        rejected = case_pressure(huge_source, tokenizer, DEFAULT_BUDGETS)
        assert len(tokenizer.calls) == 2
        assert rejected["feasibility_status"] == "not_evaluated_mandatory_overflow"
        assert rejected["candidate_count"] == 2
        assert all(row["individually_feasible_blocks"] is None and not row["eligible"] for row in rejected["budgets"])


def check_split_and_integrity():
    with tempfile.TemporaryDirectory(prefix="context-pressure-integrity-") as tmp:
        root = Path(tmp)
        dataset = root / "dataset"
        first = write_fixture(dataset, "a", "1" * 40)
        write_fixture(dataset, "b_same_commit", "1" * 40)
        write_fixture(dataset, "c", "2" * 40)
        # A zero-gold pack with identical source/evidence remains eligible.
        zero = write_fixture(dataset, "d_zero_gold", "3" * 40)
        evidence = json.loads((zero / "kernel_evidence.json").read_text())
        (zero / "kernel_evidence.json").write_text(json.dumps({"gold": [], "distractors": evidence["gold"] + evidence["distractors"]}))
        historical = root / "historical.json"
        historical.write_text(json.dumps({"case_ids": ["a"]}))
        index = case_index(dataset)
        forced, origins = resolve_forced_commits(index, [historical])
        assert forced == {"1" * 40}
        tokenizer = FixtureTokenizer()
        budget = len(build_prompt("", load_case(first)["old_source"])) + 160
        kwargs = dict(budgets=[budget], window=4096, calibration_basis_points=0)
        artifacts = build_artifacts(index, tokenizer, forced, origins, **kwargs)
        repeated = build_artifacts(dict(reversed(list(index.items()))), FixtureTokenizer(), forced, origins, **kwargs)
        assert artifacts == repeated and digest(artifacts) == digest(repeated)
        assert_contract(artifacts)
        assert artifacts[f"calibration_{budget}.json"]["case_ids"] == ["a", "b_same_commit"]
        assert artifacts[f"reserved_{budget}.json"]["case_ids"] == ["c", "d_zero_gold"]
        assert assign_split("1" * 40, forced, seed=999, calibration_basis_points=0) == "calibration"
        # Metadata must agree with the commit split even if it changes during loading.
        original_loader = pressure.load_case
        def changing_loader(path):
            loaded = original_loader(path)
            loaded["meta"]["commit"] = "changed_after_assignment"
            return loaded
        pressure.load_case = changing_loader
        try:
            build_artifacts(index, FixtureTokenizer(), forced, origins, **kwargs)
        except ValueError as exc:
            assert "changed after split assignment" in str(exc)
        else:
            raise AssertionError("commit metadata race accepted")
        finally:
            pressure.load_case = original_loader
        try:
            write_artifacts(root / "changing_code", artifacts, {"unchanged": False})
        except ValueError as exc:
            assert "code identity changed" in str(exc)
        else:
            raise AssertionError("changing code was frozen")
        assert not (root / "changing_code").exists()
        # A structurally wrong split must fail even with recomputed artifact hashes.
        bad = deepcopy(artifacts)
        bad["split.json"]["case_assignments"]["a"]["split"] = "reserved"
        bad["inventory.json"]["cases"][0]["split"] = "reserved"
        for name, manifest in bad.items():
            if name.startswith(("calibration_", "reserved_")):
                manifest["split_digest"] = digest(bad["split.json"])
                manifest["inventory_digest"] = digest(bad["inventory.json"])
        write_artifacts(root / "wrong_split", bad)
        try:
            verify_pressure_set(root / "wrong_split" / f"reserved_{budget}.json", dataset)
        except ValueError as exc:
            assert "split recipe" in str(exc)
        else:
            raise AssertionError("wrong split recipe accepted")
        out = root / "frozen"
        write_artifacts(out, artifacts)
        verify_pressure_set(out / f"reserved_{budget}.json", dataset, budget=budget, split="reserved")
        try:
            write_artifacts(out, artifacts)
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing artifact directory was overwritten")
        # Any census case content change invalidates the manifest, even a non-selected case.
        source = first / "old_module_sources/x.c"
        original = source.read_bytes()
        source.write_bytes(original + b"\n/* changed */\n")
        try:
            verify_pressure_set(out / f"reserved_{budget}.json", dataset)
        except ValueError as exc:
            assert "content or census pool changed" in str(exc)
        else:
            raise AssertionError("modified source accepted")
        source.write_bytes(original)
        manifest = out / f"reserved_{budget}.json"
        manifest.write_text(manifest.read_text() + "\n")
        try:
            verify_pressure_set(manifest, dataset)
        except ValueError as exc:
            assert "artifact content changed" in str(exc)
        else:
            raise AssertionError("modified manifest accepted")


def check_commit_prefix_resolution():
    with tempfile.TemporaryDirectory(prefix="context-pressure-commit-") as tmp:
        root = Path(tmp)
        dataset = root / "dataset"
        write_fixture(dataset, "known", "1" * 40)
        write_fixture(dataset, "other", "2" * 40)
        manifest = root / "prior.json"
        manifest.write_text(json.dumps({"commits": ["1" * 12]}))
        forced, origins = resolve_forced_commits(case_index(dataset), [manifest])
        assert forced == {"1" * 40}
        assert origins[0]["commits"] == ["1" * 40]
        assert assign_split("1" * 40, forced, calibration_basis_points=0) == "calibration"
        write_fixture(dataset, "ambiguous", "1" * 12 + "3" * 28)
        try:
            resolve_forced_commits(case_index(dataset), [manifest])
        except ValueError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous commit prefix accepted")
        for unknown in ("9" * 12, "9" * 40, "not-a-commit"):
            manifest.write_text(json.dumps({"commits": [unknown]}))
            try:
                resolve_forced_commits(case_index(dataset), [manifest])
            except ValueError as exc:
                assert "unknown" in str(exc) or "invalid" in str(exc)
            else:
                raise AssertionError("unrecognized calibration commit accepted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", help="add contract/integrity checks against a real tokenizer census")
    parser.add_argument("--dataset-dir")
    args = parser.parse_args(argv)
    for check in (check_pressure_contract, check_split_and_integrity, check_commit_prefix_resolution):
        check()
        print("PASS", check.__name__)
    if args.artifact_dir:
        if not args.dataset_dir:
            parser.error("--dataset-dir is required with --artifact-dir")
        root = Path(args.artifact_dir)
        artifacts = {path.name: json.loads(path.read_text()) for path in root.glob("*.json") if path.name != "integrity.json"}
        assert_contract(artifacts)
        for name in sorted(artifacts):
            if name.startswith(("calibration_", "reserved_")):
                verify_pressure_set(root / name, args.dataset_dir)
        print("PASS real_census_contract_and_integrity")
    print(json.dumps({"status": "passed", "model_inference_calls": 0}))


if __name__ == "__main__":
    main()
