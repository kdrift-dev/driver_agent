# Obligation-Anchored Context Distillation for driver migration

Updated 2026-09-14. Current execution status is in
[docs-impl/STATUS.md](../docs-impl/STATUS.md); earlier numerical claims are
corrected in the [historical result report](../docs-impl/06_ppl_signal_result.md).

## Problem and hypothesis

Driver migration adapts an old driver to a changed kernel contract. A migration
prompt has a finite budget, and static priority plus source truncation can
remove useful context. The hypothesis is that obligations derived from old
driver use and kernel-side changes help choose useful context under that budget.
Whether this improves generated patches remains to be measured.

The production pipeline builds compatibility obligations from kernel changes
and the old driver. These are machine-derived heuristic records, not complete
formal specifications. Neither target driver code nor reference migration
patches may enter its generation input.

## Implemented selection

`DRIVER_AGENT_CONTEXT_PRUNER` enables an experimental pipeline policy. It reads
native affected-symbol and replacement-candidate fields, preserves mandatory
editable/diagnostic source, ranks read-only blocks by obligation overlap and
size, and keeps complete blocks that fit the rendered prompt's character budget.
When mandatory input alone overflows, generation stops before a model call.
The flag remains OFF by default. Per-block retention reasons are recorded
outside the model input. Ranking coefficients remain uncalibrated.

The evaluation package `tools/ctxlab` has a separate method-comparison interface.
It includes truncation/random baselines, lexical relevance methods,
obligation-oriented greedy coverage, and explicitly labeled reference-based
oracle methods. In comparison v2:

- HF budgets use its model tokenizer; mock uses a labeled occurrence-based
  lexical proxy. The complete rendered prompt, including source and guidance,
  is charged against the budget.
- Default locus is all old source. Optional reference-derived localization is
  explicitly an evaluation oracle. Gate and scorer see the same selected source.
- Ordinary selectors receive an allowed-data view without gold labels or
  reference edits; evidence order and random seeds are deterministic.
- Reference-edit selectors use the actual configured scoring backend;
  `oracle_obligations` labels gold-derived obligations separately.
- Oversized prompts and backend truncation are explicit exclusions. Exact
  prompts, counts, selection settings and provenance are retained for review.

The candidate pool is still mined using the evaluation commit. Hiding its gold
partition does not make that pool production-derived. The ordinary comparison
obligation heuristic is also not the full production obligation builder.
End-to-end evaluation must address both limits before claiming deployability.

## Metrics and data

Atomic kernel migration commits provide kernel-side evidence and driver-side
reference edits. The miner's token-intersection labels are inexpensive but
imperfect; their completeness and specificity require audit.

The expressibility gate measures identifier mention coverage in old source
plus context. It does not check declarations, typing, semantics or compilation.
Newly introduced driver-local identifiers can fail the gate even when no extra
kernel evidence is needed.

Teacher-forced edit NLL/PPL asks how predictable the reference edit is given
context. This is an evaluation metric requiring a reference; it is not a
production selector signal unless explicitly labeled oracle. Its relationship
to patch correctness is an unvalidated hypothesis. Compilation and applicable
runtime/semantic validation remain necessary.

Mock scoring validates machinery only. Real HF scoring uses a cached causal
language model. The legacy PPL harness also supports OpenRouter echo, whose
failures are excluded; comparison v2 requires a locally countable tokenizer
and therefore supports mock and HF.

## Evidence to date

The August 26 run has 200 frozen cases and 1000 real HF scores. Gold < random
in 140/200 (70%; 44 ties), gold < none in 183/200 (91.5%). But gold gate pass
is only 116/200 (58%), below the frozen protocol's 80% stop-rule. **No successful
pre-registered validation is established.** Common character caps also did not
produce equal-length controls, and the old gate/scorer source mismatch matters.
The 116 gate-passing cases give 77 wins against random (66.38%, 29 ties); this
post-hoc subset does not waive the stop-rule.

The historical 40-case comparison found no `ours` advantage. Backend, budget,
label access and locus defects mean its ranking is diagnostic. The current
implementation corrects these contracts; regression tests assert invariants,
not a desired method winner. No new selection advantage is claimed.

The [independent audit](../docs-impl/07_gate_audit.md) reproduces every historical
row before analyzing all 84 failures. New manifests hash pack contents rather
than IDs alone; new runs retain initial identities and detect input/code changes.

## Next experiment

First settle what the gate should require and verify uncertain audit categories.
Then freeze a new protocol with exact model-token budgets, matched control
lengths, tie handling, migration-commit splits, model/data revisions and overflow
handling. Construct cases with actual evidence-budget pressure and evaluate
production-derived pools separately from oracle pools. Finally compare generated
patches at equal budgets with pruner OFF/ON and run kernel validation.

The old frozen pre-registration and all historical run artifacts remain intact.
Research positioning and novelty claims require a separate sourced comparison;
this document describes the implemented hypothesis and its present evidence.
