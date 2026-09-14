#!/usr/bin/env python3
"""Five bounded v3 runner checks using synthetic inputs and fake transport only."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import run_structured_edit_pilot as pilot
from driver_agent_pregen.paired_context import MessageCharCounter
from run_structured_edit_pilot_checks import (
    FixtureClient as BaseFixtureClient, FixtureProvider as BaseFixtureProvider,
    apply_and_verify, expect_error, response,
)
from run_substring_edit_pilot_checks import FixtureClient as V2FixtureClient

CONTRACT = "identity_substrings_v3"
PROTOCOL = "paired_identity_edit_character_diagnostic_v3"
RENDERER = "exact_original_substring_identity_edits_v3"


class FixtureProvider(BaseFixtureProvider):
    def __init__(self, ledger):
        super().__init__(ledger)
        self.edit_contract = CONTRACT


class FixtureClient(BaseFixtureClient):
    def __init__(self, replies, ledger, after=None):
        super().__init__(replies, ledger, after)
        self._provider = FixtureProvider(ledger)


def fixture_snapshot(root):
    root = Path(root)
    assert not root.is_symlink()
    return {p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()}


def fixture_reproduce(raw):
    sources = pilot.read_json(raw["old_sources.json"])
    pool = pilot.read_json(raw["evidence_pool.json"])
    pair = pilot.prepare_pair(sources, pool["obligation_signal"], pool["candidates"],
                              counter=MessageCharCounter(), model=pilot.MODEL, max_tokens=pilot.MAX_TOKENS)
    summary = {"old_ref": "a" * 40, "new_ref": "b" * 40,
               "module_path": "drivers/fixture", "kbuild_target": "fixture.ko"}
    return pair, summary, sources


def main():
    source_path = "drivers/fixture/fixture.c"
    sources = [{"path": source_path, "content": "/* Unrelated synthetic λ fixture. */\nint marker = 1;\nint stable = 7;\n"}]
    pool = {"obligation_signal": [], "candidates": [
        {"id": f"fixture-{i}", "symbols": [], "text": f"/* Neutral evidence {i}: " + chr(97 + i) * (600 + 10 * i) + " */"}
        for i in range(8)]}
    raw = {"old_sources.json": json.dumps(sources, ensure_ascii=False).encode("utf-8"),
           "evidence_pool.json": json.dumps(pool).encode("utf-8")}
    with patch.object(pilot, "reproduce", side_effect=fixture_reproduce):
        pair, _, admitted_sources = pilot.prepare(raw, CONTRACT)
        v1, _, _ = pilot.prepare(raw)
        v2, _, _ = pilot.prepare(raw, "exact_substrings_v2")
    assert admitted_sources == sources and pair["renderer_id"] == RENDERER
    assert pair["selection_pressure_eligible"] is True
    assert pair["common_input_budget"] == pair["mandatory_count"] + (pair["full_pool_count"] - pair["mandatory_count"] + 3) // 4
    assert pilot.contract_settings()["protocol"] == pilot.PROTOCOL
    assert pilot.contract_settings(CONTRACT)["protocol"] == PROTOCOL
    assert "Every edit must change text" not in pilot.IDENTITY_INSTRUCTIONS
    assert "is retained in the audit" in pilot.IDENTITY_INSTRUCTIONS
    assert "must not overlap any other edit" in pilot.IDENTITY_INSTRUCTIONS
    for key in ("source_sha256", "pool_sha256", "requirements_sha256"):
        assert pair[key] == v1[key] == v2[key]
    for condition in pair["conditions"].values():
        wire = condition["request_body"]
        assert condition["generation_ready"] is False
        assert condition["input_payload"]["old_module_sources"] == sources
        assert condition["input_count"] == len(pilot.canonical({"messages": wire["messages"], "response_format": wire["response_format"]}))
        assert condition["input_count"] <= condition["input_budget"] == pair["common_input_budget"]
        assert wire["response_format"] == {"type": "json_schema", "json_schema": {"name": RENDERER, "strict": True, "schema": pilot.RESPONSE_SCHEMA}}
        assert wire == FixtureProvider(Path("unused-fixture-ledger")).request_body(condition["prompt"], pilot.MAX_TOKENS)
    mixed = {"edits": [{"path": source_path, "old_text": "stable = 7", "new_text": "stable = 7"},
                       {"path": source_path, "old_text": "marker = 1", "new_text": "marker = 2"}],
             "rationale": "Exercise unrelated synthetic change and identity spans.", "notes": []}
    identity_only = {**mixed, "edits": [mixed["edits"][0]]}
    no_edits = {**mixed, "edits": []}
    mixed_raw, identity_raw, empty_raw = (json.dumps(value) for value in (mixed, identity_only, no_edits))
    expected_sources = [{"path": source_path, "content": "/* Unrelated synthetic λ fixture. */\nint marker = 2;\nint stable = 7;\n"}]
    code = {"commit": "synthetic-v3-fixture", "source_digest": "synthetic-v3-fixture", "tracked_worktree_dirty": False}

    with tempfile.TemporaryDirectory(prefix="identity-pilot-checks-") as directory:
        root = Path(directory)
        prepared = root / "prepared"
        prepared.mkdir()
        for name, data in raw.items():
            (prepared / name).write_bytes(data)

        def run(name, *, execute=True, factory=None):
            args = SimpleNamespace(prepared_dir=prepared, out_dir=root / name, execute=execute,
                                   config_ini=root / "no-credentials", edit_contract=CONTRACT)
            with patch.object(pilot, "snapshot_prepared", side_effect=fixture_snapshot), patch.object(pilot, "reproduce", side_effect=fixture_reproduce), patch.object(pilot, "code_identity", return_value=copy.deepcopy(code)), redirect_stdout(io.StringIO()):
                return pilot.run(args, client_factory=factory)

        def factory_for(name, replies, after=None):
            def factory(config):
                freeze = root / name / "freeze"
                manifest = json.loads((freeze / "manifest.json").read_text())
                assert manifest["protocol"] == PROTOCOL and manifest["edit_contract"] == CONTRACT
                assert manifest["common_character_cap"] == pair["common_input_budget"]
                assert manifest["call_limit"] == 2 and manifest["max_retries"] == 0
                assert manifest["generation_ready"] is False
                assert all((freeze / policy / "request_body.json").is_file() for policy in pilot.POLICIES)
                return FixtureClient(replies, root / f"{name}.ledger", after)
            return factory

        def ledger_count(name):
            ledger = root / f"{name}.ledger"
            return len(ledger.read_text().splitlines()) if ledger.exists() else 0

        with patch.object(pilot, "build_client", side_effect=AssertionError("dry run touched credentials")):
            dry = run("dry", execute=False)
        assert dry["status"] == "prepared_only" and dry["llm_calls"] == 0
        assert dry["protocol"] == PROTOCOL and not (root / "dry/generation").exists()
        expect_error(lambda: run("dry", execute=False))
        expect_error(lambda: run("wrong-v2-client", factory=lambda config: V2FixtureClient(
            [response(mixed_raw)], root / "wrong-v2-client.ledger")))
        assert ledger_count("wrong-v2-client") == 0
        assert json.loads((root / "wrong-v2-client/summary.json").read_text())["llm_calls"] == 0
        print("PASS synthetic complete-input/schema counts, v3 renderer/protocol freeze, unchanged default and pre-dispatch v2-provider rejection")

        report = run("mixed", factory=factory_for("mixed", [response(mixed_raw), response(mixed_raw)]))
        assert report["protocol"] == PROTOCOL and report["llm_calls"] == report["explicit_client_attempts"] == ledger_count("mixed") == 2
        patches = []
        for policy in pilot.POLICIES:
            stage = root / "mixed/generation" / policy
            compiled = json.loads((stage / "compiled_edits.json").read_text())
            audit = json.loads((stage / "provider_audit.json").read_text())
            frozen_wire = json.loads((root / "mixed/freeze" / policy / "request_body.json").read_text())
            assert audit["request_body"] == frozen_wire == json.loads((stage / "request_body.json").read_text())
            assert audit["request_attempt_count"] == 1 and audit["request_attempt_count_exact"] is True
            assert audit["configured_max_retries"] == 0
            assert audit["attempts"][0]["response"]["choices"][0]["message"]["content"] == mixed_raw
            assert (stage / "raw_output.txt").read_bytes() == mixed_raw.encode("utf-8")
            assert compiled["status"] == "compiled" and compiled["submitted_edits"] == mixed["edits"]
            assert (compiled["submitted_count"], compiled["changed_count"], compiled["identity_count"]) == (2, 1, 1)
            assert [a["submitted_index"] for a in compiled["edits_audit"]] == [1, 0]
            assert [a["effect"] for a in compiled["edits_audit"]] == ["changed", "identity"]
            assert compiled["patched_sources"] == expected_sources
            assert report["conditions"][policy]["candidate_admission"] == "admitted"
            patch_bytes = (stage / "generated.diff").read_bytes()
            assert patch_bytes == compiled["patch_diff"].encode("utf-8")
            assert pilot.sha_bytes(patch_bytes) == compiled["patch_sha256"]
            patches.append(patch_bytes)
            apply_and_verify(root / f"apply-{policy}", sources, compiled)
        assert patches[0] == patches[1]
        index = json.loads((root / "mixed/artifact_index.json").read_text())
        assert index["file_count"] == len(index["files"])
        assert all(pilot.sha_bytes((root / "mixed" / name).read_bytes()) == value for name, value in index["files"].items())
        print("PASS two audited fake mixed-edit calls, complete operation counts/index/effects, identical deterministic patches and actual byte-exact Git application")

        report = run("no-change", factory=factory_for("no-change", [response(identity_raw), response(empty_raw)]))
        for policy, status, count in (("first_fit", "identity_only", 1), ("obligation_ranked", "no_edits", 0)):
            stage = root / "no-change/generation" / policy
            compiled = json.loads((stage / "compiled_edits.json").read_text())
            assert compiled["status"] == report["conditions"][policy]["candidate_admission"] == status
            assert compiled["changed_count"] == 0 and compiled["submitted_count"] == compiled["identity_count"] == count
            assert len(compiled["submitted_edits"]) == len(compiled["edits_audit"]) == count
            assert compiled["patched_sources"] == sources and compiled["patch_diff"] == ""
            assert compiled["source_sha256"] == compiled["patched_source_sha256"]
            assert not (stage / "generated.diff").exists()
        assert ledger_count("no-change") == report["llm_calls"] == 2
        print("PASS identity-only and empty lists persist complete compiler evidence with no generated diff and no candidate admission")

        base = json.loads((root / "mixed/generation/first_fit/provider_audit.json").read_text())
        wire3 = pair["conditions"]["first_fit"]["request_body"]
        for old_contract, old_pair in (("whole_lines_v1", v1), ("exact_substrings_v2", v2)):
            old_wire = old_pair["conditions"]["first_fit"]["request_body"]
            changed = copy.deepcopy(base)
            changed["request_body"] = old_wire
            rejected, compiled = pilot.inspect(changed, mixed_raw, sources, old_wire, CONTRACT)
            assert compiled is None and "output_contract_does_not_match_request_schema" in rejected["rejection_reasons"]
            rejected, compiled = pilot.inspect(base, mixed_raw, sources, wire3, old_contract)
            assert compiled is None and "output_contract_does_not_match_request_schema" in rejected["rejection_reasons"]
            # Under the matching historical contract, the first submitted
            # identity remains an explicit refusal rather than being ignored.
            rejected, compiled = pilot.inspect(changed, mixed_raw, sources, old_wire, old_contract)
            assert compiled is None and rejected["rejection_reasons"] == ["invalid_structured_edits: no-op edit is forbidden"]
        full_line = {**mixed, "edits": [{"path": source_path, "old_text": "int marker = 1;\n", "new_text": "int marker = 2;\n"}]}
        full_raw = json.dumps(full_line)
        old_results = []
        for old_contract, old_pair in (("whole_lines_v1", v1), ("exact_substrings_v2", v2)):
            old_wire = old_pair["conditions"]["first_fit"]["request_body"]
            audit = copy.deepcopy(base)
            audit["request_body"] = old_wire
            audit["attempts"][0]["response"]["choices"][0]["message"]["content"] = full_raw
            result, compiled = pilot.inspect(audit, full_raw, sources, old_wire, old_contract)
            assert result["candidate_admission"] == "admitted"
            old_results.append(compiled)
        assert old_results[0] == old_results[1]
        for field, value in (("response_truncated", True), ("request_status", "failed")):
            changed = copy.deepcopy(base)
            changed[field] = value
            result, compiled = pilot.inspect(changed, mixed_raw, sources, wire3, CONTRACT)
            assert result["candidate_admission"] == "rejected" and compiled is None
        result, compiled = pilot.inspect(base, mixed_raw + " ", sources, wire3, CONTRACT)
        assert result["candidate_admission"] == "rejected" and compiled is None
        print("PASS bidirectional v1/v2/v3 schema binding, unchanged legacy admission/refusal parity, truncation and raw-response mismatch rejection")

        def mutate_freeze(ordinal, kwargs):
            if ordinal == 1:
                file = root / "freeze-mutation/freeze/obligation_ranked/request_body.json"
                file.write_bytes(file.read_bytes() + b" ")
        expect_error(lambda: run("freeze-mutation", factory=factory_for(
            "freeze-mutation", [response(mixed_raw), response(mixed_raw)], mutate_freeze)))
        assert ledger_count("freeze-mutation") == 1
        assert not (root / "freeze-mutation/generation/obligation_ranked").exists()
        assert (root / "freeze-mutation/generation/first_fit/provider_audit.json").is_file()
        def mutate_source(ordinal, kwargs):
            if ordinal == 1:
                file = prepared / "old_sources.json"
                file.write_bytes(file.read_bytes() + b" ")
        expect_error(lambda: run("source-mutation", factory=factory_for(
            "source-mutation", [response(mixed_raw), response(mixed_raw)], mutate_source)))
        assert ledger_count("source-mutation") == 1
        aborted = json.loads((root / "source-mutation/summary.json").read_text())
        assert aborted["status"] == "aborted_without_retry" and aborted["llm_calls"] == 1
        assert not (root / "source-mutation/generation/obligation_ranked").exists()
        (prepared / "old_sources.json").write_bytes(raw["old_sources.json"])
        assert fixture_snapshot(prepared) == raw
        print("PASS frozen-request and original-source mutation stop before call two, preserving first-call audits and explicit abort summaries")
    print(json.dumps({"status": "PASS", "check_groups": 5, "real_provider_requests": 0,
                      "fixture_scope": "synthetic sources/evidence only; real renderer/freeze/audit/compiler with fake transport"}))


if __name__ == "__main__":
    main()
