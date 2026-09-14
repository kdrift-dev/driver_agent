# Paired exact-substring diagnostic v2

Freeze this protocol and executable code before a fresh pair of requests. This
is an exploratory follow-up to the whole-line failures in report 21. Preserve
those failed responses and all previous protocols unchanged. Do not repair or
re-admit previous output under the new rules.

## Input, selection and budget

Use only the anchored preparation
`runs/paired_context_xilinx_chars_v1_20260914/`, whose index SHA-256 is
`6a83a8933bbe06ef9f2fd08462f7d3d24bcd8fdd0df6db666382d315a7adc638`.
Verify its indexed bytes and reproduce the original preparation before making
new requests. Both arms retain the same full 430-line old target, minimal
machine-derived requirements and 62 kernel-evidence blocks. Old source, pool
and requirement digests must match the original preparation. Previous generated
edits, target new-driver source, reference patches, other-driver migrations and
manual replacement hints are not input to either arm.

The new generic instructions describe exact substring edits and explicitly
forbid no-op placeholders. Schema fields remain unchanged. Its request name
is `exact_original_substring_edits_v2`; the run protocol is
`paired_substring_edit_character_diagnostic_v2`. Rerun first-fit and
obligation-ranked selection using this complete renderer. Freeze their common
cap before selection as `mandatory + ceil(max(0, full_pool - mandatory) / 4)`.
The counter measures canonical JSON of complete `messages` and submitted
`response_format`, including the schema. Transport settings are outside this
character metric. This does not establish equal model-token budgets or count
provider-side schema framing. Keep `generation_ready=false` and
`actual_token_budget_comparison=false`; report returned native usage separately.

## Exact substring edit contract

Keep v1 callable with its existing whole-line behavior. Select the new contract
explicitly with `--edit-contract exact_substrings_v2`; the default remains v1.

Parse the untouched response as one strict JSON object with exactly `edits`,
`rationale`, and `notes`. Each edit has exactly `path`, `old_text`, `new_text`.
Retain the strict JSON-schema request, required fields and no extra properties.
Enforce at most 16 edits locally. Bind the original string to the sole plain
assistant response. Reject refusals, tool calls, malformed fields, duplicate
keys, truncation, unexpected model/provider and non-`end_turn` native finishes.
The generic client's extracted JSON and appended metadata do not define admission.

For v2, every nonempty `old_text` must occur verbatim exactly once in its
allowed ORIGINAL source file, counting overlapping occurrences. It may begin
or end inside a line. Match exact UTF-8 text without Unicode normalization,
trimming, whitespace adjustment, fuzzy search or location hints. New text is
the model's exact replacement for that span; empty new text deletes it.
Every byte outside the matched span remains unchanged, including an adjacent
newline that is not part of the span. No text or newline is supplied by the
compiler. The only relaxation from v1 is removal of the complete-line and
replacement-final-LF requirements.

Retain safe existing-source paths, UTF-8/control-character validation, disjoint
original spans, simultaneous application and rejection of `old_text == new_text`.
No earlier edit can create a match for a later edit. Preserve canonical sorting,
byte/character offsets, source and edit hashes, and deterministic unified diffs
with three context lines and final-newline markers. Empty edits mean no edit;
combined edits that produce no source change are rejected. This is the output
representation fixed before dispatch, not post-hoc repair of a model diff.

Verify both old and new contracts on unrelated synthetic fixtures before the
run: exact expected bytes plus real Git application, Unicode/combining marks,
edits inside lines, adjacent spans, deletion and newline changes, ambiguity,
overlap, no-op and sequential-match failures. Do not reuse historical driver
responses as successful test fixtures or feedback.

## Bounded execution and validation

At most two fresh client invocations: first-fit then obligation-ranked. Use
`anthropic/claude-sonnet-4.6`, temperature zero, max output 4096 tokens, a
300-second timeout and zero explicit retries. Route only to Anthropic, require
supported parameters, disable fallback, reasoning and context compression.
Do not enable response healing or feedback iterations. Stop the pair for shared
authentication/quota failure. Preserve all failures and uncertain attempt counts.

Use a clean separate retro checkout. Freeze code identity, both exact outgoing
bodies, schema, source, selection and count audits before constructing the
provider. Verify these identities before each call and after completion. Save
dispatch intent and progress durably. Refuse output reuse or automatic resumption.

For each admitted diff, run the strict Xilinx validator unchanged against the
frozen old source and a private ordinary copy of the NEW module-control tree.
Require exact patch coordinates and bytes with no offsets/fuzz or repairs.
Set `GIT_OPTIONAL_LOCKS=0` for read-only Git operations. Verify control hashes,
tool versions, donor inventory and no mutable inode aliasing. Preserve the
saved `6.19.0-rc5+` release with the existing KERNELRELEASE pin. Record fresh
object compilation and strict module linking separately, including real
compiler/modpost/linker commands, ELF/import checks, stable kernel/config
hashes and donor preservation. Keep infrastructure failures distinct from
patch failures; any infrastructure correction requires retained failed artifacts
and an explicit new validation output, without changing the model patch.
Physical Xilinx runtime is untested and no runtime pass may be inferred.

Audit request provenance, exact edits/diffs, apply, compilation and link outcomes
independently. This single case cannot establish a selection advantage.
