from __future__ import annotations

import copy
import json
import multiprocessing as mp
import os
import queue
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .utils import extract_first_json_object, fenced_json_blocks, write_json, write_text


SYSTEM_MESSAGE = "You are a precise software analysis assistant."


def build_chat_request(model: str, prompt: str, max_tokens: int | None = None) -> dict[str, Any]:
    """Canonical provider body, shared with prompt-budget measurement (no auth)."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    if max_tokens and int(max_tokens) > 0:
        body["max_tokens"] = int(max_tokens)
    return body


def _safe(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Persist provider data, excluding credentials even when echoed in errors."""
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if str(k).lower().replace("-", "_") in {
                "authorization", "api_key", "apikey", "x_api_key", "access_token",
                "password", "cookie", "set_cookie", "headers", "request_headers",
            } else _safe(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"unserializable_type": type(value).__name__}


def _new_audit(body: dict[str, Any], provider: str, max_retries: int) -> dict[str, Any]:
    return {
        "schema_version": "llm_provider_audit_v1", "configured_provider": provider,
        "request_body": body, "configured_max_retries": max_retries,
        "request_attempt_limit": max_retries + 1, "attempts": [],
        "attempt_count_unit": "explicit_http_or_sdk_invocation",
        "request_status": "not_started", "response_truncated": False,
        # Attempts count HTTP/SDK invocations, not proven server receipt/billing.
        "request_attempt_count": 0, "request_attempt_count_exact": True,
        "observed_attempts_started": 0, "observed_attempts_completed": 0,
    }


def _count_attempts(audit: dict[str, Any], *, exact: bool) -> None:
    attempts = audit["attempts"]
    completed = sum(a.get("request_outcome") in {"succeeded", "failed"} for a in attempts)
    audit.update({
        "observed_attempts_started": len(attempts),
        "observed_attempts_completed": completed,
        "request_attempt_count_exact": exact,
        "request_attempt_count": len(attempts) if exact else None,
        "request_attempt_count_bounds": {
            "lower": len(attempts) if exact else completed,
            "upper": len(attempts) if exact else audit["request_attempt_limit"],
        },
    })


class _HardTimeoutError(TimeoutError):
    pass


class Provider(Protocol):
    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str: ...


