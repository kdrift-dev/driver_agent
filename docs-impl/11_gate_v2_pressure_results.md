# Gate v2 and context-pressure calibration — 2026-09-14

Implementation commit: `431c7a194f8ddb55d2cde9284c7dbea855432dc4` on
`codex/gate-v2-pressure`. The approved package covers the gate definition,
24-case source review and a naturally constrained context set. It does not
include a new PPL run, generated-patch A/B, or a confirmatory protocol.

## Gate v2: verified machinery with explicit unresolved cases

The standalone evaluator is `tools/ctxlab/gate_v2.py` (`gate_v2.1`). Its
[definition](../docs/gate_v2_definition.md) separates external requirements,
local-scope proofs, lexical exclusions and unresolved occurrences. Reference
judgments remain evaluation-only; existing comparison/production defaults and
the historical gate are unchanged.

The [source review](09_gate_review.md) covers 24 distinct migration commits,
113 legacy-missing names and 223 occurrences: 111 local, 106 external to the
packed file, five non-requirements and one unknown. Judgments are assistant-
authored, supported by exact source locations, not a human agreement study.
31 external occurrences depend on sibling-driver files; that source remains
forbidden as new generation evidence. The same spelling can have different
roles: `resv` in a header path is excluded, while its struct-member uses are
external. A dedicated negative control guards that distinction.

The committed evaluator ran on retro against all 24 reviewed cases with the
same complete old source in each condition. The 72 diagnostic rows give:

| Context | fail | unknown | pass | not_applicable |
|---|---:|---:|---:|---:|
| Full gold | 10 | 14 | 0 | 0 |
| Gold prefix, 8000 characters | 10 | 14 | 0 | 0 |
| No evidence | 12 | 12 | 0 | 0 |

These are not success rates or new measurements of PPL. `fail` means a
confirmed missing dependency with consistent inputs; `unknown` can include
unresolved occurrences or unsupported preprocessing even when other misses are
observed. The cutoff condition diagnoses the legacy character cap only.

All 223 review records are located: 219 match C identifier occurrences and four
are lexical exclusions. Of matched records, 119 judgments are honored
(including one explicitly reviewed unknown), and 100 are unsupported: 99 local
scope forms plus one variadic-preprocessor builtin. The 1160 added identifier
occurrences contain 941 unreviewed occurrences; 68 resolve automatically and
873 remain unresolved. Thus the sparse review cannot establish a complete
case-level gate pass. The gate remains a diagnostic, with a documented limit;
it is not a ready quality filter for confirmatory experiments.

No annotation-binding or source-reconstruction errors remain. Initial/final
code, selected inputs and manifest identity agree. The executable-source digest
on retro is `6b50447fac8807f5b71c3f6fe4ced45227f733ff0ed3a3848e74e5d3f450b29a`.
Artifacts: `runs/gate_v2_review_20260914/` both locally and under the new retro
checkout, with exact input texts, per-occurrence judgments and metadata.

## Frozen context-pressure set

The tokenizer-only census measured 685 case packs from 373 migration commits.
The complete old source remains mandatory. Eligible cases contain at least two
individually feasible evidence blocks whose union exceeds the prompt budget;
the reference target must fit the remaining 8192-token scoring window.

| Whole-prompt budget | Eligible | Calibration | Reserved |
|---|---:|---:|---:|
| 1024 | 7 | 5 | 2 |
| 2048 | 18 | 12 | 6 |
| 4096 | 48 | 37 | 11 |

The 4096 reserved set spans 10 commits. All splits are assigned by migration
commit before eligibility; every commit in the historical 200-case sample is
forced to calibration. The full assignment is 499 calibration / 186 reserved.
Across budgets there are 53 distinct calibration cases and 18 reserved cases.
Reserved means previously mined and reserved for future patch evaluation,
with no new scoring during construction; it does not mean unseen data.

The canonical retro run uses Qwen2.5-Coder-0.5B tokenizer snapshot
`8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301`, with transformers 4.57.1,
tokenizers 0.22.1 and huggingface_hub 0.35.3. Its initial/final executable
source identity agrees with the gate run. All artifacts and all 685 input
packs pass integrity verification on retro and locally; model inference calls
are zero. The [committed catalog](../datasets/context_pressure_v1.json) pins
the case IDs, content hashes, tokenizer files, and full artifact hashes.
See [selection contract and exclusions](10_context_pressure_set.md).

These cases are `unverified_mined_migration`, with evaluation-mined evidence
pools. Before patch A/B, map sources to Kbuild targets and establish required
repair using old-driver-on-new-kernel baseline builds. Feature additions and
optional modernization must not silently become repair benchmarks. No patch
generation or build has been performed in this package.

## Validation and host

On retro, the existing 45 fixture checks, 34 new gate checks and three pressure
contract groups pass. The annotation verifier checks all 223 occurrence traces
and rejects five deliberately invalid reviews. These tests verify invariants
and failure handling; they do not establish semantic accuracy or method quality.

The new checkout is `/home/arina/work/driver_agent_gate_v2`. The kernel corpus
continues to live in `/home/arina/work/driver_agent_ctx/runs/mined`; the kernel
Git repository remains `/home/arina/repos/linux`. The old context/experiment
checkouts and evolve modifications are preserved.

`/home/arina/bin/driver-agent-shell` now defaults to the new checkout and keeps
the `DRIVER_AGENT_ROOT` override. Its previous version is saved as
`/home/arina/bin/driver-agent-shell.before-gate-v2`. The evolve diff SHA-256 is
unchanged: `0644175acdb544a2b42b870566d01486c77b65002d27de0f50506e49db8f1b35`.
