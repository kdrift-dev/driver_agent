#!/usr/bin/env python3
"""Bounded offline checks of the opt-in character diagnostic. No model requests."""
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
import urllib.error

import run_paired_character_pilot as pilot


class FixtureProvider(pilot.DiagnosticProvider):
    def __init__(self, *, ledger):
        super().__init__(model=pilot.MODEL, api_key="offline-fixture-key", max_retries=0, max_tokens=pilot.MAX_TOKENS)
        self.ledger = ledger
        self.reply = None

    def _request_once(self, body, attempt):
        with self.ledger.open("a") as stream:
            stream.write(json.dumps({"body_sha256": pilot.digest(body)}) + "\n")
        if isinstance(self.reply, int):
            attempt["http_status"] = self.reply
            attempt["response"] = {"error": {"code": self.reply, "message": "fixture rejection"}}
            raise urllib.error.HTTPError("https://fixture.invalid", self.reply, "fixture", {}, io.BytesIO(b"{}"))
        attempt["http_status"] = 200
        return copy.deepcopy(self.reply)


class FixtureClient(pilot.LLMClient):
    def __init__(self, replies, ledger, after=None):
        super().__init__(pilot.LLMConfig(provider="openrouter", model=pilot.MODEL, enabled=False,
                                        max_retries=0, max_tokens=pilot.MAX_TOKENS))
        self._provider = FixtureProvider(ledger=ledger)
        self.replies, self.calls, self.after = replies, 0, after

    def call_json(self, **kwargs):
        assert self.calls < 2, "unexpected third LLM call"
        self._provider.reply = self.replies[self.calls]
        self.calls += 1
        result = super().call_json(**kwargs)
        if self.after:
            self.after(self.calls, kwargs)
        return result


