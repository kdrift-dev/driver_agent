# Context-pressure set v1

This is a tokenizer-only census of existing mined migration packs, intended to
identify cases where selecting optional kernel evidence involves a real budget
tradeoff. It does not establish repair necessity, patch correctness, or retrieval
quality. No model weights, inference, PPL scores, gate outcomes, padding, or new
distractors are used to construct the set.

## Frozen contract

The global budgets are **1024, 2048, and 4096 model tokens**, measured on the
entire `ctxlab.metrics.build_prompt` prompt with the `full` evidence arm. The
complete old driver source is mandatory; reference-localized source slicing is
disabled. The scoring window is **8192 tokens**. The reference diff is used only
to measure target length and reserve sufficient scoring-window space.

A case is eligible at a budget only when all these conditions hold:

1. The full-source prompt with no optional evidence fits the budget.
2. The prompt with all existing deduplicated evidence blocks exceeds it.
3. The budget plus reference-target tokens fits the 8192-token window.
4. There are at least two candidate blocks and at least two blocks that each
   fit when added individually to the mandatory prompt.
5. The union of all individually feasible blocks exceeds the budget. An
   impossible-to-fit giant block alone does not establish a useful choice.

These are fixed eligibility conditions, with no quota or outcome-based tuning.
The last condition was fixed before new scoring or selection of cases. Blocks
and their content hashes come from the same label-blind `selector_case` and
`evidence_chunks` candidate construction used by comparison v2. No block is
truncated, duplicated, or enlarged to manufacture pressure.

Cases whose mandatory prompt exceeds the largest fixed budget are conclusively
ineligible at every budget. Their full source and candidate pool are still
hashed and inventoried, but optional-block costs and feasibility are `null`,
with `feasibility_status=not_evaluated_mandatory_overflow`. This avoids tens of
thousands of unnecessary tokenizer probes. Exclusion counts are nonexclusive
and list established reasons only; a missing count is not a measured zero.

Gold presence does not control eligibility. Gold-item counts and zero-gold
flags are recorded afterward as evaluation diagnostics. For example, some gold
evidence may itself be impossible to fit; this is a limitation to inspect, not
a reason to remove a case based on its label.

## Split and prior exposure

The split unit is the full migration commit. Every commit is assigned before
any eligibility measurement using:

```text
u = first 64 bits of SHA256("context-pressure-v1:" + seed + ":" + commit) mod 10000
calibration if u < 5000; reserved otherwise
```

The fixed seed is `0`. All commits represented in the historical 200-case PPL
manifest override this rule to calibration. The completed 24-case source review
is drawn inside those 200 cases, so its commits are covered by the same rule.
Forcing a commit also forces every other pack from that commit to calibration.
The full corpus contains 685 cases from 373 commits; the historical 200 span
110 commits and therefore force 315 of the 685 cases to calibration.

The other split is described as **reserved for future patch evaluation,
previously mined, with no new scoring during set construction**. It is not an
unseen dataset. Selected calibration and reserved commits must be disjoint at
every budget, and the assignment is consistent across all budgets.

## Tokenizer and artifacts

The tokenizer is loaded only from the local cache:

- Model ID: `Qwen/Qwen2.5-Coder-0.5B`.
- Snapshot revision: `8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301`.
- No special tokens, padding, truncation, or chat template are added.
- Every prompt cost is tokenized as the complete rendered string, including
  the source and instructions; block costs are not summed as a proxy.

Each manifest records tokenizer class, package versions, tokenizer/config file
hashes, snapshot revision, settings, full-pool content digest, inventory digest,
split digest, and selected case content hashes. `inventory.json` includes every
case's per-file SHA-256, split, measured costs, candidate identities and costs,
and per-budget eligibility/exclusion reasons. `split.json` contains the full
assignment and the original historical manifest's content hash.
`calibration_BUDGET.json` and `reserved_BUDGET.json` contain every eligible case
in each split; there is no subsampling to hit a desired count.
`integrity.json` fingerprints the emitted artifacts and records the code state,
including initial and final uncommitted source contents. The builder refuses to
freeze if the source digest or HEAD changes during the census, and refuses an
existing output directory. Changing any census pack invalidates verification, including a pack
not selected by a particular manifest.

