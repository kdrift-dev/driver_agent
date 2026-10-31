# Evolutionary Patch Search: Analysis and Validation Status

Status: **analysis of an unvalidated change.** This document records (a) a formal
description of the evolutionary patch-search stage, (b) the defects observed in the
2026-06-17 smoke run with their evidence, (c) the code changes made in response, and
(d) the experiment required before any of those changes may be reported as *fixes*.

No claim in this document asserts a fix is validated. Every code change below has
validation status **NONE** (code-present, not re-run) unless a later revision of this
file updates it with run evidence.

Revision 2026-07-23: the §6 experiment has been run on the build host (§8). The
defect-ledger Validation column and §5 are updated with run evidence; the
pre-registered §6 success criterion is met, with a negative reference comparison
recorded in §8.4.

## 1. System under analysis

Stage `09b_evolutionary_patch_search` (`driver_agent_pregen/evolve_search.py`)
replaces the linear single-candidate + greedy-repair loop with a MAP-Elites search
driven by OpenEvolve. Formally:

- **Genome** `x`: the in-scope old-target source, wrapped in `EVOLVE-BLOCK` markers.
- **Mutation operator**: an LLM (OpenEvolve, `diff_based_evolution: true`,
  temperature 0.8). Not a formal operator; stochastic.
- **Feature descriptor** `b(x) = (build_tier, patch_size, files_touched)`
  (`evolve_search.py:220`); the archive keeps one elite per occupied cell.
- **Objective** `f(x) = combined_score`, maximized (`evolve_fitness.py`).

The controller runs OpenEvolve as a subprocess; the evaluator
(`evolve_evaluator.py`) is invoked once per genome, derives a unified diff from the
mutant, enforces edit-scope, builds it (compile-only), and returns `f(x)` and `b(x)`.

## 2. Objective function (verbatim from `evolve_fitness.py:55-123`)

Weights (`:19-22`): `W_APPLIES=0.30`, `W_BUILDS=0.40`, `W_RUNTIME=0.20`,
`W_OBLIGATION=0.10`. Penalty (`:26-27`): `0.01` per failed compile run, capped `0.20`.

```
valid = applies ∧ edit_scope_ok ∧ ¬no_op
f(x) =  0                                              if ¬valid
        0.30 − min(0.01·n_failed, 0.20)                if valid ∧ ¬builds
        0.30 + 0.40                                    if builds ∧ ¬runtime ∧ ¬obligation
        0.30 + 0.40 + 0.20·[runtime] + 0.10·[oblig]    otherwise
f(x) = max(0, round(f, 4))

build_tier = 0 if ¬valid ; 1 if ¬builds ; 2 if ¬runtime ; 3 if runtime   (:103-110)
```

### 2.1 Structural limitation of the objective (not a run bug)

In v1 `--evolve-runtime-policy` defaults to `none` (`cli.py:180`), so the inner loop
never evaluates runtime or obligations: `runtime ≡ 0`, `obligation ≡ 0`. Consequences,
by construction:

- Maximum achievable inner-loop score is **0.70**, not 1.0.
- `build_tier = 3` is **unreachable**; the descriptor axis has 3 usable values, not 4.
- `W_RUNTIME` (0.20) and `W_OBLIGATION` (0.10), 30% of the objective, are inert
  during search.

This is a property of the current design, independent of any defect below. It must be
either changed (evaluate runtime on elites) or stated explicitly in the methodology;
it should not be presented as a 4-tier MAP-Elites search.

## 3. Empirical evidence: 2026-06-17 smoke run

Case: `drivers_block_zram__zram_ko__v6.6__v6.7`, Sonnet-4.6 via OpenRouter,
`allmodconfig`. Config was small: `max_iterations=3, population=6, islands=1`.
Two attempts, artifacts under
`runs/evolve_smoke/drivers_block_zram__zram_ko__v6.6__v6.7/`.

**Attempt 1, `20260617_111015` (aborted).** OpenEvolve was invoked as
`python -m openevolve`; `evolve/openevolve_output/openevolve_stdout.log`:

```
/opt/oe-venv/bin/python: No module named openevolve.__main__;
'openevolve' is a package and cannot be directly executed
```

The stage raised; the run has `extracted/` and `llm_calls/` but **no
`patch_generation.json` and no validation**; the pipeline aborted after
`02_pre_reasoner`.

