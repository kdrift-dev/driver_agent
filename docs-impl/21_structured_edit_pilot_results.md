# Xilinx structured-edit diagnostic: results

Both real requests returned valid sole JSON objects, but **neither edit set was
admitted** by the frozen whole-line contract. Both first edits omit the final
line feed from their exact source spans. Ranked also contains a no-op second
edit. No generated diff, patch application, compilation, link or runtime result
exists for this run. There were two client attempts and zero repairs or explicit
client retries.

## Frozen execution

The [protocol](20_structured_edit_pilot_protocol.md) and code were committed as
`6c9fac6a54da2a339922bdb5fb894334ffe7084e`, on `codex/structured-edit-pilot`, before
dispatch from the clean checkout `/home/arina/work/driver_agent_structured_edits`.
The source digest is
`b5be89dbef71ee3b5ce41aca8e99576d052c6547fbd7106fca7f46282689a847`
for 114 files. Freeze manifest SHA-256:
`1c4a4547355155c0b148fb1cad91228e952770fce5ed7ba563c362606c2e1b12`.

The host launcher now defaults to this checkout. Its previous version is saved
as `/home/arina/bin/driver-agent-shell.before-structured-edits`.

This run changes the output representation: the request uses the provider's
strict JSON-schema output mode, responses supply exact `old_text`/`new_text`
edits, and a deterministic compiler constructs diff coordinates. The whole-line,
uniqueness, non-overlap and no-op rules were
committed before generation. The previous failed responses, target new-driver
source, reference patches and manual replacement hints were not model inputs.

The complete old target, requirements and 62-block kernel pool retain their
previous hashes. Both selectors were rerun with the new renderer, counting
canonical JSON containing complete messages and the submitted response schema.
Mandatory input is 15,743 characters; the full pool is 62,366. The frozen common
cap is 27,399 characters. Selected sets remain 18/20 blocks, with the changed API
block present in both. These counts do not measure provider-side schema framing
or establish equal Claude input-token budgets. `generation_ready=false` and
`actual_token_budget_comparison=false` remain in the artifacts.

## Observations

| Observation | First-fit | Obligation-ranked |
|---|---:|---:|
| Input messages/schema characters | 27,183 | 26,803 |
| Selected kernel blocks | 18 | 20 |
| Explicit client attempts | 1 | 1 |
| Provider input tokens | 12,030 | 11,866 |
| Provider output tokens | 526 | 669 |
| Provider-reported USD cost | 0.043980 | 0.045633 |
| Provider request / sole JSON | Succeeded / valid | Succeeded / valid |
| Submitted edits | 1 | 2 |
| Strict edit admission | Rejected | Rejected |
| Diff / apply / compile / link / runtime | Not run | Not run |

Both responses report `anthropic/claude-sonnet-4.6`, provider `Anthropic`,
`stop` / native `end_turn`, with no truncation, refusal or tool call. Returned
reasoning, cached and cache-write token counts are zero. Total reported cost is
**USD 0.089613**. The audited client made exactly two sequential attempts;
provider-internal retries are not independently observable.

Both first edits copy the same `struct xilinx_rng` block and propose changing
the `aesctx` member from `struct crypto_aes_ctx *` to `struct aes_enckey *`.
Each `old_text` occurs exactly once in the original source, at character span
`[1776, 1983)`, beginning on line 59. The next original character is LF, excluded
from the submitted span. Both replacement strings also omit that LF. This
violates the frozen complete-line contract even though the substring matches.
The canonical first rejection is therefore
`old_text must cover complete original lines`.

Ranked additionally includes the allocation at line 352 as an explicit
placeholder: `old_text == new_text`. It violates the no-op rule and also omits
its final LF. The independent audit records this additional violation; the
runner stops at the first invalid edit and retains its original rejection.

The proposed type change agrees with the selected kernel API evidence, and
the existing allocation uses `sizeof(*rng->aesctx)`. This is semantic plausibility
only: no candidate passed the declared edit protocol or reached the build
validator. No whitespace was added, no no-op was dropped, and no historical
response was reclassified. This single diagnostic establishes neither a method
advantage nor successful driver migration.

## Implementation and verification

The new pure edit compiler checks the response structure, allowed paths, UTF-8,
unique original matches, complete-line spans, overlap and no-op edits. It builds
diffs deterministically, including final-newline markers. The opt-in runner
freezes schema, counts, payloads and code before provider construction, verifies
them before each call and after completion, and refuses reused output paths.
Its default mode makes no model request. The legacy paired renderer is unchanged.

Four exact-edit groups, three paired-context groups, five strict-validator
groups and five structured-runner groups pass locally and on retro: **17 groups**.
They include actual Git application of unrelated synthetic edits, rejection
cases, frozen-input mutation, interrupted attempts and authentication stopping.
The independent generation audit passes **596 checks**, including all 30 indexed
generation artifacts, source identity, raw-response binding, selector replay,
request counts and both expected rejections. Its SHA-256 is
`1aa0a4838515a8f83b49f7cbad05f734d7ffcfc27b6757bdf06b419c30ffd569`.

The first audit attempt wrongly assumed every model edit would be non-no-op;
its failed report and script are retained separately. The corrected auditor
checks expected rejection rather than assuming valid model output. This changes
the audit, not the model artifacts or admission rules.

A separate no-build audit passes 375 checks and confirms the absent generated
diffs and validation trees, unchanged full donor inventory, frozen controls and
committed release pin. Its SHA-256 is
`65fae4a14cc276c329080cd3728171640d42347290fab60ec4ce95464461ce98`.
Its first attempt incorrectly compared the generic client's `parsed_output`
metadata field with the raw JSON schema. That report is preserved, with a
correction record; the final comparison accounts for `parse_status=ok` while
still binding the original response bytes. The two final audits total 971 checks.

Read-only build preflight verified 139 frozen control artifacts, six tool
versions, 92,203 tracked donor source files and its complete file inventory.
It found an infrastructure issue before generation: removing Git metadata from
the private donor copy changes `6.19.0-rc5+` to `6.19.0-rc5`. The validator now
checks the saved release and pins the original `KERNELRELEASE` for both `.o` and
`.ko` commands. Fixture checks pass; the changed build path was not exercised
by a real generated patch in this run. Prior strict module controls remain
historical evidence. Physical Xilinx runtime remains untested.

Canonical artifacts: `runs/structured_edit_generation_v1_20260914/`.
Checks: `runs/structured_edit_checks_20260914/`.
Preflight: `runs/structured_edit_build_preflight_20260914/`.
Independent reports: `runs/structured_edit_independent_audit_20260914/`.
The [catalog](../datasets/structured_edit_pilot_v1.json) pins the artifacts,
audit reports and checkout-preservation record. The previous character pilot,
paired preparation, control checkouts and dirty evolve checkout are preserved.

## Next step

A separately frozen v2 can allow exact unique substring replacements: preserve
the original bytes outside each span, require unique disjoint original matches,
and retain deterministic diff construction. This avoids requiring the model to
copy a line terminator that lies outside its chosen exact span. Keep no-op
rejection, state it explicitly in the prompt, and test Unicode, newline edits,
adjacent spans and ambiguity on unrelated fixtures before any new calls.
The current results remain failures under v1. Provider-bound token counting
and additional independent migration commits are still required before a
confirmatory selection comparison.
