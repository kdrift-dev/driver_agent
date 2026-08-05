# M4 — Comparison harness

## Plan of record

`ctxlab/compare.py` (library): `run_comparison(dataset, methods, budgets,
backend, ablations)` → per-(case, method, budget, ablation) rows via
`make_method` + `evaluate_selection`, aggregated into a unified table.
`tools/run_method_comparison.py` (thin CLI): pick dataset dir, method subset,
budget list, backend (mock default so it runs offline), out dir; writes
`comparison_rows.jsonl` (one row per cell) + `comparison_summary.md`
(per-method means: tokens kept, compression, PPL delta vs none, gate pass,
gold recall/precision). Two ablation arms from the deck: the ETH null
(pre_spec/pre_reasoner sections on/off — modeled here as guidance-context
on/off appended to the prompt) and the Sam-Bodden null transfer (skeleton-tier
vs drop — evidence rendered as one-line skeletons vs full text). A new check
runs a 2-method × 2-budget comparison on the fixture and asserts the table
shape + that within-budget holds everywhere.

## What was built

- `ctxlab/compare.py` (108 ln): `render_arm` (full / skeleton / drop /
  guidance), `run_comparison` (methods × budgets × arms → rows),
  `aggregate` (per method/budget/arm means + gate-pass and within-budget
  rates), `summary_markdown`. Recall/precision are always defined by the
  chunks the method picked; only the *rendering* changes per arm.
- `evaluate_selection` gained a `context_text` override so an arm can supply
  its own rendered context without a second scoring path.
- `tools/run_method_comparison.py` (86 ln): CLI, `--methods/--budgets/--arms/
  --scorer` (mock default, offline), writes `comparison_rows.jsonl`,
  `comparison_summary.md`, `comparison_aggregate.json`.
- `run_ctxlab_checks.py`: +5 checks (now 27 total).

### The two ablation arms

- **Sam-Bodden transfer** (skeleton vs drop): the `drop` arm supplies no
  evidence, `skeleton` supplies each evidence chunk's first line, `full`
  supplies the whole chunk. The gate — an independent, non-PPL signal —
  separates them: dropping evidence fails the gate where full passes it.
- **ETH generated-guidance null**: the `guidance` arm appends a generated
  block restating the obligation symbols. Under the mock it produces the ETH
  direction — the block costs tokens but yields no NLL gain over `full`
  (its symbols are already covered), i.e. generated guidance is not free.

## How verified (executed 2026-08-05)

Full run on the fixture-mined dataset (8 cases × 8 methods × 2 budgets × 4
arms = 512 rows): all cells within budget; arms all present. Excerpt
(`comparison_summary.md`):

- `errprune` budget 30: gate 1.00 (full/guidance), 0.62 (skeleton), 0.00
  (drop) — the skeleton-vs-drop separation;
- `guidance` NLL equals `full` NLL (102.4) everywhere — the ETH null under the
  mock;
- budget 30→80 raises gold recall (e.g. errprune 0.77→1.00) as more evidence
  fits — the recall-vs-budget curve M5 will plot.

`python3 tools/run_ctxlab_checks.py` — 27/27 PASS, new:

- comparison row count exact (128 for the 2×2×4 fixture check);
- all comparison cells within budget;
- **null arm: drop gate ≤ full gate** (0.0 ≤ 1.0);
- **ETH null: guidance NLL not better than full** (102.375 = 102.375).

Line caps: compare.py 108, CLI 86, checks 237, evaluation 55 — all < 500.
Unused-import scan clean (only the `annotations` future-import false positive).

## Review findings (hostile pass) and fixes

1. **Recall/rendering coupling risk**: an early `render_arm` design recomputed
   picks per arm, which would have conflated "what the method selected" with
   "how it was shown". Fixed by keeping `select` once per (method, budget) and
   only varying the rendered `context_text` — recall/precision stay the
   method's, gate/NLL move with the arm. This is what makes the nulls
   interpretable.
2. **Aggregate None-handling**: `compression`/`edit_nll` can be None (empty
   selection, no backend); `_mean` filters None so a single empty cell does
   not blank a whole column.
3. Noted, not fixed: the mock makes `guidance` exactly equal to `full`
   (bag-of-identifiers ignores the added block's phrasing); a real model may
   show the ETH *negative* effect (guidance slightly worse). The arm and its
   plumbing are correct; the sign is a real-model question, flagged for M5's
   written limitations.

## Limitations / next

- Numbers are mock orderings on a synthetic 8-case set — mechanism, not
  findings. Real magnitudes need the HF backend on mined data (user-gated).
- Only two ablation arms are wired; more (e.g. locus-only, evidence-only) are
  a one-line addition to `ARMS` when a question needs them.
- Next (M5): notebooks that execute this harness, plot method comparison and
  gold-recall-vs-budget curves + catalog statistics, and write the
  conclusions as claims-with-evidence with the nulls reported as nulls.