def response(raw, *, finish="stop", model=pilot.MODEL, provider="Anthropic"):
    return {"id": "fixture-response", "model": model, "provider": provider,
            "choices": [{"finish_reason": finish, "native_finish_reason": "end_turn",
                         "message": {"role": "assistant", "content": raw}}],
            "usage": {"prompt_tokens": 7010, "completion_tokens": 101, "cost": 0.022545,
                      "prompt_tokens_details": {"cached_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 0}}}


def expect_error(function, exception=ValueError):
    try:
        function()
    except exception:
        return
    raise AssertionError("expected refusal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runs/paired_context_xilinx_chars_v1_20260914")
    args = parser.parse_args()
    raw = pilot.snapshot_prepared(args.prepared_dir)
    pair, summary, sources = pilot.reproduce(raw)
    assert pair["common_input_budget"] == 26532
    assert [pair["conditions"][p]["input_count"] for p in pilot.POLICIES] == [26316, 25936]
    assert all(pair["conditions"][p]["generation_ready"] is False for p in pilot.POLICIES)
    checks = 1
    source_path = sources[0]["path"]
    generated = f"--- a/{source_path}\n+++ b/{source_path}\n@@ -1,999 +1,999 @@\n-fixture old\n+fixture new\n"
    # Deliberately invalid hunk counts are preserved for the independent apply validator.
    valid = {"patch_candidates": [{"candidate_id": "one", "rationale": "fixture", "patch_diff": generated}],
             "module_level_notes": []}
    valid_raw = json.dumps(valid)
    empty_raw = json.dumps({"patch_candidates": [], "module_level_notes": []})
    identity = {"commit": "offline-fixture", "source_digest": "offline-fixture", "tracked_worktree_dirty": False}

    with tempfile.TemporaryDirectory(prefix="paired-character-checks-") as directory:
        root = Path(directory)

        def run(name, *, execute=True, prepared=None, factory=None):
            values = SimpleNamespace(prepared_dir=prepared or args.prepared_dir, out_dir=root / name,
                                     execute=execute, config_ini=root / "nonexistent-secret-file")
            with patch.object(pilot, "code_identity", return_value=copy.deepcopy(identity)), redirect_stdout(io.StringIO()):
                return pilot.run(values, client_factory=factory)

        def factory_for(name, replies, after=None):
            def factory(config_ini):
                freeze = root / name / "freeze"
                assert (freeze / "manifest.json").is_file()
                assert all((freeze / p / "request_body.json").is_file() for p in pilot.POLICIES)
                return FixtureClient(replies, root / f"{name}.ledger", after)
            return factory

        def ledger_count(name):
            path = root / f"{name}.ledger"
            return len(path.read_text().splitlines()) if path.exists() else 0

        with patch.object(pilot, "build_client", side_effect=AssertionError("dry run read credentials")):
            dry = run("dry", execute=False)
        assert dry["llm_calls"] == 0 and dry["status"] == "prepared_only"
        assert not (root / "dry" / "generation").exists()
        expect_error(lambda: run("dry", execute=False))
        checks += 1

        report = run("two", factory=factory_for("two", [response(valid_raw), response(empty_raw)]))
        assert report["llm_calls"] == 2 and report["explicit_http_attempts"] == 2 and ledger_count("two") == 2
        assert report["conditions"]["first_fit"]["candidate_admission"] == "admitted"
        assert report["conditions"]["obligation_ranked"]["candidate_admission"] == "no_candidate"
        assert report["conditions"]["first_fit"]["native_finish_reason"] == "end_turn"
        assert report["conditions"]["first_fit"]["usage"]["prompt_tokens"] == 7010
        assert (root / "two/generation/first_fit/generated.diff").read_text() == generated
        for p in pilot.POLICIES:
            stage = root / "two" / "generation" / p
            audit = json.loads((stage / "provider_audit.json").read_text())
            wire = json.loads((root / "two/freeze" / p / "request_body.json").read_text())
            assert audit["request_body"] == wire
            assert audit["configured_max_retries"] == 0 and audit["request_attempt_count"] == 1
            assert (stage / "dispatch_intent.json").is_file()
        assert (root / "two/progress_after_1.json").is_file()
        checks += 1

        for status in (401, 402):
            name = f"status{status}"
            report = run(name, factory=factory_for(name, [status, response(valid_raw)]))
            assert report["status"] == "stopped_auth_or_quota" and report["llm_calls"] == 1
            assert ledger_count(name) == 1
            assert not (root / name / "generation/obligation_ranked").exists()
        checks += 1

        copied = root / "prepared-copy"
        shutil.copytree(args.prepared_dir, copied)
        def mutate_after_first(ordinal, kwargs):
            if ordinal == 1:
                file = copied / "old_sources.json"
                file.write_bytes(file.read_bytes() + b" ")
        expect_error(lambda: run("mutation", prepared=copied, factory=factory_for(
            "mutation", [response(empty_raw), response(empty_raw)], mutate_after_first)))
        assert ledger_count("mutation") == 1
        aborted = json.loads((root / "mutation/summary.json").read_text())
        assert aborted["status"] == "aborted_without_retry" and aborted["llm_calls"] == 1
        assert aborted["explicit_http_attempts"] == 1
        expect_error(lambda: pilot.snapshot_prepared(copied))
        assert (root / "mutation/progress_after_1.json").is_file()
        checks += 1

        class InterruptedClient(FixtureClient):
            def call_json(self, **kwargs):
                raise KeyboardInterrupt("fixture interrupted before returned audit")
        expect_error(lambda: run("interrupt", factory=lambda config: InterruptedClient([], root / "interrupt.ledger")), KeyboardInterrupt)
        aborted = json.loads((root / "interrupt/summary.json").read_text())
        assert aborted["llm_calls"] == 1 and aborted["explicit_http_attempts"] is None
        assert aborted["explicit_http_attempts_exact"] is False
        assert (root / "interrupt/generation/first_fit/dispatch_intent.json").is_file()
        checks += 1

        def freeze_mutating_factory(config):
            file = root / "freeze-mutation/freeze/first_fit/request_body.json"
            file.write_text("{}")
            return FixtureClient([response(empty_raw)], root / "freeze-mutation.ledger")
        expect_error(lambda: run("freeze-mutation", factory=freeze_mutating_factory))
        assert ledger_count("freeze-mutation") == 0
        assert json.loads((root / "freeze-mutation/summary.json").read_text())["llm_calls"] == 0
        checks += 1

        # Success of the provider does not imply success of the strict JSON/schema gate.
        stage_audit = json.loads((root / "two/generation/first_fit/provider_audit.json").read_text())
        wire = stage_audit["request_body"]
        parsed = dict(valid, parse_status="ok")
        for malformed in ("```json\n" + valid_raw + "\n```", valid_raw + "\n{}", valid_raw[:-1] + ',"module_level_notes":[]}'):
            result, admitted = pilot.inspect_response(stage_audit, parsed, sources, wire, malformed)
            assert admitted is None and result["candidate_admission"] == "rejected"
        for value in (
            {"patch_candidates": valid["patch_candidates"] * 2, "module_level_notes": []},
            {"patch_candidates": [{**valid["patch_candidates"][0], "patch_diff": generated.replace(source_path, "drivers/other.c")}], "module_level_notes": []},
            {"patch_candidates": [{**valid["patch_candidates"][0], "patch_diff": "new file mode 100644\n" + generated}], "module_level_notes": []},
        ):
            result, admitted = pilot.inspect_response(stage_audit, dict(value, parse_status="ok"), sources, wire, json.dumps(value))
            assert admitted is None and result["candidate_admission"] == "rejected"
        for field, value in (("response_truncated", True),):
            altered = copy.deepcopy(stage_audit)
            altered[field] = value
            result, admitted = pilot.inspect_response(altered, parsed, sources, wire, valid_raw)
            assert admitted is None
        for field, value in (("returned_model", "wrong/model"), ("returned_provider", "Google"), ("finish_reason", "length")):
            altered = copy.deepcopy(stage_audit)
            altered["attempts"][-1][field] = value
            result, admitted = pilot.inspect_response(altered, parsed, sources, wire, valid_raw)
            assert admitted is None
        empty_patch = {"patch_candidates": [{**valid["patch_candidates"][0], "patch_diff": " "}], "module_level_notes": []}
        result, admitted = pilot.inspect_response(stage_audit, dict(empty_patch, parse_status="ok"), sources, wire, json.dumps(empty_patch))
        assert admitted is None and result["candidate_admission"] == "empty_patch"
        checks += 1

        for name in ("../escape", "/absolute", "a/../escape", "a\\escape", "a//b"):
            assert not pilot.safe_relative(name)
        clean = root / "prepared-extra"
        shutil.copytree(args.prepared_dir, clean)
        (clean / "unindexed.txt").write_text("extra")
        expect_error(lambda: pilot.snapshot_prepared(clean))
        (clean / "unindexed.txt").unlink()
        (clean / "summary.json").unlink()
        (clean / "summary.json").symlink_to(args.prepared_dir.resolve() / "summary.json")
        expect_error(lambda: pilot.snapshot_prepared(clean))
        checks += 1
    print(json.dumps({"status": "PASS", "check_groups": checks, "real_provider_requests": 0,
                      "fixture_scope": "canonical char replay; bounded audited fake transport; mutations; strict schema/scope"}))


if __name__ == "__main__":
    main()
