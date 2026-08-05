# M2 — Metric layer

## Plan of record

Restructure scoring into the package: `ctxlab/backends.py` (NLL backends —
HF teacher-forced, OpenRouter echo, deterministic mock — plus the sha256 disk
cache and provider-failure semantics, all moved from `ppl_context_scorer.py`)
and `ctxlab/metrics.py` (`score_context(context, case, backend) -> {ppl, gate}`
plus the expressibility gate, moved likewise). `tools/ppl_context_scorer.py`
shrinks to a CLI + backward-compat re-exports so the existing harness keeps
working unchanged. The mock backend is deterministic AND informative: target
tokens found in the prompt cost less NLL than unseen ones, so gold context
must beat empty context offline — this ordering becomes a new assertion in
`run_ctxlab_checks.py`.

## What was built

- `ctxlab/backends.py` (234 ln): `ScoreCache` (sha256 disk cache),
  `cached_score`, `ProviderFailure`, `MockBackend`, `HFTeacherForcedBackend`,
  `OpenRouterEchoBackend`, `make_backend(kind)`. HF/OpenRouter logic moved
  verbatim from the old scorer; the mock is new.
- `ctxlab/metrics.py` (90 ln): `expressibility_check`, `build_prompt`,
  `score_context(context_text, case, backend=None, cache=None)` returning
  `{gate, ppl, status}` — the single entry point M3/M4 will call. `PROMPT_TEMPLATE`
  now lives here (one copy).
- `tools/ppl_context_scorer.py` (95 ln, was 351): one-shot CLI + re-exports
  (`ProviderFailure`, `ScoreCache`, `cached_score`, `make_scorer` alias,
  `expressibility_check`) so nothing downstream breaks; `--scorer mock` added.
- `tools/run_ppl_signal_experiment.py`: scoring loop now calls
  `score_context`; its local `PROMPT_TEMPLATE` and manual gate/PPL plumbing
  deleted; `--scorer mock` accepted.

### The mock backend cost model

`MockBackend` tokenizes the target into identifiers; each target token costs
`SEEN_NLL=1.0` if it appears in the prompt, `UNSEEN_NLL=4.0` otherwise. This
reproduces the one LM property the experiments depend on — context that
carries what the edit needs lowers the loss — with exact reproducibility and
zero dependencies, so M3/M4 run fully offline. Only *orderings* under this
model are meaningful; absolute values are not, and reports must say "mock".

## How verified (executed 2026-08-05)

`python3 tools/run_ctxlab_checks.py` — now 17/17 PASS, including two new:

- **mock backend: gold NLL < none NLL on every case** — 8/8 on the fixture
  (offline PPL signal exists by construction);
- **mock backend determinism** — identical `nll_sum` (108.0) on repeat.

Backward compatibility:

- scorer CLI `--scorer expressibility-only` over an intree pack prints the
  same gate JSON as before the refactor;
- harness `--scorer mock` over `datasets/intree_mini_v0 --max-cases 3`:
  PPL(gold) < PPL(none) 3/3, < PPL(distractors) 3/3, < PPL(random) 2/3 (the
  random arm borrows another case's gold, which can overlap — expected on 3
  cases);
- harness gate profile on the fixture unchanged (gold 100%/1.000, none 0%).

Line caps: backends 234, metrics 90, scorer CLI 95, harness 249, checks 176 —
all under 500.

## Review findings (hostile pass) and fixes

1. **Unused `Optional` import** in metrics.py after the type signatures
   settled — removed (AST + regex token scan across all four touched files;
   `annotations` future-import is a false positive, kept).
2. **Interface-drift risk**: the harness had its own `PROMPT_TEMPLATE` and gate
   call, which would silently diverge from what M3/M4 use. Collapsed onto
   `score_context`/`build_prompt` so every consumer scores identically — the
   whole point of the metric layer.
3. **provider-failure propagation**: verified `score_context` still returns a
   `status` string (never raises) so a dead provider excludes a row rather
   than crashing a sweep — the OpenRouter path is unreachable offline but the
   contract is preserved for the real run.
4. Noted, not fixed: `MockBackend` ignores token order and frequency (bag of
   identifiers). That is deliberate — it must not become a second real model;
   its only job is a monotone gold>none signal for offline plumbing tests.

## Limitations / next

- Mock ≠ real ranking: it cannot validate the *hypothesis* (does PPL track
  context quality), only that the plumbing carries a signal. The real HF run
  is still the pre-registered test and remains out of scope for this loop.
- Next (M3): the method zoo — every compared approach as a module under a
  common `select(case, budget) -> ranked chunks` interface, scored through
  `score_context` with the mock backend so the whole comparison runs offline.
