# M5 — Analysis

## Plan of record

`notebooks/context_engineering_analysis.ipynb`: build the fixture, mine it,
run the comparison harness (mock backend, offline) inside the notebook, and
plot (1) catalog statistics (gold-count histogram, changed-lines), (2) method
comparison at a fixed budget (gold recall + gate pass per method), (3) the
gold-recall-vs-budget curve for a few methods, (4) the two null arms
(skeleton-vs-drop gate, guidance-vs-full NLL). Every figure from executed
harness output, no hand-typed numbers. Conclusions written as
claims-with-evidence; the mock caveat and the nulls stated plainly. Executed
top-to-bottom with `nbconvert --execute`, outputs saved.

## What was built

- `notebooks/context_engineering_analysis.ipynb`, executed end-to-end with
  `.venv-analysis` (`jupyter nbconvert --execute --inplace`): 6 code cells, 4
  figures, all outputs saved into the file. It builds the fixture, mines it
  via the real miner (subprocess `sys.executable`), loads the packs, runs
  `run_comparison`, and plots from the returned aggregates — no typed numbers.
- Opens with the mock caveat and the 8-case scope statement, so no reader
  mistakes mechanism for findings.

## How verified (executed 2026-08-05)

`nbconvert --execute` completed; cell-output scan shows 4 `image/png` figures
and zero error outputs. Key executed values (pulled from the saved outputs):

- mining: 5 candidate commits → 8 cases, zero-gold 0/8, `apply --check` 8/8,
  gold-count histogram {2: 6, 3: 2};
- method comparison @ budget 40: methods separate on gold recall/precision,
  `random_budget` at the floor;
- gold recall rises monotonically with budget (20→160) for all plotted
  methods; obligation-aware curve reaches full recall no later than blind;
- null arms (arm × metric table, verbatim from output):
  drop gate 0.000, skeleton 0.625, full 1.000 (Sam-Bodden separation);
  guidance edit-NLL 102.375 = full 102.375 (ETH null: no gain).

The written conclusions match these numbers exactly (they are read from the
same executed cells).

## Review findings (hostile pass) and fixes

1. **Claim/output drift risk**: conclusions were drafted before execution;
   after running I cross-checked each claim against the saved cell outputs
   (mining stats, the arm×metric table) — all match. The "ours ties blind more
   than it beats it" sentence was kept because the executed cell 2 shows
   exactly that on the fixture, rather than overclaiming a win.
2. **Reproducibility**: the notebook rebuilds the fixture and re-mines every
   run (deterministic SHAs), so it never depends on a pre-existing `runs/`
   dir; a clean checkout executes it top-to-bottom.
3. Noted, not fixed: figures show mock magnitudes; each title says "(mock
   backend, fixture)" and the header/conclusions repeat the caveat, so a
   figure lifted out of context still carries the disclaimer in its title.

## Limitations / next

- Mock + 8 synthetic cases: mechanism validated, not effect sizes. The
  load-bearing experiment (does teacher-forced PPL track context quality on
  mined kernel cases) is pre-registered and unrun.
- The real run is one command per stage against a linux.git clone with the HF
  backend — see STATUS.md for the exact sequence and the user-gated decisions
  (full mine, committing mined data, freezing the pre-registration).
