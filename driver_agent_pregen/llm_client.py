from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .utils import extract_first_json_object, fenced_json_blocks, write_json, write_text


class Provider(Protocol):
    def complete(self, prompt: str) -> str: ...


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
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), base_url=base_url or os.getenv("OPENAI_BASE_URL"))

    def complete(self, prompt: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "You are a precise software analysis assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    timeout=self.timeout_sec,
                )
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
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))

    def complete(self, prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a precise software analysis assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
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
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
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

        try:
            raw = self._provider.complete(prompt_text)
        except Exception as exc:
            raw = ""
            failed = dict(default_output)
            failed["parse_status"] = "failed"
            failed["error"] = f"provider_error: {exc}"
            write_text(stage_dir / "raw_output.txt", raw)
            write_json(stage_dir / "parsed_output.json", failed)
            return failed

        write_text(stage_dir / "raw_output.txt", raw)

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
