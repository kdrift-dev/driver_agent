# Reviewer Feedback Response and Revision Plan

This document translates the OSDI reviewer comments into concrete paper and workflow changes.
It is intentionally strict: items marked `not addressed` should not be claimed as solved.

## High-Level Direction

The previous submission was reviewed as a broad benchmark plus a generic agent system.
The revised direction should be narrower and more defensible:

Driver-Agent is a kernel-evolution-grounded driver migration workflow. It uses old target driver source and old/new kernel-side definitions to identify affected driver functions, derive evidence-backed obligations/specs, and guide constrained patch generation. The generated specs are auxiliary patch-guidance and consistency-check artifacts, not correctness proofs. Target driver new-version diffs are evaluation-only.

The paper should not emphasize generic agent machinery, terminal access, file editing, task tracking, or sub-second latency. Those are implementation details, not contributions.

## Review #2974A Mapping

| Reviewer concern | Current status | Concrete response | Required action |
|---|---|---|---|
| Paper feels padded | not addressed in code | Reduce scope and remove generic agent framework prose. Keep only dataset/workflow/evaluation sections that support claims. | Paper rewrite: cut implementation-detail-heavy text. |
| DriverAdapt resembles existing agents | partially addressed | Reframe novelty as kernel-evolution evidence extraction, affected-function construction, and migration-specific spec/patch guidance. Do not claim terminal/file editing as novelty. | Paper rewrite: explicitly compare against OpenHands/SWE-agent/AutoCodeRover and say what is reused vs new. |
| LPA and GMA separation unclear | not addressed | Unless there is strong data, remove the two-agent design claim. Use one pipeline with stages rather than two separate agent types. | Paper rewrite: remove LPA/GMA or justify with measured ablation. |
| Sub-second latency not motivated | not addressed | Do not claim sub-second latency as a design goal. Kernel migration is offline maintenance; sub-minute or auditable analysis matters more. | Paper rewrite: remove sub-second language. |
| Data contamination due to model cutoff | partially addressed | Current workflow now excludes target driver new-version diff from agent input; mini dataset stores reference diff as evaluation-only. Need use release windows after model cutoff or explicitly treat cutoff as threat. | Experiment: choose post-cutoff release pair or report contamination risk. |
| Results unclear, metrics unclear | partially addressed | New mini dataset analysis reports compile, semantic-key match, function overlap, post reasoning, and manual verdict separately. Post reasoning is stated-spec consistency only; similarity is diagnostic only. | Experiment: run clean 10-case full flow and report per-metric table. |
| Dataset construction dedup unclear | not addressed | Need define module ownership rule, deepest marker rule, path-containment handling, and duplicate commit/case collapse policy. | Add dataset methodology section and reproducible dedup script/report. |
| Taxonomy design unclear | not addressed | Need derive taxonomy empirically from inspected cases or state it is a priori and limited. Current mini dataset uses simple categories only. | Add taxonomy protocol; label all mini cases and later larger set. |
| Code embeddings E_pre/E_post unclear | not applicable if removed | The revised workflow should avoid embedding-derived dataset fields unless essential. If kept, define model, layer, pooling, normalization, and use. | Prefer remove embeddings from paper/dataset table. |
| Why not classify with LLM over diff | not addressed | If taxonomy classification is kept, compare manual/LLM labeling or explain why deterministic labels are preferred. | Optional experiment; not needed for mini dataset v0. |
| Human vs automatic labeling proportions missing | not addressed | Need record annotation source per case. Mini dataset v0 is manually selected and should mark `manual_selected=true`. | Add annotation metadata and labeler/time fields. |
| Text embeddings in dataset unclear | not applicable if removed | Remove text embeddings from dataset claims unless they are directly used. | Paper rewrite. |
| Migration/regression labels undefined | not addressed | Define labels or remove them. For current work use `kernel_evolution_migration`, `style_only`, `hardware_semantic`, `unknown`. | Dataset schema update if labels are used. |
| First-attempt rates unclear | not addressed | Report exact denominator, numerator, iteration meaning, and table/figure reference. | Paper rewrite after clean experiments. |
| Human patch similarity metric questionable | partially addressed | Current analyzer treats reference function overlap as diagnostic, not correctness. Semantic keys and compile/kselftest/manual verdict are separate. | Report this explicitly; avoid similarity-as-quality claim. |
| Acronyms missing | not addressed | Expand LSP, LLM, QEMU, KUnit, kselftest, AST, etc. | Paper edit checklist. |
| Missing references: OpenHands, Tree-sitter, OpenRouter | not addressed | Add references when mentioning tooling. Avoid listing tools as contributions. | Paper bibliography update. |
| Figure legend issue | not addressed | Requires paper figure revision. | Paper figure cleanup. |

## Review #2974B Mapping

