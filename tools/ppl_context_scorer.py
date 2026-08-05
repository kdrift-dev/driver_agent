#!/usr/bin/env python3
"""Single-measurement CLI for the context-value metrics (thin over ctxlab).

Evaluation-only tool (never part of the agent pipeline; it reads reference
diffs, which the agent must never see). The library now lives in
ctxlab.metrics (expressibility gate + score_context) and ctxlab.backends (NLL
backends + disk cache). This file keeps a one-shot CLI and re-exports the
names the experiment harness imports.

  # deterministic gate only, no ML deps:
  python tools/ppl_context_scorer.py \
    --context-file ctx.txt --old-source-file old.c --reference-diff ref.diff \
    --scorer expressibility-only

  # teacher-forced PPL of a target under a backend, cached:
  python tools/ppl_context_scorer.py \
    --prompt-file prompt.txt --target-file ref.diff \
    --scorer hf --cache-dir runs/ppl_cache

Env overrides: PPL_MODEL (default Qwen/Qwen2.5-Coder-0.5B),
OPEN_ROUTER_KEY (openrouter backend), PPL_MAX_INPUT_TOKENS (default 8192).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.backends import (  # noqa: E402,F401  (re-exported for the harness)
    ProviderFailure,
    ScoreCache,
    cached_score,
    make_backend,
)
from ctxlab.metrics import expressibility_check  # noqa: E402,F401


# Backward-compatible alias: the harness historically called make_scorer.
def make_scorer(kind, model_name=None):
    return make_backend(kind, model_name)


def _read(path):
    return Path(path).read_text(errors="replace")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scorer", default="expressibility-only",
                        choices=["expressibility-only", "hf", "openrouter", "mock"])
    parser.add_argument("--model", default=None,
                        help="model name override (default: PPL_MODEL env)")
    parser.add_argument("--cache-dir", default=None,
                        help="disk cache dir for PPL scores (optional)")
    parser.add_argument("--context-file", default=None)
    parser.add_argument("--old-source-file", default=None)
    parser.add_argument("--reference-diff", default=None)
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
        backend = make_backend(args.scorer, args.model)
        cache = ScoreCache(args.cache_dir)
        try:
            out["ppl"] = cached_score(backend, cache,
                                      _read(args.prompt_file), _read(args.target_file))
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
