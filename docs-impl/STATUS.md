# Context-engineering implementation loop — STATUS

Branch `ctx-impl` off `context-engineering`. All milestones M0–M5 implemented,
reviewed, reported, committed. Nothing pushed; no remote or retro operations.

## Milestones (each: implement + run + review + report + commit)

| # | Milestone | Commit | Report |
|---|---|---|---|
| M0 | Migration-bench miner + ctxlab package | `4167e4b` | docs-impl/00_miner.md |
| M1 | Data layer + deterministic fixture checks | `94b17e5` | docs-impl/01_data_layer.md |
| M2 | Metric layer (score_context + mock backend) | `0a17e34` | docs-impl/02_metric_layer.md |
| M3 | Method zoo (8 methods) + default-OFF pruner hook | `6a89b30` | docs-impl/03_method_zoo.md |
| M4 | Comparison harness + ablation arms | `ba77583` | docs-impl/04_comparison_harness.md |
| M5 | Analysis notebook (executed) | (this commit) | docs-impl/05_analysis.md |

## Standing regression gate

`python3 tools/run_ctxlab_checks.py` — 28 checks, all green: fixture mine
(exact gold labels, junk filtered, per-commit sampling cap, sanity gauges,
schema, loader round-trip, catalog stats), mock-backend ordering + determinism,
method zoo (budget/locus invariants, ours ≥ random/trunc gold recall), pruner
ranking + non-destructiveness, comparison shape + both nulls, frozen-sample
manifest restrict + digest guard, end-to-end determinism. Run it after any
change to the miner / data / metric / method code. Notebooks execute under
`.venv-analysis`.

## Phase-2 offline prep (retro-gated run not yet executed)

Added ahead of the real run so it needs no code changes on the build host:

- `tools/run_ppl_signal_experiment.py --manifest <frozen>`: restricts scoring to
  the `draw_ppl_sample.py` frozen sample's `case_ids`, with a `dataset_digest`
  guard (refuses to score a mine other than the one the pre-registration froze;
  bypass only via `PPL_ALLOW_DIGEST_MISMATCH=1`, testing only). Without
  `--manifest`, behavior is unchanged.
- `docs/ppl_signal_preregistration.md`: sample now defined by the frozen manifest
  (digest + n_selected placeholders filled at freeze time); still **DRAFT**.
- `scripts/run_oacd_retro.sh`: stage-based, resumable Phase-3 runbook for retro
  (`pilot | mine | draw | ppl | compare | all`). The `pilot` stage enforces the
  pre-registered gold expressibility-gate ≥ 80% before allowing the full mine.

## Input-boundary status

Clean. `driver_agent_pregen/` imports no ctxlab/tools code (grep-verified).
The one pipeline touch is a default-OFF hook in
`patch_generation.py::_shrink_patch_input_payload`, gated by
`DRIVER_AGENT_CONTEXT_PRUNER`; unset, behavior is unchanged (verified: no note,
ordering identical to the pre-existing prioritizer). The pipeline-side
`context_pruner.py` reads only machine-derived obligation symbols, never
reference diffs or gold labels. Mined data and run artifacts stay under
`runs/` (git-ignored); no dataset is committed.

## Decisions that are the user's (STOP conditions) — none block further offline work

These were deliberately not taken; each needs an explicit call:

1. **Full v6.19..v7.0-rc1 mine.** The miner is validated on fixtures only.
   Real run (needs a linux.git clone, out of scope for this loop):
   `REPO=<linux> python3 tools/mine_migration_bench.py --old-ref v6.19 --new-ref v7.0-rc1`
   Options: (a) run on retro when reachable, (b) run locally against a clone,
   (c) defer. Recommendation: (a) — the build host already has the clone.
2. **Committing mined data.** Default is not to (packs are eval-only, tens of
   MB, regenerable from repo+refs+seed). Options: commit catalog.json + a
   curated subset / commit nothing / commit everything. Recommendation: catalog
   + curated 200-case subset once mined, rest regenerated on demand.
3. **Freezing the pre-registration** (`docs/ppl_signal_preregistration.md`,
   still DRAFT). Must be frozen by a commit BEFORE the first real PPL scoring
   run. Recommendation: freeze once mined cases exist and the sample is drawn.
4. **Real-LLM / HF-backend runs.** The pre-registered PPL experiment and any
   `--scorer hf/openrouter` run need `.venv-ppl` (torch/transformers) and, for
   openrouter, a key. Recommendation: run the 0.5B HF backend on retro after
   the mine; keep it off the paper-facing agent path.

## Real-run recipe (one command per stage, when the user green-lights data)

```bash
# 0. mine (host with a linux.git clone)
REPO=<linux> OUT_DIR=runs/mined python3 tools/mine_migration_bench.py \
    --old-ref v6.19 --new-ref v7.0-rc1
# 1. freeze pre-registration (commit), then draw the 200-case sample
# 2. PPL signal check with the real backend
PPL_SCORER=hf python3 tools/run_ppl_signal_experiment.py --dataset-dir runs/mined
# 3. method comparison with the real backend
python3 tools/run_method_comparison.py --dataset-dir runs/mined --scorer hf
```

## Caveats carried forward

- All committed numbers are mock-backend orderings on an 8-case synthetic
  fixture: they validate machinery and directions, not effect sizes.
- The two nulls (Sam-Bodden skeleton-vs-drop, ETH generated-guidance) are
  reproduced only in *direction* under the mock; their real signs need the HF
  backend.
- `--mode split-commits` in the miner is a documented refused stub.
