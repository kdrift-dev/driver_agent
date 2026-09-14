#!/usr/bin/env python3
"""Offline LLM audit/retry checks. Uses fake transports and dummy credentials only."""
from __future__ import annotations

import io
import json
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver_agent_pregen.llm_client import LLMClient, LLMConfig, OpenAIProvider, OpenRouterProvider, build_chat_request


DUMMY_KEY = "fixture-credential-do-not-persist"


def response(content='{"patches": []}', finish_reason="stop"):
    return {
        "id": "fixture-response", "model": "returned-model-version", "provider": "fixture-upstream",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 19, "completion_tokens": 7, "total_tokens": 26},
        "extra_provider_field": {"preserved": True},
    }


class FakeHTTPResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self):
        return self.payload.encode("utf-8")


class FakeHTTP:
    def __init__(self, outcomes, wire_path=None):
        self.calls = mp.get_context("fork").Value("i", 0)
        self.outcomes = outcomes
        self.wire_path = wire_path

    def __call__(self, request, *, timeout):
        with self.calls.get_lock():
            index = self.calls.value
            self.calls.value += 1
        assert request.get_header("Authorization") == "Bearer " + DUMMY_KEY
        if self.wire_path:
            Path(self.wire_path).write_bytes(request.data)
        outcome = self.outcomes[min(index, len(self.outcomes) - 1)]
        if outcome == "hang":
            time.sleep(10)
            return FakeHTTPResponse(json.dumps(response()))
        if outcome == "crash":
            import os
            os._exit(7)
        if outcome == "http_error":
            body = {"error": {"message": "authorization failed: " + DUMMY_KEY}, "headers": {"Authorization": "Bearer " + DUMMY_KEY}}
            raise urllib.error.HTTPError("https://fixture.invalid", 401, "secret " + DUMMY_KEY, {}, io.BytesIO(json.dumps(body).encode()))
        return FakeHTTPResponse(outcome if isinstance(outcome, str) else json.dumps(outcome))


def client_for(provider="openrouter", retries=0):
    client = LLMClient(LLMConfig(enabled=False, provider=provider, model="fixture-model", max_retries=retries, max_tokens=123))
    client._provider = OpenRouterProvider(model="fixture-model", api_key=DUMMY_KEY, base_url="https://fixture.invalid", max_retries=retries, max_tokens=123)
    client._provider.retry_backoff_sec = 0
    return client


def invoke(client, stage, *, dry_run=False, deadline=3):
    original = client._complete_with_hard_timeout
    with patch.object(client, "_complete_with_hard_timeout", lambda prompt, **kw: original(prompt, timeout_sec=deadline, max_tokens=kw.get("max_tokens"))):
        output = client.call_json(stage_dir=stage, prompt_text="fixture prompt", input_payload={"old_source": "old_api();"}, dry_run=dry_run, default_output={"patches": []}, max_tokens=77)
    audit = json.loads((stage / "provider_audit.json").read_text())
    for path in stage.iterdir():
        assert DUMMY_KEY not in path.read_text(), "credential leaked to " + path.name
    return output, audit


def check_http_error_no_resubmit(root):
    transport = FakeHTTP(["http_error"])
    with patch("urllib.request.urlopen", transport):
        output, audit = invoke(client_for(), root / "http_error")
    assert transport.calls.value == 1, "worker failure resubmitted a request"
    assert output["parse_status"] == "failed"
    assert audit["request_status"] == "failed"
    assert audit["request_attempt_count"] == 1 and audit["request_attempt_count_exact"]
    assert audit["attempts"][0]["http_status"] == 401
    assert audit["attempts"][0]["response"]["headers"] == "[REDACTED]"
    assert "response_raw" in audit["attempts"][0]


def check_configured_retries(root):
    for name, outcomes, expected_status in [
        ("recover", ["http_error", "http_error", response()], "succeeded"),
        ("fail", ["http_error"], "failed"),
    ]:
        transport = FakeHTTP(outcomes)
        with patch("urllib.request.urlopen", transport):
            _, audit = invoke(client_for(retries=2), root / ("retries_" + name))
        assert transport.calls.value == 3
        assert audit["request_attempt_count"] == 3 and audit["request_status"] == expected_status
        assert [a["attempt"] for a in audit["attempts"]] == [1, 2, 3]
        assert audit["observed_attempts_completed"] == 3