class _AuditedProvider:
    model: str
    max_tokens: int | None
    max_retries: int
    retry_backoff_sec: int
    provider_name: str
    _secrets: tuple[str, ...] = ()
    _audit_callback: Callable[[dict[str, Any]], None] | None = None

    def request_body(self, prompt: str, max_tokens: int | None = None) -> dict[str, Any]:
        return build_chat_request(self.model, prompt, max_tokens or self.max_tokens)

    def _emit(self, attempt: dict[str, Any]) -> None:
        if self._audit_callback:
            self._audit_callback(copy.deepcopy(attempt))

    def _request_once(self, body: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        body = self.request_body(prompt, max_tokens)
        self.last_audit = _new_audit(_safe(body, self._secrets), self.provider_name, self.max_retries)
        self.last_audit["transport"] = "openai_sdk" if self.provider_name == "openai" else "urllib"
        self.last_audit["sdk_max_retries"] = 0 if self.provider_name == "openai" else None
        self.last_audit["request_status"] = "running"
        for index in range(self.max_retries + 1):
            attempt: dict[str, Any] = {
                "attempt": index + 1, "request_outcome": "started",
                "started_at_unix": time.time(),
            }
            self.last_audit["attempts"].append(attempt)
            self._emit(attempt)
            try:
                data = self._request_once(body, attempt)
                attempt["response"] = _safe(data, self._secrets)
                if isinstance(data, dict):
                    attempt.update({
                        "returned_model": data.get("model"), "returned_provider": data.get("provider"),
                        "usage": _safe(data.get("usage"), self._secrets),
                    })
                if not isinstance(data, dict) or data.get("error"):
                    raise ValueError("invalid_provider_response")
                choices = data.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    raise ValueError("provider_response_has_no_choices")
                choice = choices[0]
                finish_reason = choice.get("finish_reason")
                attempt.update({"finish_reason": finish_reason, "content_truncated": finish_reason in {"length", "max_tokens"}})
                content = (choice.get("message") or {}).get("content") or ""
                if isinstance(content, list):
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                if not isinstance(content, str):
                    raise ValueError("invalid_provider_content")
                attempt.update({
                    "request_outcome": "succeeded", "returned_model": data.get("model"),
                    "returned_provider": data.get("provider"), "usage": _safe(data.get("usage"), self._secrets),
                    "finish_reason": finish_reason, "content_truncated": finish_reason in {"length", "max_tokens"},
                    "content_length": len(content), "completed_at_unix": time.time(),
                })
                self._emit(attempt)
                self.last_audit["request_status"] = "succeeded"
                self.last_audit["response_truncated"] = attempt["content_truncated"]
                _count_attempts(self.last_audit, exact=True)
                return content
            except _HardTimeoutError:
                attempt["request_outcome"] = "unknown_timeout"
                self._emit(attempt)
                self.last_audit["request_status"] = "hard_timeout"
                _count_attempts(self.last_audit, exact=False)
                raise
            except Exception as exc:
                # Exception strings can contain request headers, URLs or keys.
                attempt.update({"request_outcome": "failed", "error_type": type(exc).__name__, "completed_at_unix": time.time()})
                status = getattr(exc, "status_code", None)
                if isinstance(status, int):
                    attempt["http_status"] = status
                error_body = getattr(exc, "body", None)
                if error_body is not None:
                    attempt["response"] = _safe(error_body, self._secrets)
                self._emit(attempt)
                if index < self.max_retries:
                    time.sleep(self.retry_backoff_sec * (index + 1))
        self.last_audit["request_status"] = "failed"
        _count_attempts(self.last_audit, exact=True)
        raise RuntimeError(f"provider_request_failed_after_{len(self.last_audit['attempts'])}_attempts")


class OpenAIProvider(_AuditedProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_sec: int = 300,
        max_retries: int = 2,
        retry_backoff_sec: int = 3,
        max_tokens: int | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))
        self.max_tokens = max_tokens if max_tokens and int(max_tokens) > 0 else None
        key = api_key or os.getenv("OPENAI_API_KEY")
        self._secrets = (key,) if key else ()
        self.provider_name = "openai"
        self.client = OpenAI(api_key=key, base_url=base_url or os.getenv("OPENAI_BASE_URL"), max_retries=0)

    def _request_once(self, body: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        response = self.client.chat.completions.create(**body, timeout=self.timeout_sec)
        return response.model_dump(mode="json")


class OpenRouterProvider(_AuditedProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_sec: int = 300,
        max_retries: int = 2,
        retry_backoff_sec: int = 3,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._secrets = (api_key,)
        self.provider_name = "openrouter"
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))
        self.max_tokens = max_tokens if max_tokens and int(max_tokens) > 0 else None

    def _request_once(self, body: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                raw = response.read().decode("utf-8", errors="replace")
                attempt["http_status"] = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            attempt["http_status"] = exc.code
            attempt["response_raw"] = _safe(raw, self._secrets)
            try:
                attempt["response"] = _safe(json.loads(raw), self._secrets)
            except (ValueError, TypeError):
                pass
            raise
        attempt["response_raw"] = _safe(raw, self._secrets)
        return json.loads(raw)


@dataclass
class LLMConfig:
    provider: str = "openai"
    model: str = "gpt-4.1"
    enabled: bool = False
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    config_ini: Optional[str] = None
    timeout_sec: int = 300
    max_retries: int = 2
    retry_backoff_sec: int = 3
    max_tokens: int = 10000


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._provider: Provider | None = None
        self.last_audit: dict[str, Any] = {}
        self._audit_stage_dir: Path | None = None
        if config.enabled:
            if config.provider == "openai":
                self._provider = OpenAIProvider(
                    model=config.model,
                    api_key=config.api_key or os.getenv("OPENAI_API_KEY"),
                    base_url=config.base_url or os.getenv("OPENAI_BASE_URL"),
                    timeout_sec=config.timeout_sec,
                    max_retries=config.max_retries,
                    retry_backoff_sec=config.retry_backoff_sec,
                    max_tokens=config.max_tokens,
                )
            elif config.provider == "openrouter":
                key = config.api_key or os.getenv("OPEN_ROUTER_KEY")
                if not key and config.config_ini:
                    key = _read_kv_like_config_key(Path(config.config_ini), "OPEN_ROUTER_KEY")
                if not key:
                    raise ValueError("OPEN_ROUTER_KEY not found (env or config.ini)")
                self._provider = OpenRouterProvider(
                    model=config.model,
                    api_key=key,
                    base_url=config.base_url or "https://openrouter.ai/api/v1",
                    timeout_sec=config.timeout_sec,
                    max_retries=config.max_retries,
                    retry_backoff_sec=config.retry_backoff_sec,
                    max_tokens=config.max_tokens,
                )
            elif config.provider == "deepseek":
                key = config.api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_KEY")
                if not key and config.config_ini:
                    key = (
                        _read_kv_like_config_key(Path(config.config_ini), "DEEPSEEK_API_KEY")
                        or _read_kv_like_config_key(Path(config.config_ini), "DEEPSEEK_KEY")
                    )
                if not key:
                    raise ValueError("DEEPSEEK_API_KEY/DEEPSEEK_KEY not found (env or config.ini)")
                # DeepSeek exposes an OpenAI-compatible Chat Completions API.
                # Use the stdlib HTTP provider rather than the OpenAI Python
                # SDK so sudo/root validation runs do not depend on root's
                # site-packages.
                self._provider = OpenRouterProvider(
                    model=config.model,
                    api_key=key,
                    base_url=config.base_url or "https://api.deepseek.com",
                    timeout_sec=config.timeout_sec,
                    max_retries=config.max_retries,
                    retry_backoff_sec=config.retry_backoff_sec,
                    max_tokens=config.max_tokens,
                )
            else:
                raise ValueError(f"unsupported provider: {config.provider}")

    def _prepare_audit(self, prompt: str, max_tokens: int | None) -> None:
        if isinstance(self._provider, _AuditedProvider):
            body = self._provider.request_body(prompt, max_tokens)
        else:
            body = build_chat_request(self.config.model, prompt, max_tokens or self.config.max_tokens)
        self.last_audit = _new_audit(body, self.config.provider, max(0, self.config.max_retries))

    def _persist_audit(self, stage_dir: Path) -> None:
        secrets = getattr(self._provider, "_secrets", ())
        self.last_audit = _safe(self.last_audit, secrets)
        write_json(stage_dir / "request_body.json", self.last_audit["request_body"])
        write_json(stage_dir / "provider_audit.json", self.last_audit)

    def _record_attempt(self, attempt: dict[str, Any]) -> None:
        attempts = self.last_audit["attempts"]
        index = attempt["attempt"] - 1
        if index == len(attempts):
            attempts.append(attempt)
        else:
            attempts[index] = attempt
        if "content_truncated" in attempt:
            self.last_audit["response_truncated"] = attempt["content_truncated"]
        _count_attempts(self.last_audit, exact=False)
        if self._audit_stage_dir:
            self._persist_audit(self._audit_stage_dir)

    def call_json(
        self,
        *,
        stage_dir: Path,
        prompt_text: str,
        input_payload: dict[str, Any],
        dry_run: bool,
        default_output: dict[str, Any],
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        write_text(stage_dir / "prompt.md", prompt_text)
        write_json(stage_dir / "input.json", input_payload)
        self._audit_stage_dir = stage_dir
        self._prepare_audit(prompt_text, max_tokens)
        self._persist_audit(stage_dir)

        def finish(raw: str, parsed: dict[str, Any]) -> dict[str, Any]:
            self.last_audit["parse_status"] = parsed.get("parse_status")
            self.last_audit["content_error"] = parsed.get("error") if self.last_audit["request_status"] == "succeeded" else None
            self._persist_audit(stage_dir)
            secrets = getattr(self._provider, "_secrets", ())
            write_text(stage_dir / "raw_output.txt", _safe(raw, secrets))
            write_json(stage_dir / "parsed_output.json", _safe(parsed, secrets))
            return parsed

        if dry_run:
            self.last_audit["request_status"] = "dry_run"
            _count_attempts(self.last_audit, exact=True)
            parsed = dict(default_output)
            parsed["parse_status"] = "dry_run"
            return finish("", parsed)

        if not self._provider:
            self.last_audit["request_status"] = "skipped_no_provider"
            _count_attempts(self.last_audit, exact=True)
            parsed = dict(default_output)
            parsed.update({"parse_status": "skipped_no_provider", "error": "LLM provider not configured"})
            return finish("", parsed)

        timeout_sec = max(30, int(self.config.timeout_sec or 300))
        try:
            raw = self._complete_with_hard_timeout(prompt_text, timeout_sec=timeout_sec, max_tokens=max_tokens)
        except Exception as exc:
            failed = dict(default_output)
            failed.update({"parse_status": "failed", "error": f"provider_error: {type(exc).__name__}"})
            return finish("", failed)

        if not raw.strip():
            failed = dict(default_output)
            failed.update({"parse_status": "failed", "error": "empty_llm_response"})
            return finish(raw, failed)

        parsed = self._robust_parse_json(raw)
        if parsed is None:
            failed = dict(default_output)
            failed.update({"parse_status": "failed", "error": "json_parse_failed"})
            return finish(raw, failed)
        parsed.setdefault("parse_status", "ok")
        return finish(raw, parsed)

    def _robust_parse_json(self, text: str) -> Optional[dict[str, Any]]:
        blocks = fenced_json_blocks(text)
        for block in blocks:
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj

        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        first = extract_first_json_object(text)
        if first:
            try:
                obj = json.loads(first)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

        return None

    def _complete_with_hard_timeout(self, prompt_text: str, *, timeout_sec: int, max_tokens: int | None = None) -> str:
        if not self._provider:
            raise RuntimeError("LLM provider not configured")
        self._prepare_audit(prompt_text, max_tokens)
        self.last_audit["request_status"] = "running"

        # Drain responses while the worker is alive. Joining first deadlocks
        # once a response fills the IPC buffer. No retry/fallback is permitted
        # after process.start(): the child may already have sent a request.
        ctx = None
        if isinstance(self._provider, OpenRouterProvider):
            try:
                ctx = mp.get_context("fork")
            except (ValueError, AttributeError):
                pass  # No worker/request has been started; direct mode is safe.
        if ctx is not None:
            receiver, sender = ctx.Pipe(duplex=False)
            process = ctx.Process(target=_provider_complete_worker, args=(self._provider, prompt_text, sender, max_tokens))
            started = False
            reader: threading.Thread | None = None
            try:
                process.start()
                started = True
                sender.close()
                deadline = time.monotonic() + float(timeout_sec)
                events: queue.Queue = queue.Queue()

                def read_events() -> None:
                    # poll() only promises a frame prefix is readable. recv()
                    # can still block on the remainder, so keep it off the
                    # deadline-enforcing thread and unblock it by killing the
                    # writer on timeout.
                    try:
                        while True:
                            event = receiver.recv()
                            events.put(("event", event))
                            if event["kind"] != "attempt":
                                return
                    except (EOFError, OSError) as exc:
                        events.put(("ipc_error", type(exc).__name__))

                reader = threading.Thread(target=read_events, daemon=True, name="llm-provider-pipe-reader")
                reader.start()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _HardTimeoutError(f"hard_timeout_after_{timeout_sec}s")
                    try:
                        kind, event = events.get(timeout=remaining)
                    except queue.Empty:
                        raise _HardTimeoutError(f"hard_timeout_after_{timeout_sec}s") from None
                    if kind == "ipc_error":
                        raise RuntimeError("provider_subprocess_no_output")
                    if event["kind"] == "attempt":
                        self._record_attempt(event["attempt"])
                        continue
                    self.last_audit = event["audit"]
                    self.last_audit["configured_provider"] = self.config.provider
                    self._provider.last_audit = self.last_audit
                    if event["kind"] == "ok":
                        return event["content"]
                    raise RuntimeError("provider_worker_request_failed")
            except BaseException as exc:
                if self.last_audit["request_status"] == "running":
                    self.last_audit["request_status"] = "hard_timeout" if isinstance(exc, TimeoutError) else "worker_failed"
                    self.last_audit["error_type"] = type(exc).__name__
                    for attempt in self.last_audit["attempts"]:
                        if attempt["request_outcome"] == "started":
                            attempt["request_outcome"] = "unknown_timeout" if isinstance(exc, TimeoutError) else "unknown_worker_failure"
                    _count_attempts(self.last_audit, exact=False)
                raise
            finally:
                if started:
                    if process.is_alive():
                        process.kill()
                    process.join()
                    process.close()
                if reader is not None:
                    reader.join(timeout=0.2)
                receiver.close()
                sender.close()

        # Non-fork providers remain compatible with complete() -> str.
        def run_direct() -> str:
            try:
                if isinstance(self._provider, _AuditedProvider):
                    self._provider._audit_callback = self._record_attempt
                raw = self._provider.complete(prompt_text, max_tokens=max_tokens)
                self.last_audit = getattr(self._provider, "last_audit", self.last_audit)
                self.last_audit["configured_provider"] = self.config.provider
                if self.last_audit["request_status"] == "running":
                    self.last_audit["request_status"] = "succeeded"
                    _count_attempts(self.last_audit, exact=False)
                return raw
            except BaseException as exc:
                self.last_audit = getattr(self._provider, "last_audit", self.last_audit)
                if self.last_audit["request_status"] == "running":
                    self.last_audit["request_status"] = "hard_timeout" if isinstance(exc, TimeoutError) else "failed"
                    _count_attempts(self.last_audit, exact=False)
                raise
            finally:
                if isinstance(self._provider, _AuditedProvider):
                    self._provider._audit_callback = None

        if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
            def _raise_timeout(signum, frame):  # type: ignore[no-untyped-def]
                raise _HardTimeoutError(f"hard_timeout_after_{timeout_sec}s")

            prev_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, float(timeout_sec))
            try:
                return run_direct()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)
        return run_direct()


def _provider_complete_worker(provider: Provider, prompt_text: str, sender, max_tokens: int | None = None) -> None:  # type: ignore[no-untyped-def]
    try:
        provider._audit_callback = lambda attempt: sender.send({"kind": "attempt", "attempt": attempt})
        out = provider.complete(prompt_text, max_tokens=max_tokens)
        sender.send({"kind": "ok", "content": out, "audit": provider.last_audit})
    except Exception as exc:
        sender.send({"kind": "err", "error_type": type(exc).__name__, "audit": provider.last_audit})
    finally:
        sender.close()


def _read_kv_like_config_key(path: Path, key_name: str) -> Optional[str]:
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key_name)}\s*=\s*(.+?)\s*$")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = pattern.match(raw)
        if not m:
            continue
        val = m.group(1).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        return val.strip() or None
    return None
