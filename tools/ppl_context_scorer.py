#!/usr/bin/env python3
"""Context-value scorers for the context-engineering evaluation harness.

Evaluation-only tool (never part of the agent pipeline; it reads reference
diffs, which the agent must never see). Two halves:

1. Deterministic half — expressibility gate (no ML dependencies):
   for every identifier used by the added lines of the reference edit, is it
   available in (candidate context UNION old source)? Pure set logic.

2. PPL half — teacher-forced NLL of a given target under a causal LM:
   score(prompt, target) -> {nll_sum, n_target_tokens, nll_per_token, ppl}.
   Backends:
     - "hf": local HuggingFace causal LM (default Qwen/Qwen2.5-Coder-0.5B),
       CPU is fine — only the *ranking* across contexts matters. Requires a
       separate venv with torch+transformers (see PPL_PYTHON convention),
       e.g.:  python3 -m venv .venv-ppl && \
              .venv-ppl/bin/pip install torch transformers --extra-index-url \
                  https://download.pytorch.org/whl/cpu
     - "openrouter": raw /completions with echo+logprobs. Provider support is
       spotty; a missing-support or auth/quota error is reported as a
       provider_failure, never as a scoring result.

All PPL scores go through a JSON disk cache keyed by
sha256(model \\0 prompt \\0 target); reruns are free and deterministic.

CLI (single measurement; the experiment loop lives in
tools/run_ppl_signal_experiment.py):

  python tools/ppl_context_scorer.py \
    --context-file ctx.txt --old-source-file old.c --reference-diff ref.diff \
    --scorer expressibility-only

  python tools/ppl_context_scorer.py \
    --prompt-file prompt.txt --target-file ref.diff \
    --scorer hf --cache-dir runs/ppl_cache

Env overrides: PPL_MODEL (default Qwen/Qwen2.5-Coder-0.5B),
OPEN_ROUTER_KEY (openrouter backend), PPL_MAX_INPUT_TOKENS (default 8192).
"""

import argparse
import hashlib
import json
import math
import os
import sys
import urllib.request
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.textutil import (  # noqa: E402
    C_KEYWORDS,
    identifiers as _identifiers,
    strip_comments_and_strings as _strip_comments_and_strings,
)

DEFAULT_PPL_MODEL = os.environ.get("PPL_MODEL", "Qwen/Qwen2.5-Coder-0.5B")
DEFAULT_MAX_INPUT_TOKENS = int(os.environ.get("PPL_MAX_INPUT_TOKENS", "8192"))
OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/completions"


# ---------------------------------------------------------------------------
# Deterministic half: expressibility gate
# ---------------------------------------------------------------------------

def _added_lines(reference_diff):
    lines = []
    for line in reference_diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines


def expressibility_check(context_text, old_source_text, reference_diff):
    """Can the reference edit be expressed given (context + old source)?

    Returns identifier coverage of the edit's added lines. An identifier is
    "covered" if it appears anywhere in the candidate context or in the old
    source. This is deliberately a *mention* check, not a full declaration
    check: it is monotone, deterministic and cheap, and its per-condition
    pass-rate profile (gold ~1.0, none ~0.0) is validated by the experiment
    itself.
    """
    needed = _identifiers(_strip_comments_and_strings("\n".join(_added_lines(reference_diff))))
    available = _identifiers(old_source_text) | _identifiers(context_text)
    missing = sorted(needed - available)
    coverage = 1.0 if not needed else (len(needed) - len(missing)) / len(needed)
    return {
        "needed_identifiers": len(needed),
        "missing_identifiers": missing,
        "coverage": round(coverage, 4),
        "gate_pass": not missing,
    }


# ---------------------------------------------------------------------------
# PPL half: teacher-forced NLL scorers behind a disk cache
# ---------------------------------------------------------------------------

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


class HFTeacherForcedScorer:
    """Local causal-LM teacher forcing. Lazy model load; deterministic."""

    def __init__(self, model_name=DEFAULT_PPL_MODEL,
                 max_input_tokens=DEFAULT_MAX_INPUT_TOKENS):
        self.model_name = model_name
        self.max_input_tokens = max_input_tokens
        self._model = None
        self._tokenizer = None
        self._torch = None

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "hf scorer needs torch+transformers; create .venv-ppl (see "
                "module docstring) and run this tool with its python"
            ) from exc
        self._torch = torch
        torch.manual_seed(0)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
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
            "backend": "hf",
            "model": self.model_name,
            "nll_sum": round(nll_sum, 6),
            "n_target_tokens": n_target,
            "n_prompt_tokens": len(prompt_ids),
            "nll_per_token": round(nll_per_token, 6),
            "ppl": round(math.exp(min(nll_per_token, 50.0)), 4),
            "prompt_truncated": prompt_truncated,
        }