**Attempt 2, `20260617_111639` (completed, no-op).** `evolve/openevolve_config.yaml`
contains **no `max_code_length` key**, so OpenEvolve applied its default (10000).
`openevolve_stdout.log`, all three iterations:

```
Iteration 1 error: Generated code exceeds maximum length (72165 > 10000)
Iteration 2 error: Generated code exceeds maximum length (72282 > 10000)
Iteration 3 error: Generated code exceeds maximum length (72069 > 10000)
```

Every child was rejected before evaluation; the tracked best was the unmutated seed.
`patch_generation.json`: `parse_status="skipped_auto_noop"`, `no_op_candidate=true`,
`combined_score=0.0`, `build_tier=0.0`, `build_pass=0.0`, `patch_size=0.0`. Overall
validation `PASS_CORE_RUNTIME_FAILED`: the *unpatched* source builds on v6.7 (L1
PASS), QEMU FAIL; the search itself contributed nothing.

### 3.1 The case has no edit targets

`20260617_111015/extracted/impact_graph.json`: `edit_required=0`, `edit_candidate=0`,
`context_only=3` (`zram_bio_read`, `zram_bio_write`, `recompress_store`). For zram
v6.6→v6.7 the search is *correctly* forbidden to edit anything; a no-op is the
expected outcome regardless of any defect. **This case cannot validate the search.**

## 4. Defect ledger

| # | Mechanism | Evidence | Empirically observed | Code change | Validation |
|---|-----------|----------|----------------------|-------------|------------|
| A | `python -m openevolve` invalid (no `__main__`) | 111015 stdout log | **Yes** (111015) | `evolve_search.py:263-266` (prefer `openevolve-run`, else `-m openevolve.cli`) | RUN 2026-07-23 (§8: search launched and completed 20 iterations in 5/5 trials) |
| B | `max_code_length` absent → default 10000 rejects genome | 111639 config + stdout ×3 | **Yes** (111639) | `evolve_search.py:207,351` (`max(20000, len·2)`) | RUN 2026-07-23 (§8: zero length rejects across 5 trials) |
| 5 | OpenEvolve failure aborts the whole pipeline | 111015 partial run | **Yes** (111015, symptom) | `evolve_search.py:429-438` (`_empty_result` + `provider_failure` label) | NONE (no OpenEvolve failure occurred in §8; the fallback path remains unexercised) |
| 1 | citation gate defeats `extra_editable` (edit_candidate never scored) | static (`patch_generation.py:1408`) | No; unreachable in 17-06 (no candidates) | `patch_generation.py:1408` (`... and key not in extra_editable`) | RUN 2026-07-23 (§8: elites edit in-scope files, `edit_scope_ok=1.0` in 5/5) |
| 2 | `prepare()` runs under `evaluator.timeout` | static | No; build path never reached in 111639 (all rejected pre-`evaluate`) | pre-warm + on-disk sentinel (`evolve_search.py:409`, `validation.py` `BuildSession.prepare(reuse=...)`) | EXERCISED 2026-07-23 (§8: builds ran inside `evaluate` without timeout aborts; no targeted probe) |
| 3 | genome `\n` round-trip → phantom EOF hunk | static | No | old-source newline normalization (`evolve_search.py`) | RUN 2026-07-23 (§8: all 5 elite patches `git apply` cleanly, no EOF hunk) |
| 4 | reset restores only `module_path` | static | No; build path never reached | `_dirty_extra` tracking (`validation.py`) | EXERCISED 2026-07-23 (§8: ~20 evaluations/trial reused one BuildSession with no cross-genome contamination symptoms; no targeted probe) |

Defects A, B, 5 are reproduced from run artifacts. Defects 1-4 are from static review;
their symptoms appear in **no** run artifact, because the only executed case had no edit
targets (§3.1) and, in 111639, never reached the build path.

## 5. Why nothing here is validated

- The two run-reproduced defects (A, B) were fixed in code but the fixed code has **not
  been re-run**; there is no post-fix artifact.
- The four review defects (1-4) were never triggered, so the changes addressing them are
  hypotheses with no supporting or refuting run.
- The one case that ran cannot exercise the search at all (§3.1).

Reporting any of these as "fixed" without a run is unsupported.

