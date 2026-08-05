# M1 — Data layer + synthetic fixture generator

## Plan of record

Move the case-pack loader out of the harness into `ctxlab/dataset.py`
(`load_case`/`load_cases` become the single canonical implementation; the
harness imports them — one contract, no drift), add `validate_case_dir` (schema
errors as a list, not exceptions) and `catalog_stats`. New
`ctxlab/fixtures.py`: deterministic throwaway git repos (fixed commit dates →
stable SHAs) covering api_rename, struct_field_change, treewide_sed,
comment_only, pure_rename; each scenario declares its *expected* mining
outcome (cases in / junk out / exact gold sets). New `tools/run_ctxlab_checks.py`
executes fixture → miner → assertions → loader/validator round-trip and exits
nonzero on any failure; this is the standing regression gate for the loop.

## What was built

- `ctxlab/dataset.py` (182 ln): `load_case`/`load_cases` (moved verbatim from
  the harness, plus `meta`/`gold_items`/`distractor_items` in the return),
  `evidence_text`, `case_dirs`, `validate_case_dir` (missing files, required
  meta keys, `evaluation_only: true`, evidence↔meta gold agreement, gold ⊆
  reference.diff), `catalog_stats` (recomputed from packs, not trusted from
  catalog.json).
- `ctxlab/fixtures.py` (168 ln): one repo, five scenario commits over a common
  base; pinned author/dates → SHAs, case ids and seeded sampling are identical
  across machines. Returns the expected outcome (candidate count, exact gold
  per case, treewide scale/sample, subjects that must be filtered).
- `tools/build_ctx_fixture.py` (38 ln): thin CLI over the generator.
- `tools/run_ctxlab_checks.py` (158 ln): 15 assertions, exit-nonzero gate.
- `tools/run_ppl_signal_experiment.py` now imports `load_cases` from
  `ctxlab.dataset`; its local loader (80 lines) deleted.
- Miner fix that the struct_field scenario forced: `FIELD_DECL_RE` in
  `ctxlab/mining.py` — a struct-field rename hunk contains no call/define/
  struct-tag token, so the old regex set extracted zero kernel symbols and the
  case was silently dropped. Declarator names (`int payload_len;`) are now
  extracted.

## How verified (executed 2026-08-05)

`python3 tools/run_ctxlab_checks.py` — 15/15 PASS:

- census: 5 candidate commits (all five scenarios touch both prefix groups);
- full mine: exactly 8 cases (2 api_rename + 1 struct_field + 5 sampled of 7
  treewide), junk subjects absent;
- gold labels exact: api_rename `[ALPHA_SETUP_SYNC, alpha_init, alpha_setup]`,
  struct_field `[legacy_len, payload_len]`, treewide
  `[BETA_TIMEOUT_JIFFIES, BETA_TIMEOUT_MS]` ×5 with `treewide_scale=7`;
- zero-gold rate 0.0; `git apply --check` 8/8; `validate_case_dir` clean on
  all packs; loader round-trip 8/8 with non-empty gold/old-source;
  `catalog_stats` agrees with catalog.json;
- harness gate profile on the mined fixture: gold 100%/1.000 coverage vs none
  0%/0.188 (real separation, unlike the M0 two-case fixture);
- end-to-end determinism: rebuild fixture + re-mine → identical case-id sets.

Backward compatibility: harness over `datasets/intree_mini_v0 --max-cases 2`
(intree pack shape) produces the same gate rows as before the loader move.

## Review findings (hostile pass) and fixes

1. **struct_field blind spot** (found by writing the fixture *before* trusting
   the miner): field-rename hunks yielded zero kernel symbols → case dropped.
   Fixed with `FIELD_DECL_RE`; the checks pin the exact resulting gold set.
2. **Unused `json` import** left in the harness after the loader move —
   removed (AST scan of imports vs uses).
3. Fixture cosmetics: the api_rename caller edit passed the flags argument in
   the wrong position relative to the declared signature; irrelevant to the
   text-level miner but fixed for realism (`alpha_setup(&aN_dev,
   ALPHA_SETUP_SYNC)`).
4. Noted, not fixed: `validate_case_dir` checks mined packs only (intree packs
   have a different meta schema — out of scope until intree data is used
   beyond smoke); `FIELD_DECL_RE` can pick up local variable declarations from
   .c kernel-side hunks (headers dominate the kernel prefix set; the stoplist
   and the both-diffs intersection bound the noise, and the zero-gold gauge
   would surface regressions).

## Limitations / next

- Fixture drivers are miniature; no fixture yet for a rename+modify driver
  file (similarity < 100%) — worth adding when a method needs it.
- `catalog_stats` is intentionally minimal; M5 notebooks will extend the
  reporting side.
- Next (M2): metric layer — `score(context, case)` with the expressibility
  gate and a pluggable PPL backend including a deterministic mock, so M3/M4
  run fully offline.
