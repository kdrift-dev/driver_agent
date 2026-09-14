# Paired structured-edit diagnostic v1

Freeze this protocol, source code and both requests before generation. This is
an exploratory follow-up to the format/coordinate failures in report 19, using
a new output representation. The failed run and its two responses stay intact;
no prior generated edit, reference patch, target new-driver source or manually
inferred replacement is part of the new model input.

## Input and selection

Reuse only the anchored production preparation in
`runs/paired_context_xilinx_chars_v1_20260914/`; index SHA-256:
`6a83a8933bbe06ef9f2fd08462f7d3d24bcd8fdd0df6db666382d315a7adc638`.
Verify every indexed artifact and replay the old preparation before deriving
the new requests. Recover JSON field order from the anchored old prompt while
requiring identical source and requirement values. Retain the complete 430-line
old target, minimal machine-derived obligations and the same 62 kernel blocks.

Use one new renderer for both first-fit and obligation-ranked selection. Only
generic output instructions and the strict response schema change. Rerun both
policies against the full newly rendered input; do not transplant old token or
character counts. Whole blocks keep their original pool order in final messages.
Old source, kernel-evidence pool and requirement digests must stay identical.

The diagnostic counter measures canonical JSON of an object with `messages`
and `response_format`: complete system/user messages plus the submitted output
schema. The common cap is `mandatory + ceil((full_pool - mandatory) / 4)`, with
the nonnegative increment and rounding from protocol 16. It is fixed before
selection. Fixed transport settings are outside this character metric.

These are submitted-input characters, not Claude tokens or a complete measure
of server-side framing. Structured output can add provider-side instructions.
Actual native usage is reported afterward. Keep `generation_ready=false` and
`actual_token_budget_comparison=false`; this remains distinct from the unresolved
confirmatory model-token-budget protocol.

## Response and deterministic edit construction

Request `response_format.type=json_schema`, `strict=true`, all object fields
required and `additionalProperties=false` at every object. Schema fields are
`edits`, `rationale`, and `notes`; each edit has `path`, `old_text`, `new_text`.
Do not include unsupported schema length/count restrictions; enforce a maximum
of 16 edits locally. Native format support is documented by
[OpenRouter](https://openrouter.ai/docs/guides/features/structured-outputs) and
[Anthropic](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
Refusal and output exhaustion still require explicit failure checks.

Parse the original response as one strict JSON object. Reject duplicate keys,
extra fields, prose/fences, malformed types, refusal/tool calls, truncation,
unexpected model/provider or a non-`end_turn` native finish. Bind the raw string
to the sole recorded assistant message in the actual provider response. The
generic client's tolerant parser does not define admission.

Match each nonempty `old_text` verbatim to exactly one complete-line span in
the ORIGINAL allowed file. Include overlapping occurrences when checking
uniqueness. All spans must be disjoint and all edits apply simultaneously;
an earlier edit cannot create another match. New text is model-supplied,
including its whitespace; there is no trimming, fuzzy search, API-specific
replacement, context repair or manual correction. Empty replacement deletes the
matched complete lines; no-op edits are rejected. Empty `edits` is a recorded
no-edit outcome. Paths must refer to the supplied existing source files.

A pure deterministic compiler builds patched UTF-8 source and a unified diff
with three context lines. It calculates coordinates from exact original bytes,
handles final-newline markers, sorts paths and spans, and preserves every
unchanged byte. This is the declared output representation, not repair of a
model-authored diff. Save the original JSON, edit audit, source/diff hashes and
generated diff. Verify this contract on unrelated synthetic fixtures before the
real run; do not use previous Xilinx responses as test repair instructions.

## Two bounded calls

Model: `anthropic/claude-sonnet-4.6`, temperature zero, max output 4096 tokens.
Route only to Anthropic, disable fallbacks, require supported parameters,
disable reasoning and context-compression. Do not enable response-healing.
Make at most one explicit client invocation per arm, first-fit then ranked,
with zero retries, a 300-second timeout and no feedback iteration.
Stop after a shared authentication/quota failure; preserve every outcome.

Freeze source identity, schema, selection/count audits, payloads, prompts and
exact outgoing bodies before constructing the provider. Verify input and code
identities and the freeze before each dispatch and after results. Persist a
pre-dispatch intent and per-arm progress. Unknown incomplete attempts stay
unknown, and an output directory can never be resumed or reused automatically.

## Apply, compile and link

For each admitted nonempty diff, run the existing exact Xilinx validator on
retro. Require exact old-source application without offsets/fuzz, and use a
private ordinary copy of the preserved new-kernel control tree. Verify all
frozen control artifacts, tool versions, donor tracked source and donor file
inventory before copying; confirm no writable inode is shared.

The donor release is `6.19.0-rc5+`. Its Git-free copy would otherwise lose the
`+` suffix. Verify `utsrelease.h` against `include/config/kernel.release`, pin
that exact `KERNELRELEASE` in both make commands and verify it afterward.
This preserves the frozen build identity; it does not change source or weaken
configuration/symbol checks.

Remove stale target outputs. Record exact application, fresh compilation and
strict module linking separately. Require fresh compiler and final linker
commands, modpost against the frozen `vmlinux.o` with no warning-only flags,
and a new ELF `.ko`; verify stable kernel/config/source hashes and unchanged
donor afterward. Retain infrastructure failures rather than relabeling them as
patch failures. Runtime is untested for the physical Xilinx device.
