# M3 — Method zoo

## Plan of record

One common interface in `ctxlab/methods/base.py`: a `Chunk` (id, text, kind,
symbols) and `Method.select(case, budget) -> Selection` returning ranked
chunks whose total token cost fits `budget`. Candidate chunks come from one
shared splitter (`ctxlab/chunks.py`): the reference edit's own edit-locus
slices are always kept verbatim; kernel evidence items (gold + distractors)
are the selectable context chunks. Each method is its own <150-line module
under `ctxlab/methods/`: trunc_baseline, random_budget, longcodezip,
keepdrop_dd, errprune, shapley_probe, static_epam, ours. A registry maps name
→ instance. `ours` is also exposed as `context_pruner` behind a default-OFF
flag; with the flag off the agent pipeline is byte-for-byte unchanged. Verify
each method on the fixture through the mock backend: budget respected, edit
locus always present, gold recall/precision computed, `ours` ≥ trunc/random on
gold recall.

## What was built

- `ctxlab/chunks.py` (92 ln): `Chunk` (identity-hashed, token cost via the
  metric proxy), `edit_locus_slices` (functions the reference edit touches,
  kept verbatim; falls back to whole source so the locus is never empty),
  `evidence_chunks` (one selectable chunk per gold/distractor item),
  `obligation_symbols` (gold labels = machine-derived obligations for mined
  cases).
- `ctxlab/methods/base.py` (58 ln): `Selection` + `Method.select` — charges
  the locus first, then knapsacks ranked evidence into the remaining budget.
  Methods override only `rank_evidence` (or `select` when they need an
  oracle loop).
- Eight methods, one module family:
  - `simple.py` (95 ln): `TruncBaseline` (blind order), `RandomBudget`
    (deterministic seeded shuffle = noise floor), `ErrPrune` (keep evidence
    whose symbols surface in the edit locus — the identifiers a diagnostic
    names), `StaticEpam` (rank by identifier overlap with old source),
    `ShapleyProbe` (per-chunk marginal edit-NLL drop).
  - `longcodezip.py` (56 ln): conditional-PPL value-density knapsack.
  - `keepdrop_dd.py` (64 ln): delta-debugging minimal sufficient context
    against the backend NLL oracle proxy.
  - `ours.py` (64 ln): obligation-anchored submodular greedy coverage.
- `driver_agent_pregen/context_pruner.py` (91 ln) + `context_pruner_coeffs.json`:
  the pipeline-side, boundary-safe scorer. `marginal_gain` and `load_coeffs`
  are shared: `ours` imports them so the paper-facing selector and the
  pipeline hook rank identically. Direction of the dependency is
  eval→pipeline (allowed); the pipeline never imports ctxlab.
- `ctxlab/evaluation.py` (51 ln): `evaluate_selection` → tokens kept, within
  budget, compression, gold recall/precision, obligation recall, gate,
  edit-NLL.
- Default-OFF hook in `patch_generation.py::_shrink_patch_input_payload`:
  guarded by `DRIVER_AGENT_CONTEXT_PRUNER`; when set it reorders read-only
  source blocks by obligation overlap (editable blocks stay first,
  non-destructive) before the existing budget shrink; when unset the block is
  skipped entirely.

## How verified (executed 2026-08-05)

`python3 tools/run_ctxlab_checks.py` — 22/22 PASS, new in M3:

- **budget respected + locus kept** for all 8 methods across all 8 fixture
  cases;
- **ours gold-recall ≥ random_budget** (0.77 vs 0.69) and **≥ trunc_baseline**
  (0.77 vs 0.77) at budget 40 through the mock backend;
- **context_pruner**: editable block stays first and the obligation-carrying
  read-only block outranks noise; non-destructive (no block dropped).

Pipeline-hook inertness, verified directly (`_shrink_patch_input_payload` with
flag off vs on, budget effectively infinite): flag OFF → no `context_pruner`
note, ordering is exactly the pre-existing `_prioritize_patch_sources` result;
flag ON → the obligation block moves ahead of the noise block and a note is
recorded. The only OFF→ON difference is my hook's effect. Boundary grep:
`driver_agent_pregen/` imports no ctxlab/tools code.

All new files ≤ 95 lines; `context_pruner.py` 91; compile clean.

## Review findings (hostile pass) and fixes

1. **Unhashable Chunk** — the `@dataclass` auto-`__eq__` set `__hash__=None`,
   so `{chunk ...}` in `evaluate_selection` crashed on the first probe. Fixed
   with `@dataclass(eq=False)` (identity semantics, which the DD `is not`
   checks already assume).
2. **Duplicate coefficients** — `ours_coeffs.json` under ctxlab duplicated the
   pipeline coeffs and would drift. Deleted; `ours` now loads
   `driver_agent_pregen/context_pruner_coeffs.json` via the shared
   `load_coeffs`, and the module docstring points there.
3. **Boundary risk** — putting the pruner in ctxlab would have forced the
   pipeline to import an eval-only package. Inverted: the boundary-safe scorer
   lives in `driver_agent_pregen/context_pruner.py` (obligations only, no
   reference diffs), and `ours` imports *from* it. Grep confirms the pipeline
   imports nothing under ctxlab/tools.
4. Noted, not fixed: on the tiny fixture ours ties trunc on gold recall
   (budget fits at most one evidence chunk in most cases, so ranking rarely
   changes the pick); the separation is expected to widen on real multi-symbol
   cases with distractors — M4/M5 aggregate this. `keepdrop_dd` can drop a
   gold chunk when a second gold chunk covers the same mock tokens (the mock
   oracle sees no NLL change) — a mock artifact, not a method bug; the real
   build oracle would not.

## Limitations / next

- Method separation is under-resolved on the 8-case fixture; the point here is
  a correct, uniform interface and computed metrics, not final numbers.
- `context_pruner` reorders but never drops (deferring all dropping to the
  existing budget logic); a dropping variant is a later, riskier step.
- Coefficients are placeholders until calibrated on mined migrations (a
  user-gated real-data step).
- Next (M4): comparison harness — run any subset of methods over a dataset,
  emit the unified table + per-case JSONL, plus the ETH (pre_spec/pre_reasoner
  on/off) and Sam-Bodden (skeleton vs drop) ablation arms.
