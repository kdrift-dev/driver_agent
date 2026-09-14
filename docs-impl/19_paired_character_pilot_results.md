# Xilinx paired generation diagnostic: results

The two real requests completed, but **neither response was admitted** by the
frozen output protocol. Both contained prose followed by fenced JSON. A separate
post-hoc check of their unchanged embedded diffs also rejected both patches:
the hunk coordinates do not match the complete old source. Compilation, linking
and runtime were not run. No request was repeated and no patch was repaired.

## Frozen execution

The [protocol](18_paired_character_pilot_protocol.md) and executable code were
committed as `e13e895a75ee68aa97b085e90b3d8cd654a54dae` before dispatch, on
`codex/paired-character-pilot`. The clean retro checkout is
`/home/arina/work/driver_agent_character_pilot`. Execution source digest:
`e0f09748b71f4660ac63f15bb4d0f388030f415c7c10026f11f75b4e41125afb`
for 110 source files. Freeze manifest SHA-256:
`099238897a1824617f3ee5b14f011aa7e3715a7531b18a0fc0aa593b6218bd0c`.

The host launcher now defaults to this checkout. Its previous version is saved
as `/home/arina/bin/driver-agent-shell.before-character-pilot`.

Both full requests were saved before provider construction. They reuse the
exact previous character-arm messages, complete old source and selected kernel
blocks, plus identical documented transport options. The shared cap remains
26,532 message-JSON characters. These are character-budget diagnostics;
`generation_ready=false` and `actual_token_budget_comparison=false` are retained.
The strict token-budget protocol is not retroactively satisfied by usage data.

## Observed outcomes

| Observation | First-fit | Obligation-ranked |
|---|---:|---:|
| Input message-JSON characters | 26,316 | 25,936 |
| Selected kernel blocks | 18 | 20 |
| Explicit client invocations | 1 | 1 |
| Provider input tokens | 11,639 | 11,475 |
| Provider output tokens | 943 | 747 |
| Provider-reported USD cost | 0.049062 | 0.045630 |
| Provider request | Succeeded | Succeeded |
| Strict JSON admission | Rejected | Rejected |
| Post-hoc exact diff application | Rejected | Rejected |
| Compilation / link / runtime | Not run | Not run |

Both responses report model `anthropic/claude-sonnet-4.6`, provider `Anthropic`,
finish reason `stop`, native finish reason `end_turn`, and no truncation. Cached,
cache-write and reasoning tokens are zero in the returned usage. Total reported
cost is USD 0.094692. This accounts for two explicit client invocations; internal
provider retries and transport hops are not independently observed.

The complete raw responses violate the declared sole-JSON requirement. The
existing generic client can extract their fenced objects and reports
`parse_status=ok`; the separate strict diagnostic correctly retains rejection.
No successful provider response or generic parse status is counted as a valid
patch. Both canonical response files and all rejection metadata are preserved.

## Separate forensic application check

After the canonical run completed, the evaluator copied the exact `patch_diff`
strings from the saved parsed objects into a separate forensic input directory.
This is an explicitly post-hoc diagnostic, not a relaxation of admission or a
repair. Their SHA-256 values are:

- First-fit: `37a6717120f18893a1f4229438e0241a5e1545e26b6f68c7c2ed1974accafd0c`.
- Ranked: `52623c3f55bba80fc4b39b0f17e9e0682d329fe593105ab60396e4dec7ba3cb8`.

Both suggest replacing `struct crypto_aes_ctx *aesctx` with
`struct aes_enckey *aesctx`. This matches the selected public API signature
change, and the existing allocation uses `sizeof(*rng->aesctx)`. Their proposed
edit content is identical, but hunk headers start at 56 and 57. The supplied old
source's exact context starts at line 60 and the changed member is at line 63.
The strict validator rejects both at their declared coordinates with zero
repairs. It records `apply=rejected`, `compile=not_run`, `link=not_run`, and
verified input integrity. No kernel copy or build was needed after rejection.

These observations separate output-format and patch-location failures from
recognizing the API migration. They provide no method advantage or successful
patch count. Semantic plausibility of the one-line edit does not establish
compiled or runtime correctness.

## Verification and artifacts

Nine new pilot check groups and four validator groups pass locally and on retro.
They cover canonical replay, zero-call preparation, bounded mocked attempts,
auth/quota stopping, frozen-input mutation, interrupted-call accounting, strict
JSON/schema/scope, exact patch application, donor isolation and compilation/link
evidence gates. The replay fix restores original JSON insertion order from the
hash-anchored prompt while verifying identical source/requirement values; no
historical prompt was changed.

The independent generation audit passes 535 checks, verifies all 26 indexed
files and 110 committed execution source files, reproduces both format failures,
and confirms freeze-before-dispatch and exactly two sequential client attempts.
Its SHA-256 is
`06cbd061e7d5c3b5427a3cc553a0c51f2e2c2f78551cde0e4a16df1e1c41371a`.

The separate forensic audit passes another 271 checks (806 in total), verifies
the unchanged 346-byte diff strings, exact parent source and coordinate failures,
and confirms no compilation, kernel copy or repair occurred. Its SHA-256 is
`6c6f4469e6ab731d4111b8c3410e764500b47f11aefba6aad8031f1a1766f00b`.
The checkout preservation record confirms the previous HEADs, clean control
checkouts and the unchanged three-file evolve diff.

Canonical artifacts: `runs/paired_character_generation_v1_20260914/`.
Post-hoc inputs and validation: `runs/paired_character_forensic_inputs_20260914/`
and `runs/paired_character_forensic_validation_20260914/`.
Tests: `runs/paired_character_checks_20260914/`.
The [result catalog](../datasets/paired_character_pilot_v1.json) pins these
artifacts and the independent audits. Earlier pressure, PPL, extraction and
module-control results are unchanged.

The next generation protocol should address structured output and exact patch
construction before another run. A provider-enforced JSON schema and a fixed,
validated edit representation with deterministic diff generation can be tested
on unrelated fixtures first. This would be a new frozen protocol, with the
current failed run retained. Equal model-token budgeting remains a separate
unresolved prerequisite for a confirmatory comparison.