def check_success_and_body_identity(root):
    stage = root / "success"
    wire = root / "wire.json"
    expected_response = response()
    transport = FakeHTTP([expected_response], wire)
    with patch("urllib.request.urlopen", transport):
        output, audit = invoke(client_for(), stage)
    assert transport.calls.value == 1
    assert output == {"patches": [], "parse_status": "ok"}
    assert json.loads(wire.read_text()) == json.loads((stage / "request_body.json").read_text()) == build_chat_request("fixture-model", "fixture prompt", 77)
    attempt = audit["attempts"][0]
    assert attempt["response"] == expected_response
    assert json.loads(attempt["response_raw"]) == expected_response
    assert attempt["returned_model"] == "returned-model-version" and attempt["returned_provider"] == "fixture-upstream"
    assert attempt["usage"] == expected_response["usage"] and attempt["finish_reason"] == "stop"
    assert audit["response_truncated"] is False


def check_content_failures(root):
    for label, content, reason, parse_status in [
        ("truncated_bad_json", '{"patches": [', "length", "failed"),
        ("truncated_valid_json", '{"patches": []}', "length", "ok"),
        ("empty", "", "stop", "failed"),
        ("invalid_json", "plain text", "stop", "failed"),
    ]:
        transport = FakeHTTP([response(content, reason)])
        with patch("urllib.request.urlopen", transport):
            output, audit = invoke(client_for(), root / label)
        assert transport.calls.value == 1
        assert output["parse_status"] == parse_status
        assert audit["request_status"] == "succeeded"
        assert audit["response_truncated"] == (reason == "length")
        assert audit["attempts"][0]["finish_reason"] == reason
        assert audit["attempts"][0]["usage"]["total_tokens"] == 26


def check_malformed_provider_json(root):
    transport = FakeHTTP(['{"unfinished":'])
    with patch("urllib.request.urlopen", transport):
        output, audit = invoke(client_for(), root / "malformed_provider_json")
    assert transport.calls.value == 1 and output["parse_status"] == "failed"
    assert audit["attempts"][0]["response_raw"] == '{"unfinished":'
    assert audit["request_status"] == "failed"


def check_large_subprocess_response(root):
    content = json.dumps({"large": "x" * 1_500_000})
    transport = FakeHTTP([response(content)])
    start = time.monotonic()
    with patch("urllib.request.urlopen", transport):
        output, audit = invoke(client_for(), root / "large", deadline=5)
    assert transport.calls.value == 1
    assert len(output["large"]) == 1_500_000
    assert audit["request_status"] == "succeeded"
    assert time.monotonic() - start < 5, "large response IPC deadlocked"


def check_timeout_and_worker_crash(root):
    for kind, status in [("hang", "hard_timeout"), ("crash", "worker_failed")]:
        transport = FakeHTTP([kind])
        with patch("urllib.request.urlopen", transport):
            output, audit = invoke(client_for(retries=2), root / kind, deadline=0.2)
        assert transport.calls.value == 1, "timeout/crashed worker resubmitted a request"
        assert output["parse_status"] == "failed" and audit["request_status"] == status
        assert audit["request_attempt_count"] is None and not audit["request_attempt_count_exact"]
        assert audit["observed_attempts_started"] == 1
        assert audit["request_attempt_count_bounds"] == {"lower": 0, "upper": 3}
        assert audit["attempts"][0]["request_outcome"].startswith("unknown_")


def partial_frame_worker(provider, prompt, sender, max_tokens=None):
    import os
    import struct
    provider._audit_callback = lambda attempt: sender.send({"kind": "attempt", "attempt": attempt})
    provider.complete(prompt, max_tokens=max_tokens)
    # A readable frame prefix must not make the deadline thread wait forever
    # for the remainder of the serialized terminal result.
    os.write(sender.fileno(), struct.pack("!i", 4096) + b"partial")
    time.sleep(10)