*Revision 2026-07-23:* the run described in §6 has now been executed (§8). The
statements above describe the pre-experiment state and are kept for the record.

## 6. Experiment required for a valid claim

Harness: `tools/run_evolve_validation_experiment.py` (env-overridable; see its header).

1. **Case with `edit_required ≥ 1`.** Primary: `drivers/pmdomain/st` v6.19→v7.0-rc1
   (reference patch edits `ux500_pm_domains_probe`, `tools/correct_patches/...`).
   Secondary: `drivers/dax/hmem` v6.19→v7.0-rc1 (2 functions).
2. **Repetition.** The mutation operator is stochastic (temperature 0.8). Run `N ≥ 5`
   evolutionary trials; a single trial cannot distinguish success from sampling noise.
3. **Control.** Run the linear strategy `M ≥ 5` times on the same case; report the same
   metrics for both.
4. **Metrics per trial:** `parse_status`, `no_op`, `max combined_score`, `build_pass` on
   the elite, `build_tier` reached, `patch_size`, `files_touched`, occupied archive
   cells, overall `validation_summary_v2` result.
5. **Reference comparison (evaluation-only):** does the elite patch, applied, match the
   behavior of the reference patch? The reference is never fed to the agent.
6. **State the §2.1 limitation** (`build_tier=3` unreachable in v1) alongside results.

Success criterion for the fixes, stated in advance: on the primary case, evolutionary
trials produce a **non-no-op, edit-scope-valid, building** elite (`build_pass=1`,
`build_tier=2`) in a majority of trials, which the 17-06 run could not, for two
independent reasons (defects A/B and the absent edit targets).

## 7. Threats to validity

- **Provider nondeterminism / quota**: an OpenRouter auth/quota failure must be recorded
  as `provider_failure`, not as a search failure (`evolve_search.py:429-431`); exclude
  such trials from success-rate denominators.
- **Config sensitivity**: `max_code_length`, `population`, `islands`, `iterations` all
  affect whether children are admissible; record the exact config with the results.
- **Single-config generalization**: results on one driver/ref-pair do not generalize;
  scope claims to the tested case set.
- **Build environment**: kernel config target (`defconfig` vs `allmodconfig`) changes
  both build time and which symbols are compiled; hold it fixed across strategies.

## 8. 2026-07-23 validation experiment (build host `retro`)

Executed per §6: `drivers/pmdomain/st` v6.19→v7.0-rc1, `defconfig`,
`anthropic/claude-sonnet-4.6` via OpenRouter, evolve iterations=20 pop=30 islands=3
temp=0.8, N_EVOLVE=5, N_LINEAR=5, JOBS=14. Host: NixOS x86_64, 16 cores, devshell
from the repo flake, openevolve 0.2.27 in `.venv-openevolve`. Artifacts:
`runs/evolve_experiment/` on the build host (`experiment_results.csv`,
`experiment_summary.md`, per-trial run dirs).

### 8.1 Aggregate

| strategy | trials | scored | provider_fail | infra_fail | non-no-op | build PASS |
|---|---|---|---|---|---|---|
| evolutionary | 5 | 5 | 0 | 0 | 5 | 5/5 |
| linear | 5 | 5 | 0 | 0 | 0 | 5/5 |

Every evolutionary trial: elite is a non-no-op, single-file, edit-scope-valid patch
with `build_pass=1.0`, `build_tier=2.0`, inner `combined_score=0.7`. Every linear
trial: the pipeline emitted its automatic no-op candidate (`cand-auto-noop-001`)
and L1 build passed.

### 8.2 Pre-registered criterion

The §6 success criterion (majority of evolutionary trials produce a non-no-op,
edit-scope-valid, building elite) is **met, 5/5**. Defects A and B did not recur;
the two 17-06 failure causes (A/B and absent edit targets) are both cleared.

### 8.3 Harness defect found during the run

`_find_run_dir` selected OpenEvolve checkpoint directories
(`evolve/openevolve_output/checkpoints/checkpoint_NN/metadata.json`) instead of the
timestamped run dir (`max()` by name; `"c" > "2"` in ASCII), so the first summary
reported all evolutionary trials as `files=0 / L1 UNKNOWN`. Fixed the same day
(timestamp-regex filter) and results re-parsed from the untouched trial artifacts
(`--reparse`; no re-run, no additional LLM spend). The tables above are post-fix.

