#!/usr/bin/env python3
"""Six bounded v2 fixture groups: exact substring edits and zero real requests."""
from __future__ import annotations

import argparse
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import run_structured_edit_pilot as pilot
from run_structured_edit_pilot_checks import (
    FixtureClient as OriginalFixtureClient,
    FixtureProvider as OriginalFixtureProvider,
    apply_and_verify, expect_error, response,
)

CONTRACT = "exact_substrings_v2"
PROTOCOL = "paired_substring_edit_character_diagnostic_v2"
RENDERER = "exact_original_substring_edits_v2"


class FixtureProvider(OriginalFixtureProvider):
    def __init__(self, ledger):
        super().__init__(ledger)
        self.edit_contract = CONTRACT


class FixtureClient(OriginalFixtureClient):
    def __init__(self, replies, ledger, after=None):
        super().__init__(replies, ledger, after)
        self._provider = FixtureProvider(ledger)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runs/paired_context_xilinx_chars_v1_20260914")
    args = parser.parse_args()
    raw = pilot.snapshot_prepared(args.prepared_dir)
    unchanged_raw = copy.deepcopy(raw)
    previous, original_summary, original_sources = pilot.reproduce(raw)
    pair, summary, sources = pilot.prepare(raw, edit_contract=CONTRACT)
    v1, _, _ = pilot.prepare(raw)
    assert raw == unchanged_raw and summary == original_summary and sources == original_sources
    assert pair["renderer_id"] == RENDERER and v1["renderer_id"] == pilot.RENDERER != RENDERER
    for key in ("source_sha256", "pool_sha256", "requirements_sha256"):
        assert pair[key] == previous[key] == v1[key]
    assert pair["candidate_count"] == previous["candidate_count"] == 62
    assert pair["selection_pressure_eligible"] is True
    pool = pilot.read_json(raw["evidence_pool.json"])
    requirements = previous["conditions"]["first_fit"]["input_payload"]["migration_requirements"]
    by_id = {c["id"]: c for c in pool["candidates"]}
    mandatory_wire = pilot.render(sources, requirements, [], model=pilot.MODEL, max_tokens=pilot.MAX_TOKENS, edit_contract=CONTRACT)[2]
    full_wire = pilot.render(sources, requirements, pool["candidates"], model=pilot.MODEL, max_tokens=pilot.MAX_TOKENS, edit_contract=CONTRACT)[2]
    count = lambda wire: len(pilot.canonical({"messages": wire["messages"], "response_format": wire["response_format"]}))
    assert pair["mandatory_count"] == count(mandatory_wire)
    assert pair["full_pool_count"] == count(full_wire)
    assert pair["common_input_budget"] == count(mandatory_wire) + (count(full_wire) - count(mandatory_wire) + 3) // 4
    for condition in pair["conditions"].values():
        wire = condition["request_body"]
        assert condition["generation_ready"] is False and condition["dispatch_refusal"] == "counter_provider_binding_unverified"
        assert pilot.StructuredInputCounter().dispatch_compatible(wire) is False
        assert condition["input_budget"] == pair["common_input_budget"]
        assert condition["input_count"] == count(wire) <= pair["common_input_budget"]
        assert count(wire) > len(pilot.canonical(wire["messages"]))
        payload = condition["input_payload"]
        assert set(payload) == {"old_module_sources", "migration_requirements", "kernel_evidence"}
        assert payload["old_module_sources"] == sources and payload["migration_requirements"] == requirements
        assert payload["kernel_evidence"] == [{"id": i, "content": by_id[i]["text"]} for i in condition["selected_ids"]]
        assert wire["response_format"] == {"type": "json_schema", "json_schema": {"name": RENDERER, "strict": True, "schema": pilot.RESPONSE_SCHEMA}}
        assert wire["provider"] == {"only": ["anthropic"], "order": ["anthropic"], "allow_fallbacks": False, "require_parameters": True}
        assert wire["reasoning"] == {"enabled": False}
        assert wire["plugins"] == [{"id": "context-compression", "enabled": False}]
        assert wire == FixtureProvider(Path("unused-offline-ledger")).request_body(condition["prompt"], pilot.MAX_TOKENS)
    expect_error(lambda: pilot.prepare(raw, edit_contract="unknown_contract"))

    # An interior substring of a source comment exercises v2 without consulting
    # any earlier generated response or migration-specific replacement.
    source = sources[0]
    old = "SPDX-License-Identifier"
    new = "SPDX-License-Identifier (offline synthetic β fixture)"
    assert source["content"].count(old) == 1
    start = source["content"].index(old)
    end = start + len(old)
    assert source["content"][start - 1] != "\n" and source["content"][end] != "\n"
    value = {"edits": [{"path": source["path"], "old_text": old, "new_text": new}],
             "rationale": "Unrelated synthetic comment substring exercises exact-byte transport.", "notes": []}
    raw_output = json.dumps(value, ensure_ascii=False)
    expected_sources = copy.deepcopy(sources)
    expected_sources[0]["content"] = source["content"][:start] + new + source["content"][end:]
    expected_sources.sort(key=lambda item: item["path"])
    identity = {"commit": "offline-fixture", "source_digest": "offline-fixture", "tracked_worktree_dirty": False}

    with tempfile.TemporaryDirectory(prefix="substring-edit-pilot-checks-") as directory:
        root = Path(directory)

        def run(name, *, execute=True, prepared=None, factory=None, contract=CONTRACT):
            values = SimpleNamespace(prepared_dir=prepared or args.prepared_dir, out_dir=root / name,
                                     execute=execute, config_ini=root / "nonexistent-secret-file", edit_contract=contract)
            with patch.object(pilot, "code_identity", return_value=copy.deepcopy(identity)), redirect_stdout(io.StringIO()):
                return pilot.run(values, client_factory=factory)

        def factory_for(name, replies, after=None):
            def factory(config):
                freeze = root / name / "freeze"
                manifest = json.loads((freeze / "manifest.json").read_text())
                assert manifest["protocol"] == PROTOCOL and manifest["edit_contract"] == CONTRACT
                assert manifest["call_limit"] == 2 and manifest["max_retries"] == 0
                assert manifest["generation_ready"] is False and manifest["actual_token_budget_comparison"] is False
                assert manifest["common_character_cap"] == pair["common_input_budget"]
                assert json.loads((freeze / "response_schema.json").read_text()) == pilot.RESPONSE_SCHEMA
                assert all((freeze / policy / "request_body.json").is_file() for policy in pilot.POLICIES)
                return FixtureClient(replies, root / f"{name}.ledger", after)
            return factory

        def ledger_count(name):
            ledger = root / f"{name}.ledger"
            return len(ledger.read_text().splitlines()) if ledger.exists() else 0

        with patch.object(pilot, "build_client", side_effect=AssertionError("dry run touched credentials")):
            dry = run("dry", execute=False)
        assert dry["status"] == "prepared_only" and dry["llm_calls"] == 0 and dry["protocol"] == PROTOCOL
        assert not (root / "dry/generation").exists()
        expect_error(lambda: run("dry", execute=False))
        expect_error(lambda: run("wrong-contract", factory=lambda config: OriginalFixtureClient(
            [response(raw_output)], root / "wrong-contract.ledger")))
        assert ledger_count("wrong-contract") == 0
        print("PASS v2-only contract binding, unchanged source/pool/requirements, messages/schema cap and credential-free preparation")

        report = run("two", factory=factory_for("two", [response(raw_output), response(raw_output)]))
        assert report["llm_calls"] == report["explicit_client_attempts"] == ledger_count("two") == 2
        assert report["explicit_client_attempts_exact"] is True and report["protocol"] == PROTOCOL
        patches = []
        for policy in pilot.POLICIES:
            stage = root / "two/generation" / policy
            audit = json.loads((stage / "provider_audit.json").read_text())
            wire = json.loads((root / "two/freeze" / policy / "request_body.json").read_text())
            compiled = json.loads((stage / "compiled_edits.json").read_text())
            assert compiled["patched_sources"] == expected_sources
            assert compiled["source_sha256"] == pilot.digest(sorted(sources, key=lambda item: item["path"]))
            assert compiled["patched_source_sha256"] == pilot.digest(expected_sources)
            assert audit["request_body"] == wire == json.loads((stage / "request_body.json").read_text())
            assert audit["configured_max_retries"] == 0 and audit["request_attempt_count"] == 1
            assert audit["request_attempt_count_exact"] is True
            assert audit["attempts"][0]["response"]["choices"][0]["message"]["content"] == raw_output
            assert (stage / "raw_output.txt").read_bytes() == raw_output.encode()
            patch_bytes = (stage / "generated.diff").read_bytes()
            assert patch_bytes == compiled["patch_diff"].encode()
            assert pilot.sha_bytes(patch_bytes) == compiled["patch_sha256"]
            patches.append(patch_bytes)
            assert report["conditions"][policy]["candidate_admission"] == "admitted"
            assert report["conditions"][policy]["generation_ready"] is False
            assert (stage / "dispatch_intent.json").is_file()
            # The source expectation above is computed with direct slicing,
            # independently of both the compiler and its generated patch.
            apply_and_verify(root / f"apply-{policy}", sources, compiled)
        assert patches[0] == patches[1]
        assert (root / "two/progress_after_1.json").is_file()
        index = json.loads((root / "two/artifact_index.json").read_text())
        assert index["file_count"] == len(index["files"])
        assert all(pilot.sha_bytes((root / "two" / name).read_bytes()) == expected for name, expected in index["files"].items())
        print("PASS two audited fake calls, raw Unicode JSON binding, independent original-source substring result and actual Git application")

        base = json.loads((root / "two/generation/first_fit/provider_audit.json").read_text())
        wire = base["request_body"]

        def inspect(altered, text):
            return pilot.inspect(altered, text, sources, wire, edit_contract=CONTRACT)

        # Same response remains invalid under the unchanged v1 compiler.
        old_wire = v1["conditions"]["first_fit"]["request_body"]
        old_audit = copy.deepcopy(base)
        old_audit["request_body"] = old_wire
        old_result, old_compiled = pilot.inspect(old_audit, raw_output, sources, old_wire)
        assert old_result["candidate_admission"] == "rejected" and old_compiled is None
        assert old_result["rejection_reasons"] == ["invalid_structured_edits: old_text must cover complete original lines"]
        no_op = copy.deepcopy(value)
        no_op["edits"][0]["new_text"] = old
        empty_old = copy.deepcopy(value)
        empty_old["edits"][0]["old_text"] = ""
        extra_field = copy.deepcopy(value)
        extra_field["edits"][0]["occurrence"] = 1
        for invalid in (no_op, empty_old, extra_field):
            text = json.dumps(invalid)
            altered = copy.deepcopy(base)
            altered["attempts"][0]["response"]["choices"][0]["message"]["content"] = text
            result, compiled = inspect(altered, text)
            assert result["candidate_admission"] == "rejected" and compiled is None and result["rejection_reasons"]
        print("PASS v1 whole-line contract remains strict; v2 no-op, empty original span and undeclared output fields are rejected")

        variants = []
        for key, val in (("request_status", "failed"), ("response_truncated", True),
                         ("request_attempt_count", 2), ("request_attempt_count_exact", False)):
            altered = copy.deepcopy(base)
            altered[key] = val
            variants.append((altered, raw_output))
        for key, val in (("returned_model", "wrong/model"), ("returned_provider", "Other"), ("finish_reason", "length")):
            altered = copy.deepcopy(base)
            altered["attempts"][0][key] = val
            variants.append((altered, raw_output))
        for key, val in (("refusal", "fixture refusal"), ("role", "tool"), ("tool_calls", [{"id": "fixture-tool"}])):
            altered = copy.deepcopy(base)
            altered["attempts"][0]["response"]["choices"][0]["message"][key] = val
            variants.append((altered, raw_output))
        altered = copy.deepcopy(base)
        altered["attempts"][0]["response"]["choices"][0]["native_finish_reason"] = "max_tokens"
        variants.append((altered, raw_output))
        altered = copy.deepcopy(base)
        altered["request_body"]["response_format"]["json_schema"]["strict"] = False
        variants.append((altered, raw_output))
        variants.append((copy.deepcopy(base), raw_output + " "))
        malformed = ["```json\n" + raw_output + "\n```", raw_output + "\n{}",
                     '{"edits":[],"rationale":"x","notes":[],"notes":[]}',
                     '{"edits":[],"rationale":"x","notes":[],"unexpected":true}']
        for text in malformed:
            altered = copy.deepcopy(base)
            altered["attempts"][0]["response"]["choices"][0]["message"]["content"] = text
            variants.append((altered, text))
        for altered, text in variants:
            result, compiled = inspect(altered, text)
            assert result["candidate_admission"] == "rejected" and compiled is None and result["rejection_reasons"]
        quota = run("quota", factory=factory_for("quota", [402, response(raw_output)]))
        assert quota["status"] == "stopped_auth_or_quota" and quota["llm_calls"] == ledger_count("quota") == 1
        assert not (root / "quota/generation/obligation_ranked").exists()
        print("PASS refusal, truncation, mismatch, malformed sole JSON, changed schema/raw output and quota stop")

        prepared = root / "prepared-copy"
        shutil.copytree(args.prepared_dir, prepared)
        def mutate_source(ordinal, kwargs):
            if ordinal == 1:
                file = prepared / "old_sources.json"
                file.write_bytes(file.read_bytes() + b" ")
        expect_error(lambda: run("source-mutation", prepared=prepared, factory=factory_for(
            "source-mutation", [response(raw_output), response(raw_output)], mutate_source)))
        assert ledger_count("source-mutation") == 1
        aborted = json.loads((root / "source-mutation/summary.json").read_text())
        assert aborted["status"] == "aborted_without_retry" and aborted["llm_calls"] == 1
        assert aborted["explicit_client_attempts"] == 1
        assert (root / "source-mutation/progress_after_1.json").is_file()
        def mutate_freeze(ordinal, kwargs):
            if ordinal == 1:
                file = root / "freeze-mutation/freeze/obligation_ranked/request_body.json"
                file.write_bytes(file.read_bytes() + b" ")
        expect_error(lambda: run("freeze-mutation", factory=factory_for(
            "freeze-mutation", [response(raw_output), response(raw_output)], mutate_freeze)))
        assert ledger_count("freeze-mutation") == 1
        assert not (root / "freeze-mutation/generation/obligation_ranked").exists()
        print("PASS original-input and frozen-request mutation preserve first-call evidence and prevent the second dispatch")

        class InterruptedClient(FixtureClient):
            def call_json(self, **kwargs):
                super().call_json(**kwargs)
                raise KeyboardInterrupt("fixture interruption after audited response")
        expect_error(lambda: run("interrupt", factory=lambda config: InterruptedClient(
            [response(raw_output)], root / "interrupt.ledger")), KeyboardInterrupt)
        aborted = json.loads((root / "interrupt/summary.json").read_text())
        assert aborted["status"] == "aborted_without_retry" and aborted["llm_calls"] == ledger_count("interrupt") == 1
        assert aborted["explicit_client_attempts"] is None and aborted["explicit_client_attempts_exact"] is False
        stage = root / "interrupt/generation/first_fit"
        assert (stage / "dispatch_intent.json").is_file() and (stage / "provider_audit.json").is_file()
        assert (stage / "raw_output.txt").read_bytes() == raw_output.encode()
        assert not (root / "interrupt/generation/obligation_ranked").exists()
        expect_error(lambda: run("interrupt", execute=False))
        assert pilot.snapshot_prepared(args.prepared_dir) == unchanged_raw
        print("PASS interrupted response is retained with uncertain aggregate count; existing outputs cannot be resumed or retried")
    print(json.dumps({"status": "PASS", "check_groups": 6, "real_provider_requests": 0,
                      "fixture_scope": "canonical preparation; neutral comment substring; independent source slicing and actual Git application"}))


if __name__ == "__main__":
    main()