def check_partial_frame_timeout(root):
    transport = FakeHTTP([response()])
    start = time.monotonic()
    with patch("urllib.request.urlopen", transport), patch("driver_agent_pregen.llm_client._provider_complete_worker", partial_frame_worker):
        output, audit = invoke(client_for(retries=2), root / "partial_frame", deadline=0.2)
    assert time.monotonic() - start < 1, "partial Pipe frame escaped hard deadline"
    assert transport.calls.value == 1, "partial Pipe frame caused a second request"
    assert output["parse_status"] == "failed" and audit["request_status"] == "hard_timeout"
    assert audit["request_attempt_count"] is None and not audit["request_attempt_count_exact"]
    assert audit["observed_attempts_completed"] == 1
    assert audit["attempts"][0]["response"] == response()
    import threading
    assert not any(t.name == "llm-provider-pipe-reader" for t in threading.enumerate()), "Pipe reader survived worker termination"


def check_dry_run_and_disabled(root):
    transport = FakeHTTP([response()])
    with patch("urllib.request.urlopen", transport):
        output, audit = invoke(client_for(), root / "dry_run", dry_run=True)
        disabled = LLMClient(LLMConfig(enabled=False, model="fixture-model", max_retries=0))
        _, no_provider = invoke(disabled, root / "disabled")
    assert transport.calls.value == 0
    assert output["parse_status"] == "dry_run" and audit["request_attempt_count"] == 0
    assert audit["request_status"] == "dry_run" and audit["attempts"] == []
    assert json.loads((root / "dry_run" / "request_body.json").read_text()) == build_chat_request("fixture-model", "fixture prompt", 77)
    assert no_provider["request_attempt_count"] == 0 and no_provider["request_status"] == "skipped_no_provider"


def check_openai_sdk(root):
    constructors = []
    requests = []
    mode = {"value": "error"}

    def create(**kwargs):
        requests.append(kwargs)
        if mode["value"] == "error":
            exc = RuntimeError("headers contain " + DUMMY_KEY)
            exc.status_code = 429
            exc.body = {"error": {"message": "quota exceeded " + DUMMY_KEY}}
            raise exc
        if mode["value"] == "hang":
            time.sleep(10)
        return SimpleNamespace(model_dump=lambda **unused: response())

    def constructor(**kwargs):
        constructors.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=constructor)}):
        for retries in (0, 2):
            provider = OpenAIProvider(model="fixture-model", api_key=DUMMY_KEY, base_url="https://fixture.invalid", max_retries=retries, max_tokens=123)
            provider.retry_backoff_sec = 0
            client = LLMClient(LLMConfig(enabled=False, provider="openai", model="fixture-model", max_retries=retries))
            client._provider = provider
            count_before = len(requests)
            output, audit = invoke(client, root / ("sdk_fail_" + str(retries)))
            assert len(requests) - count_before == retries + 1
            assert audit["request_attempt_count"] == retries + 1 and output["parse_status"] == "failed"
        mode["value"] = "success"
        output, audit = invoke(client, root / "sdk_success")
        assert output["parse_status"] == "ok" and audit["request_attempt_count"] == 1
        assert {k: v for k, v in requests[-1].items() if k != "timeout"} == build_chat_request("fixture-model", "fixture prompt", 77)
        assert audit["attempts"][0]["response"] == response()
        mode["value"] = "hang"
        count_before = len(requests)
        _, audit = invoke(client, root / "sdk_timeout", deadline=0.2)
        assert len(requests) - count_before == 1, "signal timeout retried"
        assert audit["request_status"] == "hard_timeout" and not audit["request_attempt_count_exact"]
        assert client._provider._audit_callback is None
    assert all(c["max_retries"] == 0 for c in constructors), "SDK hidden retries enabled"


def main():
    checks = [
        check_http_error_no_resubmit, check_configured_retries, check_success_and_body_identity,
        check_content_failures, check_malformed_provider_json, check_large_subprocess_response,
        check_timeout_and_worker_crash, check_partial_frame_timeout, check_dry_run_and_disabled, check_openai_sdk,
    ]
    with tempfile.TemporaryDirectory(prefix="driver-agent-llm-checks-") as tmp:
        for check in checks:
            check(Path(tmp))
            print("PASS", check.__name__)
    print(f"PASS: {len(checks)} offline check groups; zero real provider requests")


if __name__ == "__main__":
    main()
