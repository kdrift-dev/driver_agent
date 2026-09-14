"""Teacher-forced NLL backends with a shared disk cache.

Backends implement `score(prompt, target) -> dict` with keys
{backend, model, nll_sum, n_target_tokens, nll_per_token, ppl,
prompt_truncated}. Any transport/auth/echo-support problem raises
ProviderFailure — harnesses must exclude such rows from denominators, never
count them as scores. The mock backend is deterministic and offline; see its
docstring for the (intentionally simple) cost model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from pathlib import Path

from .textutil import IDENTIFIER_RE

DEFAULT_PPL_MODEL = os.environ.get("PPL_MODEL", "Qwen/Qwen2.5-Coder-0.5B")
DEFAULT_MAX_INPUT_TOKENS = int(os.environ.get("PPL_MAX_INPUT_TOKENS", "8192"))
OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/completions"


class ProviderFailure(RuntimeError):
    pass


class ContextWindowExceeded(ValueError):
    """A comparison prompt cannot be scored intact in the model window."""


def lexical_token_count(text):
    """Offline non-model proxy: word/number and punctuation occurrences."""
    return len(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\w\s]", text))


class ScoreCache:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(model, prompt, target):
        h = hashlib.sha256()
        for part in (model, prompt, target):
            h.update(part.encode("utf-8", errors="replace"))
            h.update(b"\x00")
        return h.hexdigest()

    def get(self, key):
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text())
                payload["cache_hit"] = True
                return payload
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def put(self, key, payload):
        if not self.cache_dir:
            return
        path = self.cache_dir / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True))
        tmp.replace(path)


def cached_score(backend, cache, prompt, target):
    identity = f"{type(backend).__name__}:{backend.model_name}:{getattr(backend, 'max_input_tokens', None)}:v2"
    key = ScoreCache.key(identity, prompt, target)
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = backend.score(prompt, target)
    result["cache_hit"] = False
    cache.put(key, {k: v for k, v in result.items() if k != "cache_hit"})
    return result


class MockBackend:
    """Deterministic offline NLL: copy-friendly tokens are cheap.

    Cost model: split the target into identifier/word tokens; a token costs
    SEEN_NLL if it occurs in the prompt, UNSEEN_NLL otherwise. This mimics the
    one LM behavior the experiments rely on — context that contains what the
    edit needs lowers the loss — while staying exactly reproducible. Only
    orderings produced under this model are meaningful offline; absolute
    values are not, and reports must label mock results as such.
    """

    SEEN_NLL = 1.0
    UNSEEN_NLL = 4.0

    def __init__(self, model_name="mock"):
        self.model_name = model_name

    token_count_kind = "non_model_lexical_proxy"

    def count_tokens(self, text):
        return lexical_token_count(text)

    def score(self, prompt, target):
        tokens = IDENTIFIER_RE.findall(target)
        if not tokens:
            raise ValueError("empty target after tokenization")
        prompt_tokens = set(IDENTIFIER_RE.findall(prompt))
        nll_sum = sum(self.SEEN_NLL if t in prompt_tokens else self.UNSEEN_NLL
                      for t in tokens)
        per_token = nll_sum / len(tokens)
        return {
            "backend": "mock", "model": self.model_name,
            "nll_sum": round(nll_sum, 6), "n_target_tokens": len(tokens),
            "n_prompt_tokens": self.count_tokens(prompt),
            "token_count_kind": self.token_count_kind,
            "target_token_count_kind": "mock_identifier_occurrences",
            "nll_per_token": round(per_token, 6),
            "ppl": round(math.exp(min(per_token, 50.0)), 4),
            "prompt_truncated": False,
        }


class HFTeacherForcedBackend:
    """Local causal-LM teacher forcing. Lazy model load; deterministic."""

    def __init__(self, model_name=DEFAULT_PPL_MODEL,
                 max_input_tokens=DEFAULT_MAX_INPUT_TOKENS):
        self.model_name = model_name
        self.max_input_tokens = max_input_tokens
        self._model = None
        self._tokenizer = None
        self._torch = None

    token_count_kind = "model_tokens"

    def _load_tokenizer(self):
        if self._tokenizer is None:
            from transformers import AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)

    def count_tokens(self, text):
        self._load_tokenizer()
        return len(self._tokenizer(text, add_special_tokens=False).input_ids)

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM
        except ImportError as exc:
            raise RuntimeError(
                "hf backend needs torch+transformers; create .venv-ppl and run "
                "with its interpreter"
            ) from exc
        self._torch = torch
        torch.manual_seed(0)
        self._load_tokenizer()
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_name, torch_dtype=torch.float32
        )
        self._model.eval()

    def score(self, prompt, target):
        self._load()
        torch = self._torch
        prompt_ids = self._tokenizer(prompt, add_special_tokens=False).input_ids
        target_ids = self._tokenizer(target, add_special_tokens=False).input_ids
        if not target_ids:
            raise ValueError("empty target after tokenization")
        # If over the window, trim the prompt head: the target and the most
        # recent context are what matter for teacher forcing.
        overflow = len(prompt_ids) + len(target_ids) - self.max_input_tokens
        prompt_truncated = False
        if overflow > 0:
            prompt_ids = prompt_ids[overflow:]
            prompt_truncated = True
        input_ids = torch.tensor([prompt_ids + target_ids])
        labels = input_ids.clone()
        labels[0, : len(prompt_ids)] = -100  # mask prompt tokens
        with torch.no_grad():
            out = self._model(input_ids=input_ids, labels=labels)
        # loss is mean NLL over unmasked (target) tokens
        n_target = len(target_ids)
        nll_per_token = float(out.loss)
        nll_sum = nll_per_token * n_target
        return {
            "backend": "hf", "model": self.model_name,
            "token_count_kind": self.token_count_kind,
            "nll_sum": round(nll_sum, 6), "n_target_tokens": n_target,
            "n_prompt_tokens": len(prompt_ids),
            "nll_per_token": round(nll_per_token, 6),
            "ppl": round(math.exp(min(nll_per_token, 50.0)), 4),
            "prompt_truncated": prompt_truncated,
        }


class OpenRouterEchoBackend:
    """Raw /completions with echo+logprobs. Fallback backend only."""

    def __init__(self, model_name, api_key=None, timeout=120):
        self.model_name = model_name
        self.api_key = api_key or os.environ.get("OPEN_ROUTER_KEY", "")
        self.timeout = timeout

    def score(self, prompt, target):
        if not self.api_key:
            raise ProviderFailure("OPEN_ROUTER_KEY not set")
        body = {
            "model": self.model_name, "prompt": prompt + target,
            "max_tokens": 0, "echo": True, "logprobs": 1, "temperature": 0,
        }
        req = urllib.request.Request(
            OPENROUTER_COMPLETIONS_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - all transport errors are provider failures
            raise ProviderFailure(f"openrouter request failed: {exc}") from exc
        try:
            choice = payload["choices"][0]
            token_logprobs = choice["logprobs"]["token_logprobs"]
            offsets = choice["logprobs"].get("text_offset")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderFailure(
                f"provider did not return echo logprobs (model={self.model_name})"
            ) from exc
        if offsets is None:
            raise ProviderFailure("no text_offset in logprobs; cannot locate target")
        target_start = len(prompt)
        nlls = [-lp for off, lp in zip(offsets, token_logprobs)
                if off >= target_start and lp is not None]
        if not nlls:
            raise ProviderFailure("no target tokens in echoed logprobs")
        nll_sum = sum(nlls)
        return {
            "backend": "openrouter", "model": self.model_name,
            "nll_sum": round(nll_sum, 6), "n_target_tokens": len(nlls),
            "nll_per_token": round(nll_sum / len(nlls), 6),
            "ppl": round(math.exp(min(nll_sum / len(nlls), 50.0)), 4),
            "prompt_truncated": False,
        }


def make_backend(kind, model_name=None):
    if kind == "hf":
        return HFTeacherForcedBackend(model_name or DEFAULT_PPL_MODEL)
    if kind == "openrouter":
        return OpenRouterEchoBackend(model_name or "qwen/qwen-2.5-coder-32b-instruct")
    if kind == "mock":
        return MockBackend(model_name or "mock")
    raise ValueError(f"unknown backend kind: {kind}")
