#!/usr/bin/env python3
"""Five bounded offline checks of frozen structured-edit generation; no API calls."""
from __future__ import annotations

import argparse
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

import run_structured_edit_pilot as pilot


class FixtureProvider(pilot.StructuredProvider):
    def __init__(self, ledger):
        super().__init__(model=pilot.MODEL, api_key="offline-fixture-key", max_retries=0,
                         max_tokens=pilot.MAX_TOKENS)
        self.ledger, self.reply = ledger, None

    def _request_once(self, body, attempt):
        with self.ledger.open("a") as stream:
            stream.write(json.dumps({"body_sha256": pilot.digest(body)}) + "\n")
        if type(self.reply) is int:
            attempt["http_status"] = self.reply
            attempt["response"] = {"error": {"code": self.reply, "message": "fixture rejection"}}
            raise urllib.error.HTTPError("https://fixture.invalid", self.reply, "fixture", {}, io.BytesIO(b"{}"))
        attempt["http_status"] = 200
        return copy.deepcopy(self.reply)


class FixtureClient(pilot.LLMClient):
    def __init__(self, replies, ledger, after=None):
        super().__init__(pilot.LLMConfig(provider="openrouter", model=pilot.MODEL, enabled=False,
                                        max_retries=0, max_tokens=pilot.MAX_TOKENS))
        self._provider = FixtureProvider(ledger)
        self.replies, self.calls, self.after = replies, 0, after

    def call_json(self, **kwargs):
        assert self.calls < 2, "unexpected third generation call"
        self._provider.reply = self.replies[self.calls]
        self.calls += 1
        result = super().call_json(**kwargs)
        if self.after:
            self.after(self.calls, kwargs)
        return result


