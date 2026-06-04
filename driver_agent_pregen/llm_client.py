from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .utils import extract_first_json_object, fenced_json_blocks, write_json, write_text


class Provider(Protocol):
    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str: ...


class OpenAIProvider:
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
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), base_url=base_url or os.getenv("OPENAI_BASE_URL"))

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are a precise software analysis assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "timeout": self.timeout_sec,
                }
                effective_max_tokens = max_tokens or self.max_tokens
                if effective_max_tokens:
                    kwargs["max_tokens"] = effective_max_tokens
                resp = self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                sleep_sec = self.retry_backoff_sec * (attempt + 1)
                time.sleep(sleep_sec)

        raise RuntimeError(f"LLM request failed after retries: {last_exc}")


class OpenRouterProvider:
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
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))
        self.max_tokens = max_tokens if max_tokens and int(max_tokens) > 0 else None

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a precise software analysis assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        effective_max_tokens = max_tokens or self.max_tokens
        if effective_max_tokens:
            body["max_tokens"] = effective_max_tokens
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                choices = data.get("choices", [])
                if not choices:
                    raise RuntimeError("openrouter response has no choices")
                msg = choices[0].get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    out = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            out.append(part.get("text", ""))
                    return "".join(out)
                return content or ""
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff_sec * (attempt + 1))

        raise RuntimeError(f"OpenRouter request failed after retries: {last_exc}")


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

        if dry_run:
            raw = ""
            parsed = dict(default_output)
            parsed["parse_status"] = "dry_run"
            write_text(stage_dir / "raw_output.txt", raw)
            write_json(stage_dir / "parsed_output.json", parsed)
            return parsed

        if not self._provider:
            raw = ""
            parsed = dict(default_output)
            parsed["parse_status"] = "skipped_no_provider"
            parsed["error"] = "LLM provider not configured"
            write_text(stage_dir / "raw_output.txt", raw)
            write_json(stage_dir / "parsed_output.json", parsed)
            return parsed

        timeout_sec = max(30, int(self.config.timeout_sec or 300))

        try:
            raw = self._complete_with_hard_timeout(
                prompt_text,
                timeout_sec=timeout_sec,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raw = ""
            failed = dict(default_output)
            failed["parse_status"] = "failed"
            failed["error"] = f"provider_error: {exc}"
            write_text(stage_dir / "raw_output.txt", raw)
            write_json(stage_dir / "parsed_output.json", failed)
            return failed

        write_text(stage_dir / "raw_output.txt", raw)

        if not raw.strip():
            failed = dict(default_output)
            failed["parse_status"] = "failed"
            failed["error"] = "empty_llm_response"
            write_json(stage_dir / "parsed_output.json", failed)
            return failed

        parsed = self._robust_parse_json(raw)
        if parsed is None:
            failed = dict(default_output)
            failed["parse_status"] = "failed"
            failed["error"] = "json_parse_failed"
            write_json(stage_dir / "parsed_output.json", failed)
            return failed

        if isinstance(parsed, dict):
            parsed.setdefault("parse_status", "ok")
            write_json(stage_dir / "parsed_output.json", parsed)
            return parsed

        failed = dict(default_output)
        failed["parse_status"] = "failed"
        failed["error"] = "json_not_object"
        write_json(stage_dir / "parsed_output.json", failed)
        return failed

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

        # OpenRouter occasionally hangs deep in HTTP stack; isolate into subprocess and enforce wall-clock timeout.
        if isinstance(self._provider, OpenRouterProvider) and hasattr(mp, "get_context"):
            try:
                ctx = mp.get_context("fork")
                q = ctx.Queue(maxsize=1)
                p = ctx.Process(target=_provider_complete_worker, args=(self._provider, prompt_text, q, max_tokens))
                p.start()
                p.join(float(timeout_sec))
                if p.is_alive():
                    p.kill()
                    p.join()
                    raise TimeoutError(f"hard_timeout_after_{timeout_sec}s")
                if q.empty():
                    raise RuntimeError("provider_subprocess_no_output")
                status, payload = q.get_nowait()
                if status == "ok":
                    return str(payload)
                raise RuntimeError(str(payload))
            except TimeoutError:
                raise
            except Exception:
                # Fall back to in-process timeout path below.
                pass

        # Best-effort hard timeout guard to avoid indefinite hangs in provider HTTP stacks.
        if hasattr(signal, "SIGALRM"):
            def _raise_timeout(signum, frame):  # type: ignore[no-untyped-def]
                raise TimeoutError(f"hard_timeout_after_{timeout_sec}s")

            prev_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, float(timeout_sec))
            try:
                return self._provider.complete(prompt_text, max_tokens=max_tokens)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)

        # Non-POSIX fallback.
        return self._provider.complete(prompt_text, max_tokens=max_tokens)


def _provider_complete_worker(provider: Provider, prompt_text: str, q, max_tokens: int | None = None) -> None:  # type: ignore[no-untyped-def]
    try:
        out = provider.complete(prompt_text, max_tokens=max_tokens)
        q.put(("ok", out))
    except Exception as exc:  # pragma: no cover - defensive path for subprocess boundary
        q.put(("err", str(exc)))


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
