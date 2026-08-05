# M0 — Benchmark miner (atomic mode)

## Plan of record

Ship `tools/mine_migration_bench.py` as a thin CLI over a new `tools/ctxlab/`
package (the first draft was 552 lines, over the 500-line cap): `gitio.py`
(subprocess git), `textutil.py` (identifier/comment logic, canonical home of
what `ppl_context_scorer.py` had inlined), `diffs.py` (diff splitting/line
mapping), `mining.py` (commit scan, gold/distractor labeling, case-pack
writing, catalog). Output schema is pinned to
`tools/run_ppl_signal_experiment.py::load_case`. Validate on a hand-rolled
synthetic git repo with one known atomic migration commit plus junk commits
(comment-only, pure rename); the full fixture generator with all scenarios is
M1. Sanity gauges (0-gold share, `git apply --check` sample, random dump) are
part of the run output. Mined data goes to `runs/`, never into git.

## What was built

- `tools/ctxlab/` package: `gitio.run_git`, `textutil.{identifiers,
  strip_comments_and_strings, C_KEYWORDS}`, `diffs.{parse_diff_changed_lines,
  split_diff_by_file, changed_content_lines}`, `mining.{scan_candidate_commits,
  kernel_symbols_from_segments, symbol_definition_diff,
  extract_cases_for_commit, write_case_pack, apply_check_sample,
  write_catalog}`. `run_git`/`parse_diff_changed_lines` are ports of the
  helpers in `tools/build_candidate_small_dataset.py`.
- `tools/mine_migration_bench.py`: 146-line CLI (scan → extract → write →
  gauges), env-overridable (`REPO`/`OLD_REF`/`NEW_REF`/`OUT_DIR`/`JOBS`),
  `--list-only` census mode, default `--out-dir runs/mined_migration_bench`
  (git-ignored: mined data never enters the repo).
- `tools/ppl_context_scorer.py` now imports the text utilities from
  `ctxlab.textutil` instead of inlining them (same public names).

## How verified (all commands executed on 2026-08-05)

Fixture: throwaway git repo (scratchpad) with 4 commits — base; a treewide
migration (`foo_helper_init` → `foo_helper_start(+flags)` +
`#define FOO_START_DEFAULT`, 3 driver callers adapted in the same commit); a
comment-only header+driver touch; a pure driver-file rename plus an unrelated
header decl.

- `--list-only base..tip`: 3 candidate commits (all touch both prefix groups).
- Full run, `--max-cases-per-commit 2`: exactly 2 case packs, both from the
  migration commit; comment-only commit filtered (0 non-comment changed
  lines < min 2), pure rename filtered (`similarity index 100%`);
  `treewide_scale=3` recorded; gold labels exact:
  `[FOO_START_DEFAULT, foo_helper_init, foo_helper_start]`; zero-gold rate 0;
  `git apply --check` 2/2.
- Contract check: `run_ppl_signal_experiment.py --scorer expressibility-only`
  over the mined dir loads both packs and yields the expected gate profile —
  gold 100% coverage, none 41.7%/0% pass. (`distractors_only`/`random` also
  read 100% here: with a single-commit two-case fixture, random borrows an
  identical gold set and the only distractor `foo_ctl` is already named in the
  old source. Property of this fixture, not of the gate; M1's multi-scenario
  fixtures give these conditions separation.)
- Determinism: two identical runs produce identical case-id sets (`diff` of
  `ls cases/` clean).
- Line caps: miner CLI 146, mining.py 342, all ctxlab files ≤ 342,
  scorer 351 — all under 500.

## Review findings (hostile pass) and fixes

1. **NUL byte in argv** — `--pretty=format:…%x00…` was passed as a literal
   `\x00` in the git argv; `exec` rejects embedded NULs, so the very first
   real invocation crashed. Caught by the first fixture run (the earlier
   draft had never been executed against any repo). Fixed: pass literal
   `%x01`/`%x02` and let git expand them in output. (mining.py)
2. **Non-reproducible sampling** — per-commit RNG was seeded with
   `hash((seed, sha))`; `str.__hash__` is salted per process, so
   `--max-cases-per-commit` sampling differed between runs with the same
   seed. Fixed: seed from `int(sha[:16], 16) ^ seed`; verified by the
   determinism diff above. (mine_migration_bench.py)
3. **Mispaired evidence locations** — `_evidence_items` zipped a list of
   unique paths against a list of sample lines, attributing lines to wrong
   files. Fixed: record `(path, line)` pairs at extraction time. (mining.py)
4. Noted, not fixed (acceptable): with `--max-cases`, `ThreadPoolExecutor.map`
   still processes all candidate commits in the background before the
   consumer breaks — wasted work on a huge scan, output still correct.
   `kernel_side.diff` is duplicated across cases of the same commit
   (disk-cheap, keeps packs self-contained).

## Limitations / next

- Gold labeling is token-intersection: misses typedef/macro indirection,
  over-counts symbols named in both diffs coincidentally. Gauged by the
  zero-gold rate and (from M1) exact-label fixture assertions.
- `--mode split-commits` is a documented, refused stub by design.
- Not yet run against a real linux.git (out of scope for this loop); the
  one-command real run is
  `REPO=… tools/mine_migration_bench.py --old-ref v6.19 --new-ref v7.0-rc1`.
- Next (M1): fixture generator covering API rename / struct field change /
  treewide sed / comment-only / pure rename as a reusable module + dataset
  loader/validator, then assert exact expected-case sets against it.