def response(raw):
    return {"id": "offline-fixture-response", "model": pilot.MODEL, "provider": "Anthropic",
            "choices": [{"finish_reason": "stop", "native_finish_reason": "end_turn",
                         "message": {"role": "assistant", "content": raw}}],
            "usage": {"prompt_tokens": 7000, "completion_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 0}}}


def expect_error(function, exception=ValueError):
    try:
        function()
    except exception:
        return
    raise AssertionError("expected explicit refusal")


def apply_and_verify(root, sources, compiled):
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    for source in sources:
        file = root / source["path"]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(source["content"].encode("utf-8"))
    raw = compiled["patch_diff"].encode("utf-8")
    for args in (["--check"], []):
        subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", *args, "-"],
                       input=raw, cwd=root, check=True, capture_output=True, env=env)
    for source in compiled["patched_sources"]:
        assert (root / source["path"]).read_bytes() == source["content"].encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runs/paired_context_xilinx_chars_v1_20260914")
    args = parser.parse_args()
    raw = pilot.snapshot_prepared(args.prepared_dir)
    frozen_raw = copy.deepcopy(raw)
    previous, original_summary, original_sources = pilot.reproduce(raw)
    pair, summary, sources = pilot.prepare(raw)
    assert raw == frozen_raw and summary == original_summary and sources == original_sources
    for key in ("source_sha256", "pool_sha256", "requirements_sha256"):
        assert pair[key] == previous[key]
    assert pair["candidate_count"] == previous["candidate_count"] == 62
    assert pair["selection_pressure_eligible"] is True
    assert pair["common_input_budget"] == pair["mandatory_count"] + (pair["full_pool_count"] - pair["mandatory_count"] + 3) // 4
    for condition in pair["conditions"].values():
        wire = condition["request_body"]
        assert condition["generation_ready"] is False
        assert pilot.StructuredInputCounter().dispatch_compatible(wire) is False
        assert condition["input_budget"] == pair["common_input_budget"]
        assert condition["input_count"] <= pair["common_input_budget"]
        assert condition["input_count"] == len(pilot.canonical({"messages": wire["messages"], "response_format": wire["response_format"]}))
        assert condition["input_count"] > len(pilot.canonical(wire["messages"]))
        assert condition["input_payload"]["old_module_sources"] == sources
        assert condition["input_payload"]["migration_requirements"] == previous["conditions"]["first_fit"]["input_payload"]["migration_requirements"]
        assert wire["response_format"]["json_schema"]["strict"] is True
        assert wire["response_format"]["json_schema"]["schema"] == pilot.RESPONSE_SCHEMA
        assert wire == FixtureProvider(Path("unused-offline-ledger")).request_body(condition["prompt"], pilot.MAX_TOKENS)

    # Only a neutral synthetic comment is added. No historical responses,
    # reference patch or migration-specific replacement text are consulted.
    source = sources[0]
    first_line = source["content"].split("\n", 1)[0] + "\n"
    assert source["content"].startswith(first_line) and source["content"].count(first_line) == 1
    assert first_line.startswith("//")
    value = {"edits": [{"path": source["path"], "old_text": first_line,
                         "new_text": first_line + "/* Offline synthetic exact-edit fixture. */\n"}],
             "rationale": "Exercise exact-edit transport with an unrelated synthetic comment.", "notes": []}
    raw_output = json.dumps(value)
    expected = pilot.compile_edits(sources, value)
    identity = {"commit": "offline-fixture", "source_digest": "offline-fixture", "tracked_worktree_dirty": False}

    with tempfile.TemporaryDirectory(prefix="structured-edit-pilot-checks-") as directory:
        root = Path(directory)

        def run(name, *, execute=True, prepared=None, factory=None):
            values = SimpleNamespace(prepared_dir=prepared or args.prepared_dir, out_dir=root / name,
                                     execute=execute, config_ini=root / "nonexistent-secret-file")
            with patch.object(pilot, "code_identity", return_value=copy.deepcopy(identity)), redirect_stdout(io.StringIO()):
                return pilot.run(values, client_factory=factory)

        def factory_for(name, replies, after=None):
            def factory(config):
                freeze = root / name / "freeze"
                manifest = json.loads((freeze / "manifest.json").read_text())
                assert manifest["call_limit"] == 2 and manifest["max_retries"] == 0
                assert manifest["generation_ready"] is False and manifest["actual_token_budget_comparison"] is False
                assert json.loads((freeze / "response_schema.json").read_text()) == pilot.RESPONSE_SCHEMA
                assert all((freeze / policy / "request_body.json").is_file() for policy in pilot.POLICIES)
                return FixtureClient(replies, root / f"{name}.ledger", after)
            return factory

        def ledger_count(name):
            ledger = root / f"{name}.ledger"
            return len(ledger.read_text().splitlines()) if ledger.exists() else 0

        with patch.object(pilot, "build_client", side_effect=AssertionError("dry run touched credentials")):
            dry = run("dry", execute=False)
        assert dry["status"] == "prepared_only" and dry["llm_calls"] == 0
        assert not (root / "dry/generation").exists()
        expect_error(lambda: run("dry", execute=False))
        print("PASS canonical source/pool/requirement replay, complete message-plus-schema cap, unverified token binding and zero-call preparation")

        report = run("two", factory=factory_for("two", [response(raw_output), response(raw_output)]))
        assert report["llm_calls"] == report["explicit_client_attempts"] == ledger_count("two") == 2
        assert report["explicit_client_attempts_exact"] is True
        for policy in pilot.POLICIES:
            stage = root / "two/generation" / policy
            audit = json.loads((stage / "provider_audit.json").read_text())
            wire = json.loads((root / "two/freeze" / policy / "request_body.json").read_text())
            compiled = json.loads((stage / "compiled_edits.json").read_text())
            assert compiled == expected
            assert audit["request_body"] == wire == json.loads((stage / "request_body.json").read_text())
            assert audit["configured_max_retries"] == 0 and audit["request_attempt_count"] == 1
            assert audit["request_attempt_count_exact"] is True
            assert wire["response_format"]["json_schema"]["schema"] == pilot.RESPONSE_SCHEMA
            assert (stage / "generated.diff").read_bytes() == expected["patch_diff"].encode("utf-8")
            assert report["conditions"][policy]["candidate_admission"] == "admitted"
            assert report["conditions"][policy]["generation_ready"] is False
            assert (stage / "dispatch_intent.json").is_file()
            apply_and_verify(root / f"apply-{policy}", sources, compiled)
        assert (root / "two/progress_after_1.json").is_file()
        index = json.loads((root / "two/artifact_index.json").read_text())
        assert index["file_count"] == len(index["files"])
        assert all(pilot.sha_bytes((root / "two" / name).read_bytes()) == digest for name, digest in index["files"].items())
        print("PASS two audited fake requests match frozen schema/body, produce identical deterministic diffs and apply to original source bytes")

        base = json.loads((root / "two/generation/first_fit/provider_audit.json").read_text())
        wire = base["request_body"]
        variants = []
        for key, value in (("request_status", "failed"), ("response_truncated", True),
                           ("request_attempt_count", 2), ("request_attempt_count_exact", False)):
            altered = copy.deepcopy(base)
            altered[key] = value
            variants.append((altered, raw_output))
        for key, value in (("returned_model", "wrong/model"), ("returned_provider", "Other"), ("finish_reason", "length")):
            altered = copy.deepcopy(base)
            altered["attempts"][-1][key] = value
            variants.append((altered, raw_output))
        for key, value in (("refusal", "fixture refusal"), ("role", "tool"), ("tool_calls", [{"id": "fixture-tool"}])):
            altered = copy.deepcopy(base)
            altered["attempts"][-1]["response"]["choices"][0]["message"][key] = value
            variants.append((altered, raw_output))
        altered = copy.deepcopy(base)
        altered["attempts"][-1]["response"]["choices"][0]["native_finish_reason"] = "max_tokens"
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
            altered["attempts"][-1]["response"]["choices"][0]["message"]["content"] = text
            variants.append((altered, text))
        for altered, text in variants:
            result, compiled = pilot.inspect(altered, text, sources, wire)
            assert result["candidate_admission"] == "rejected" and compiled is None
            assert result["rejection_reasons"]
        quota = run("quota", factory=factory_for("quota", [402, response(raw_output)]))
        assert quota["status"] == "stopped_auth_or_quota" and quota["llm_calls"] == ledger_count("quota") == 1
        assert not (root / "quota/generation/obligation_ranked").exists()
        print("PASS request failure, truncation, refusal, provider/finish mismatch, malformed strict JSON, raw-response mismatch and quota stop")

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
        assert (root / "freeze-mutation/generation/first_fit/provider_audit.json").is_file()
        print("PASS original-artifact and second-request freeze mutation stop before a second call and preserve first-call evidence")

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
        assert (stage / "raw_output.txt").read_text() == raw_output
        assert not (root / "interrupt/generation/obligation_ranked").exists()
        expect_error(lambda: run("interrupt", execute=False))
        assert pilot.snapshot_prepared(args.prepared_dir) == frozen_raw
        print("PASS interrupted audited call retains raw response and intent, marks aggregate count uncertain, and cannot resume or resubmit")
    print(json.dumps({"status": "PASS", "check_groups": 5, "real_provider_requests": 0,
                      "fixture_scope": "canonical preparation; neutral synthetic comment edit; bounded audited fake transport"}))


if __name__ == "__main__":
    main()
