# Paired context and request accounting, 2026-09-14

Execution: `446e42c`, branch `codex/paired-context-budget`. The
[preparation protocol](16_paired_context_protocol.md) was committed before the
canonical replays on retro. Execution source digest:
`f60e95e40370bfefcc8c9d979ca7903a7f9bb697641ef914a594bee6f7ae677b`.

## Implemented behavior

The explicit paired harness uses one complete old target, one production-derived
kernel evidence pool, one renderer, one request builder and one budget counter.
The two policies change only whole-block selection. Selected blocks return to
the original pool order; diagnostics never enter the prompt. This is a new
opt-in experiment, not a relabeling of the native source-pruner flag. Production
source modes and pruner defaults are unchanged.

The shared request builder is also used by the OpenAI and OpenRouter clients.
Stages save `request_body.json` and `provider_audit.json`: requested/returned
model, returned provider, full redacted response, usage, finish reason and
attempt records. SDK hidden retries and the OpenRouter worker-error resubmission
are removed. Bounded IPC handles large and incomplete frames. Request failure,
parse failure, empty content and truncation are separate; an uncertain timeout
count stays unknown with observed-attempt bounds rather than becoming zero.

## Real Xilinx replay

The retained production extraction resolves one C file, 430 old lines, without
private headers. Every old source byte and admitted kernel excerpt was verified
against its exact Git revision. The adapter uses only three allowlisted
production documents and the old source inventory. Reference patches, commit
messages, driver-side kernel evidence and replacement/guidance prose are not
selection inputs.

There are 62 deduplicated whole excerpt-pair blocks, totaling 35,302 content
characters before prompt serialization. Twenty symbol/kind rows have no located
excerpt; duplicate scanner kinds share one block. Exactly one block covers the
changed symbol `crypto_drbg_ctr_df`. Pool SHA-256:
`9f8ca2561631976c2bceba810cfed803ef338e12dfc82818a5f273d9b5ee7558`.
Both replays keep the same complete old-source payload, SHA-256
`baf2437b224e7fe5a0f8fb1472f87519b2e9751d19561f184818b0c761616367`.

| Counter | Mandatory | Full pool | Common cap | First-fit count / blocks | Ranked count / blocks |
|---|---:|---:|---:|---:|---:|
| Canonical system/user message JSON characters | 14,876 | 61,499 | 26,532 | 26,316 / 18 | 25,936 / 20 |
| Pinned Qwen chat-template tokens | 4,675 | 22,587 | 9,153 | 9,033 / 17 | 8,966 / 19 |

The cap is the predefined quarter-pool rule in both counter units. Both
conditions fit the same cap; consumed input counts are reported rather than
claimed to be identical. Mandatory input fits, the full pool does not, and
multiple individual blocks compete. Neither source nor evidence is truncated.
**Both policies retain the changed DF block.** Different additional blocks or
block counts do not establish better context, better patches or an effect size.

Qwen counts now include the complete system/user messages, pinned chat template
and assistant generation prefix. They differ from the earlier user-prompt-only
diagnostics. The tokenizer snapshot is
`8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301`; template SHA-256 is
`44d5f08f3f72b837eaad09f13a54c1f9f4eb58d75240334548b7fd52a5437fa5`.
These remain diagnostic for the candidate Claude request. All four prepared
conditions record `generation_ready=false` because provider/model/framing
compatibility has not been verified. No model request was sent.

## Validation

Local and retro checks pass:

- Ten LLM-client groups: exact configured retry counts, no error fallback,
  canonical request/body equality, response metadata, truncation/parse cases,
  1.5 MB IPC response, incomplete-frame deadline, timeout/crash uncertainty,
  credential redaction, zero-call dry-run, mocked SDK behavior.
- Forty-two evidence-boundary checks, including a real temporary Git fixture,
  forbidden paths/metadata, forged source bytes, stable ordering/deduplication,
  target ownership and minimal obligation signals.
- Three paired-context groups: nonadditive full-message counts, pressure-induced
  selection differences, same-set byte identity, intact mandatory overflow,
  mutable/mismatched counters, changed dispatch data and metadata rejection.
- Six existing native-pruner groups and 45 existing ctxlab checks.

The installed OpenAI SDK on retro is 2.33.0. Offline construction with an
explicit dummy key and fixture URL confirms `max_retries=0`; no request occurs.
Test logs and their digests are in `runs/paired_context_checks_20260914/`.

An independent audit passes **1731 checks**. Without importing the production
renderer or selector, it reproduces 3986 complete-message render/count variants
and every recorded policy choice. It verifies all 22 indexed output files,
the extraction bytes, 106 committed execution source files per run, and 1714
kernel excerpt lines. The original 93-file generation dry-run remains unchanged
locally and on retro. The audit does not establish semantic effectiveness or
provider-token equivalence. Audit:
`runs/paired_context_independent_audit_20260914.json`, SHA-256
`bd914c1f4283986b8fbe6a6c3358396d4ec7903459f0c346ad66a5048989c6f0`.
The independent script is retained alongside the runs and pinned by the audit.

No kernel rebuild or runtime test was needed for this change; the existing
strict Xilinx module controls remain the validation configuration for a future
generated patch.

## Artifacts and next admission step

The clean experiment checkout is `/home/arina/work/driver_agent_paired_context`.
The [machine-readable catalog](../datasets/paired_context_preparation_v1.json)
pins both runs, their exact selected IDs/request hashes and verification records.
Both local and remote `runs/paired_context_xilinx_chars_v1_20260914/` and
`runs/paired_context_xilinx_qwen_v1_20260914/` contain the pool, complete old
source, exact per-condition payload/prompt/request, decisions, code/input
identities and artifact index. Output reuse is refused. The frozen source
extraction, prior module controls and historical pressure/PPL runs are retained.
The host launcher now defaults to this checkout; its previous version is saved
as `/home/arina/bin/driver-agent-shell.before-paired-context`.
The preservation check confirms unchanged evolve HEAD/diff, clean ctx/Xilinx
checkouts and the original kernel HEAD. Its artifact and digest are in the catalog.

Before generation, bind a verified input counter to the actual provider's
model/revision and message framing. Then freeze the exact request, same cap
and output limit for both policies; the dispatch guard must recheck request
hash, counter identity and final count. A matching model-name string alone is
insufficient. The candidate Claude alias has not been verified by this offline
work. No new PPL measurements, generated patches or hardware claims are made.