class OpenRouterEchoScorer:
    """Raw /completions with echo+logprobs. Fallback backend only.

    Any HTTP/auth/quota error or missing echo/logprobs support raises
    ProviderFailure so the harness can exclude the trial from denominators
    (a provider failure is never a model/pipeline result).
    """

    def __init__(self, model_name, api_key=None, timeout=120):
        self.model_name = model_name
        self.api_key = api_key or os.environ.get("OPEN_ROUTER_KEY", "")
        self.timeout = timeout

    def score(self, prompt, target):
        if not self.api_key:
            raise ProviderFailure("OPEN_ROUTER_KEY not set")
        body = {
            "model": self.model_name,
            "prompt": prompt + target,
            "max_tokens": 0,
            "echo": True,
            "logprobs": 1,
            "temperature": 0,
        }
        req = urllib.request.Request(
            OPENROUTER_COMPLETIONS_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
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
                "provider did not return echo logprobs "
                f"(model={self.model_name})"
            ) from exc
        if offsets is None:
            raise ProviderFailure("no text_offset in logprobs; cannot locate target")
        target_start = len(prompt)
        nlls = [
            -lp
            for off, lp in zip(offsets, token_logprobs)
            if off >= target_start and lp is not None
        ]
        if not nlls:
            raise ProviderFailure("no target tokens in echoed logprobs")
        nll_sum = sum(nlls)
        return {
            "backend": "openrouter",
            "model": self.model_name,
            "nll_sum": round(nll_sum, 6),
            "n_target_tokens": len(nlls),
            "nll_per_token": round(nll_sum / len(nlls), 6),
            "ppl": round(math.exp(min(nll_sum / len(nlls), 50.0)), 4),
            "prompt_truncated": False,
        }


class ProviderFailure(RuntimeError):
    pass


def make_scorer(kind, model_name=None):
    if kind == "hf":
        return HFTeacherForcedScorer(model_name or DEFAULT_PPL_MODEL)
    if kind == "openrouter":
        return OpenRouterEchoScorer(model_name or "qwen/qwen-2.5-coder-32b-instruct")
    raise ValueError(f"unknown scorer kind: {kind}")


def cached_score(scorer, cache, prompt, target):
    key = ScoreCache.key(scorer.model_name, prompt, target)
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = scorer.score(prompt, target)
    result["cache_hit"] = False
    cache.put(key, {k: v for k, v in result.items() if k != "cache_hit"})
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read(path):
    return Path(path).read_text(errors="replace")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scorer", default="expressibility-only",
                        choices=["expressibility-only", "hf", "openrouter"])
    parser.add_argument("--model", default=None,
                        help="model name override (default: PPL_MODEL env)")
    parser.add_argument("--cache-dir", default=None,
                        help="disk cache dir for PPL scores (optional)")
    # expressibility inputs
    parser.add_argument("--context-file", default=None)
    parser.add_argument("--old-source-file", default=None)
    parser.add_argument("--reference-diff", default=None)
    # ppl inputs
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--target-file", default=None)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    out = {}
    if args.reference_diff:
        out["expressibility"] = expressibility_check(
            _read(args.context_file) if args.context_file else "",
            _read(args.old_source_file) if args.old_source_file else "",
            _read(args.reference_diff),
        )
    if args.scorer != "expressibility-only":
        if not (args.prompt_file and args.target_file):
            print("error: --prompt-file and --target-file required for PPL scoring",
                  file=sys.stderr)
            return 2
        scorer = make_scorer(args.scorer, args.model)
        cache = ScoreCache(args.cache_dir)
        try:
            out["ppl"] = cached_score(scorer, cache,
                                      _read(args.prompt_file),
                                      _read(args.target_file))
        except ProviderFailure as exc:
            out["ppl"] = {"provider_failure": str(exc)}
    if not out:
        print("error: nothing to do (pass --reference-diff and/or --prompt-file/--target-file)",
              file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
