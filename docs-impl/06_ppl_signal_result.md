# M6 — historical PPL result, corrected 2026-09-14

The real `Qwen/Qwen2.5-Coder-0.5B` teacher-forced run completed on August 26.
**The pre-registered result cannot be declared SUCCESS:** its gold-gate stop-rule
failed. This report corrects the interpretation; the frozen pre-registration,
CSV, manifest, cached scores and original generated summaries are unchanged.

## Provenance and descriptive results

Mine: `v6.19..v7.0-rc1`, 685 cases, 579 with at least one gold symbol. The pilot
reported an 87% gold gate rate, but the full sample has its own failed gate.
The 200-case sample (`n=200`, `seed=0`) was frozen at `924a8b5` before scoring.
Historical digest:
`32ecbf4da490b32ba86c9fc7e0eed268bd444a2890b675ab0d9409914b11c723`.
This v1 digest hashes eligible case IDs, **not file contents**; it cannot prove
that the scored files match the frozen files byte for byte.

Retro used CPU torch 2.9.1 / transformers 4.57.1, five conditions, 1000 rows,
zero recorded provider exclusions. A common 8000-character cap did not make
contexts equal in length. The scoring source was capped at 12000 characters,
while the gate saw the entire old source; the HF backend could also trim the
prompt head at its model window.

| Pair | Wins | Cases | Win rate |
|---|---:|---:|---:|
| gold < random | 140 | 200 | 70.0% |
| gold < none | 183 | 200 | 91.5% |
| gold < distractors_only | 138 | 200 | 69.0% |

Gold versus random has **44 exact ties**. The old summary's nominal p-values
(`1.5e-8`, `2.5e-36`, `8.0e-8`) treated ties as nonwins. They are retained as
historical outputs, not presented as a corrected paired sign test. Cases can
also share a migration commit, so independence needs treatment in a new protocol.
Mean NLL/token was gold 0.640, gold+distractors 0.640, distractors 0.668,
random 0.682, none 0.709; this ordering is descriptive.

## The stop-rule applies

The frozen pre-registration requires fixing labels before interpreting PPL
when gold gate pass rate is below 80%. Observed: **116/200 = 58%**, 84 failures,
mean coverage 0.891058. Passing the two PPL win-rate thresholds does not override
that condition.

The gate-passing subset has gold < random in **77/116 = 66.38%**, with 29 ties.
That is lower than 70%, is a post-hoc subset, and does not refute the concern
behind the stop-rule. Gate-passing does not establish label correctness.

The earlier explanation blaming 41 large treewide edits was unsupported and
is contradicted by the audit: all 41 cases with coverage below .75 have
`treewide_scale <= 12`; 83 of the 84 failures have scale <=12. The independent
[audit](07_gate_audit.md) reproduces all 1000 historical rows and examines every
missing-identifier occurrence. Its classifications distinguish observations
from declaration/indirection hypotheses and retain unresolved cases.

## Historical 40-case method comparison

At the old budgets, reported gold recall was:

| Historical budget | ours | trunc_baseline | random_budget | shapley_probe | keepdrop_dd |
|---|---:|---:|---:|---:|---:|
| 400 | .86 | .94 | .87 | .92 | .18 |
| 1200 | 1.00 | 1.00 | .98 | 1.00 | .18 |
| 3000 | 1.00 | 1.00 | .98 | 1.00 | .18 |

These budgets counted distinct identifiers, not actual model tokens. Budgets
30/80 could not even contain the roughly 125-unit reference-derived locus.
Further defects affected interpretation: oracle selectors silently used a mock
backend while final scores used HF; ordering put gold first; ordinary `ours`
received gold obligations; the budgeted locus differed from the source scored;
and Python's salted hash made random selection process-dependent.

Thus the archive shows no advantage for `ours`, but establishes neither a fair
method ranking nor that lack of overflow alone caused the result. Comparison v2
fixes these contracts and labels oracle access; its smoke results are not a
replacement research experiment. The old and new numeric budgets are not
comparable.

## Artifacts and next step

- Historical raw local files: `runs/oacd_results/`.
- Historical retro PPL: `/home/arina/work/driver_agent_ctx/runs/oacd/ppl_signal/`.
- Original frozen protocol: `docs/ppl_signal_preregistration.md` (unchanged).
- [Full gate audit](07_gate_audit.md) and [implementation report](08_experiment_foundation.md).

Resolve gate validity and freeze a new content-pinned, length-controlled protocol
before new confirmatory scoring. Do not edit the historical protocol to make
its outcome successful.