| Reviewer concern | Current status | Concrete response | Required action |
|---|---|---|---|
| Design motivation unclear | partially addressed | The mini dataset and ablation modes can motivate design: source-only baseline fails to infer kernel migrations; kernel-evidence-guided workflow provides structured obligations. | Run and report B0/B1/D on mini dataset. |
| Conflates system design with implementation details | partially addressed | Reorganize design around data flow: evidence extraction -> affected functions -> obligations/specs -> patch generation -> validation. Move tool details to implementation appendix. | Paper rewrite. |
| Evaluation limited | partially addressed | Mini dataset v0 is a seed, not enough for final. Need clean 10-case full-flow results and larger non-LLM affected-function evaluation. | Experiments. |
| Typical OSDI paper structure missing | not addressed | Rewrite sections: Motivation, Problem, Dataset Method, System Design, Evaluation, Limitations. | Paper rewrite. |
| Shallow taxonomy motivation unclear | not addressed | Tie taxonomy to workflow decisions: affected detection, spec generation, validation feasibility. If taxonomy does not influence workflow/evaluation, remove it. | Dataset methodology revision. |
| Manual labeling cost/agreement missing | not addressed | Record labeler, time spent, disagreement policy. For mini v0 at least record single-labeler manual audit; for paper need two annotators or clear limitation. | Add annotation log. |
| Trade-off between effectiveness and better-labeled data missing | not addressed | Evaluate small high-confidence manual set vs larger automatically screened set. | Experiment design. |
| Evaluation section too dense/no subsections | not addressed | Split evaluation into RQs: affected discovery, patch generation benefit, spec-gap validation, kselftest feasibility, cost. | Paper rewrite. |
| Setup unclear: which drivers/classes | partially addressed | `datasets/intree_mini_v0/manifest.csv` now records module path, code_loc, source_file_count, category, semantic keys. | Include dataset table and selection criteria. |
| Static score weights unjustified | not addressed | Remove static score or replace with unweighted diagnostic metrics unless a validation ablation justifies weights. | Paper rewrite and maybe code cleanup. |

## What Is Already Implemented in Workflow

- Target driver reference diff is now stored only in `datasets/intree_mini_v0/cases/*/reference.diff` and documented as evaluation-only.
- Default public workflow hides and rejects other-driver/collateral evidence unless an internal environment variable is set for private debugging.
- Kernel commit information is disabled by default and only available through an explicit private/debug ablation flag.
- Synthetic replacement hints and case-specific patch fixups are disabled by default and must not be mixed into paper-facing results.
- The 10-case mini dataset includes old source snapshots, kernel-side evidence, semantic keys, and manual causal notes.
- The result analyzer separates compile, post reasoning, semantic-key match, and reference-function overlap.
- FM-Agent-style specs are documented as auxiliary patch guidance / consistency checks, not correctness proofs.
- The kselftest candidate list is explicit and marks hardware-dependent cases as limitations.

## What Remains Far From Solved

- Dataset deduplication is not yet formalized beyond the existing module marker CSV.
- Taxonomy is not yet empirically justified or independently labeled.
- There is no inter-annotator agreement or labeling-cost report.
- There is no clean full-flow 10-case result table yet.
- FM-Agent-style spec is not a correctness proof; `post_reasoning=PASS` only means the patch satisfies stated generated specs/obligations.
- kselftest integration is metadata-only; no QEMU kselftest pass/fail results are included yet.
- Prior claims around sub-second latency, two-agent LPA/GMA, and static score weights should be removed unless new evidence justifies them.

## Revision Checklist Before Next Submission

1. Build the clean 10-case mini dataset from `datasets/intree_mini_v0`.
2. Run Driver-Agent full flow on all 10 cases with no target-driver new diff and no collateral driver evidence.
3. Analyze results with `tools/analyze_intree_mini_results.py`.
4. Add one deterministic baseline: source-only prompt.
5. Add one kernel-evidence baseline: old source plus kernel-side evidence, no spec/reasoning.
6. Report semantic-key match, compile, no-op, post reasoning, and manual verdict separately.
7. Run at least `drivers/dma-buf/heaps` with relevant kselftest if feasible.
8. Create an annotation log with case selection reason, labeler, time spent, and confidence.
9. Remove or rewrite LPA/GMA and sub-second latency claims.
10. Remove weighted static score unless justified by ablation.
11. Add missing references for agent frameworks and tooling only where relevant.
12. Add a limitations section explicitly covering hardware, kselftest coverage, model cutoff, and FM-Agent spec incompleteness.

## Suggested Revised Evaluation RQs

- RQ1: Can kernel-side evidence identify the driver functions that require migration?
- RQ2: Does structured Driver-Agent context improve patch generation over source-only prompting?
- RQ3: How often do generated patches satisfy semantic keys and compile?
- RQ4: When does FM-Agent-style post reasoning fail to catch missing obligations, and what additional validation is needed?
- RQ5: Which cases have executable in-tree validation through kselftest or QEMU, and what remains hardware-limited?

## Suggested Paper Claim Boundaries

Safe claims:

- The workflow avoids target driver new-version diff as agent input.
- Kernel-side evidence and old driver usage can produce auditable migration obligations.
- Function-level/slice-level patch generation is a better fit for small in-tree migrations than full-module prompting.
- FM-Agent-style specs are useful for structuring patch guidance and local consistency checks, but not sufficient as a standalone proof of correctness.

Unsafe claims unless more evidence is added:

- The system generally fixes Linux drivers.
- Post reasoning proves patch correctness.
- LLM-generated specs are complete correctness specifications.
- Similarity to upstream patch is a correctness metric.
- Two-agent architecture is necessary.
- Sub-second latency matters for kernel maintenance.
- The taxonomy is comprehensive or empirically validated.