```bash
# On retro, inside /home/arina/bin/driver-agent-shell; use a fresh output path.
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "$PPL_PY" \
  tools/build_context_pressure_set.py \
  --dataset-dir /home/arina/work/driver_agent_ctx/runs/mined \
  --calibration-manifest runs/oacd_results/ppl_sample_manifest.json \
  --out-dir runs/context_pressure_set_v1_reproduction

python3 tools/run_context_pressure_checks.py \
  --artifact-dir runs/context_pressure_set_v1_20260914 \
  --dataset-dir /home/arina/work/driver_agent_ctx/runs/mined
```

The dependency-free check runner uses temporary synthetic packs to test the
contracts; those fixtures are not benchmark candidates. It checks actual
mandatory/overflow/window conditions, genuine competing feasible choices,
commit-disjoint splits, forced historical commits, zero-gold eligibility,
determinism, content changes, artifact changes, and refusal to overwrite.

Pressure manifests have their own `context_pressure_v1` protocol. They must not
be fed to the legacy gold-eligible sample-manifest loader. A later experiment
can use `load_pressure_cases(manifest_path, dataset_dir, budget=..., split=...)`
from the builder to verify the entire census and load a particular selection.

## Canonical census on retro

The canonical tokenizer-only census at implementation commit `431c7a1`
produced these counts without relaxing the eligibility conditions:

| Whole-prompt budget | Eligible cases | Calibration | Reserved |
|---|---:|---:|---:|
| 1024 | 7 | 5 | 2 |
| 2048 | 18 | 12 | 6 |
| 4096 | 48 | 37 | 11 |

Across the three budgets there are 53 distinct calibration cases and 18 distinct
reserved cases. The full split contains 499 calibration and 186 reserved cases.
At budget 4096, 518 cases are rejected because the mandatory full-source prompt
is already too large; another 109 have no natural evidence pressure, and 10
have overflow only from individually infeasible evidence blocks. Reasons can
overlap. Eight eligible cases at 4096 have no gold items, recorded as diagnostics
without filtering them out.

At 4096, calibration spans 29 commits and reserved spans 10; the commit sets
are disjoint across all budgets. The canonical run used the pinned snapshot
with `transformers=4.57.1`, `tokenizers=0.22.1`, and
`huggingface_hub=0.35.3`. Tokenizer file digest:
`07849fe009ec921e884799588c5ebd6c581116c1d3ae0e7a8b2a6acc70302c9f`.
Initial/final executable code identity agrees, and verification passes against
both the original retro corpus and its local copy. No model weights were loaded.

The immutable artifacts are in `runs/context_pressure_set_v1_20260914/` locally
and in `/home/arina/work/driver_agent_gate_v2/` on retro. The committed
[catalog](../datasets/context_pressure_v1.json) pins all six selections, their
case hashes, split/tokenizer identities, and all artifact SHA-256 values.
It is an index, not a replacement for the verified pressure manifests.
Full-pool content digest:
`8a5fe9cc2de8fad46ecbd8734418a8c90dd0595b217f1043ce461871b648f30f`.
`integrity.json` file SHA-256:
`43b1f61284ef53b3436e8242134bb54f9fcb46ac1cce2539a1fffb301ce3dcbe`.

The earlier local run in `runs/context_pressure_set_v1_20260914_diagnostic`
has the same counts but remains diagnostic: it used different package versions
and preceded the final code-integrity guard. It is preserved, not promoted to
the canonical set.

## Limits before patch evaluation

The candidate pools remain **evaluation-mined case packs**, even though their
label ordering and metadata are hidden from ordinary selectors. This census
does not show that the production pipeline could retrieve the same evidence.

The task scope is `unverified_mined_migration`. The corpus includes feature
additions, optional modernization, and changes with sibling-driver dependencies.
Before a future patch-quality A/B evaluation, cases must be mapped to concrete
Kbuild module targets and their dependencies, and an old-driver-on-new-kernel
baseline build must establish a required repair. No such build is performed by
this tokenizer census. Changes to task eligibility or experimental scoring
require a separate protocol, while the manifests here stay immutable.
