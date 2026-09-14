# Explicit identity edit contract v3

This policy is chosen after the Xilinx v2 diagnostic and its separate post-hoc
evaluation. It is a development decision, not a prospective success on that
case. Preserve all v1/v2 refusals and the evaluation-only interpretation under
their original protocols. Do not run another Xilinx generation to establish a
method advantage. Freeze a new case and its prerequisites before dispatch.

## Production contract

`compile_identity_edits()` in `driver_agent_pregen/exact_edits.py` adds an
explicit production entry point. Its shared compiler retains the v1/v2
entry points, defaults, outputs and rejection behavior. It does not import the
post-hoc evaluator or consume historical responses.

V3 accepts the same strict JSON schema: exactly `edits`, `rationale`, `notes`,
with at most 16 edits containing exactly `path`, `old_text`, `new_text`.
Each nonempty original substring must occur exactly once, including overlapping
occurrences, in an allowed original file. Every submitted span participates in
overlap validation and simultaneous application, even if its old and new text
are equal. Keep UTF-8 validation, safe paths, control-character restrictions,
exact bytes, original-source coordinates and deterministic unified diffs.
There is no trimming, normalization, inferred newline, fuzzy matching, repair,
or filtering of submitted operations.

V3 records `submitted_edits` in response order, `submitted_count`,
`changed_count`, `identity_count`, and `submitted_index`/`effect` on every
audited span. The audit remains in canonical file/span order. Preserve the
raw JSON response separately with its rationale and notes.

Statuses have distinct meanings:

- `compiled`: at least one changing edit produces a nonempty diff. It may be
  admitted for an independent patch application/build check.
- `identity_only`: every submitted edit is an identity. Preserve its complete
  audit and unchanged source hashes; emit no candidate diff and do not build it.
- `no_edits`: the list is empty. Preserve the output; emit no diff or build.

Changing operations whose combined result has no source change still reject.
No-op output is never a repair or a build success. Mixed identity/changing
lists must pass every span check; an invalid identity invalidates the list.

## Runner binding and evidence limits

Select v3 explicitly with `--edit-contract identity_substrings_v3` in
`tools/run_structured_edit_pilot.py`. The default remains whole-line v1.
The schema name is `exact_original_substring_identity_edits_v3`; the protocol
is `paired_identity_edit_character_diagnostic_v3`. Generic instructions explain
identity operations without migration-specific examples or replacement hints.

The runner currently retains the original Xilinx preparation anchor. Adding
v3 does not silently admit a new preparation or make NTB generation eligible.
A future case needs an explicit source/evidence anchor, full module controls,
input-boundary audit and frozen dispatch protocol first. NTB qualification is
evaluation-only and cannot supply target NEW source or manual repair hints to
the production evidence path.

Existing execution limits remain: fresh output, immutable code and inputs,
complete request/schema freeze before credentials, at most two explicit
requests in first-fit/ranked order, fixed provider/model settings, no retries
or repair loop, and refusal/truncation/raw-response/schema binding checks.
Only `compiled` emits `generated.diff`. Identity-only and empty results have
distinct admission statuses, no diff and no build claim.

The selector counts complete messages plus the submitted response schema in
characters. It reselects under its common quarter-pool cap using the v3
renderer. This does not establish a provider-token budget:
`generation_ready=false` and `actual_token_budget_comparison=false` remain.
Native usage is observational after any future request. Physical driver
runtime remains a separate, unexecuted evidence layer.

Verify the compiler and fake-client transport with unrelated synthetic edits,
exact independently expected bytes and actual Git application. Check identity
ambiguity/overlap/path errors, complete audit retention, no-op-only behavior,
contract binding and unchanged v1/v2 fixtures before any future v3 dispatch.