### 8.4 Reference comparison (evaluation-only) — negative

The upstream reference change for this pair is a one-line modernization:
`kzalloc(sizeof(*genpd_data), GFP_KERNEL)` → `kzalloc_obj(*genpd_data)`
(treewide `kmalloc_obj` conversion). The old form still compiles on v7.0-rc1, so
the migration is not compile-forced.

- No evolutionary elite reproduces it. All five elites are gratuitous single-include
  insertions (`of_device.h` ×3, `pm.h`, `pm_runtime.h`, `mod_devicetable.h`) that
  build but change no behavior: the v1 fitness (no-op penalty + build tier) is
  satisfied by the cheapest building non-no-op edit — reward hacking of the
  fitness function, exactly the §2.1/§7 concern about a build-only signal.
- The linear control's no-op is semantically closer to "nothing breaks" but misses
  the modernization equally.

Conclusion: the search machinery is validated (it runs, scores, respects edit
scope, returns building elites), but on a case whose reference change is not
compile-forced the v1 fitness cannot prefer the correct edit over a vacuous one.
A fitness signal beyond build tier (obligation checks in the inner loop, or a
compile-forced case) is required before claiming the search finds *correct*
migrations.

## 9. v2 fitness: response to §8.4 (2026-07-23, unvalidated until re-run)

Code changes (`evolve_fitness.py`, `evolve_evaluator.py`,
`semantic_obligation_validator.py`); working tree only, not committed:

1. **Edit-relevance penalty.** The evaluator maps the candidate diff to
   modified functions (`_modified_functions_from_patch`) and checks them
   against the editable set (`editable_functions` + hard/semantic affected +
   `edit_candidate_functions`). A building patch that modifies no editable
   function (global-only edits, e.g. a bare `#include` insertion) loses
   `GRATUITOUS_EDIT_PENALTY=0.25`: score 0.45, strictly below an honest
   scored no-op (0.70). This removes the §8.4 exploit directly.
2. **Deterministic obligations in the inner loop.** New
   `obligation_progress_from_patch(patch_text, obligations_payload)` counts
   the two mechanically checkable obligation shapes (required replacement
   symbol added; old symbol removed and not re-added) and the fitness awards
   `W_OBLIGATION * satisfied/required` partial credit, so obligation-relevant
   edits (0.75-0.80) dominate both no-ops and vacuous edits.
3. **Honest no-op scoring.** When no obligation mechanically requires an edit
   (`_effective_patch_required` false), a no-op genome is scored against a
   one-time baseline build of the unmodified staged source
   (`BuildSession.evaluate_patch("")`, cached per trial) instead of being
   hard-zeroed: the search is no longer pressured to invent changes. When an
   effective patch IS required, a no-op still scores 0.

Score order after the change (verified by a local dependency-free check of
`validation_to_metrics`): required-and-satisfied edit 0.80 > relevant building
edit 0.70 = honest no-op (nothing required, baseline builds) 0.70 > gratuitous
building edit 0.45 > non-building edit <=0.30 > no-op when a patch is required
0.00. Legacy call shapes (no new kwargs) score exactly as v1.

Expected v2 behavior on the §8 case (no mechanically required obligation): the
elite should converge to the no-op, agreeing with the linear control, instead
of a gratuitous include. Producing *correct* non-forced modernizations (the
`kzalloc_obj` reference) remains out of reach of a deterministic fitness and
is out of scope for this fix.

### 9.1 v2 re-run (2026-07-23, same day) — prediction confirmed

Re-ran the evolutionary arm with the v2 fitness on the same case and settings
(N_EVOLVE=5; linear control unchanged from §8; artifacts in
`runs/evolve_experiment_v2/`). Result: **5/5 elites converge to the no-op**
(`parse_status=skipped_auto_noop`, `combined_score=0.7`, `build_tier=2.0` from
the baseline build, `obligation_required=0`), agreeing with the 5/5 no-op
linear control from §8. No trial produced a gratuitous include; 0 provider/
infra failures. Validation status of the v2 fitness on this case: **RUN
2026-07-23**. Caveat (§7 unchanged): a single case; the obligation-credit path
(`obligation_required > 0`) and the required-patch pressure path are exercised
only by the local fitness matrix check, not yet by a case whose obligations
mechanically require an edit.
