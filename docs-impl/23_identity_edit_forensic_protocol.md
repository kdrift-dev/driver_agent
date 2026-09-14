# Post-hoc identity-edit interpretation v1

This evaluation policy is chosen **after observing** the exact-substring v2
responses. Both canonical responses remain rejected for containing no-op edits.
This protocol does not reopen generation, change their admission, or establish
a successful v2 result. It asks a separate question: does literal execution of
the complete original edit list, allowing identity operations, produce source
that passes the strict module build?

Freeze this document and the evaluation helper before materializing source or
running builds. No further model requests, feedback or response revision are
allowed. Preserve the prior generation and all unsuccessful protocols.

## Frozen inputs and one policy change

Canonical input directory: `runs/substring_edit_generation_v2_20260914/`.
Its artifact index SHA-256 is
`32bb2f2ab1495d45b05e81ea1bb4f409a22afefb60135135ce7a0fd926f92cb7`;
freeze manifest SHA-256 is
`f857319b43d210ce3c39f29b02f2a434c59cfb4c8bdb4fd1d8d2264d4decfb42`.
Generation code is `0e5ed41062c4973a5f0106a6c0d0dd667b5dde29`.
Verify the indexed bytes, frozen full old source, exact outgoing requests and
raw response binding before interpretation. The sole old Xilinx source has
SHA-256 `088cbd398390ad9651d4066d50bf9f413e5ea6991ca49cc7a9b03a89c8863e5f`.
Keep the original `candidate_admission=rejected` and rejection reasons intact.

Only the rule forbidding `old_text == new_text` is waived. Preserve every
submitted edit, including identity operations, in the audit and apply the
complete list simultaneously against the original source. Keep nonempty exact
unique matches, overlap-aware ambiguity detection, allowed existing paths,
schema/type/UTF-8/control checks, the 16-edit cap and disjointness of ALL spans,
including identity spans. No-op operations cannot hide invalid paths, missing
matches, ambiguity or overlaps. Empty or all-identity lists are reported as
no source change and do not count as a buildable candidate.

No operation is removed, rewritten or inferred. Text outside the matched spans
is copied unchanged; replacement bytes come solely from the original response.
Do not trim, normalize, fix whitespace, add headers, change context or consult
reference patches. Record source/raw/list hashes, each original span and its
identity flag, counts of submitted/changed/identity operations, resulting source
hashes and the deterministic diff. Preserve the raw response and complete edit
list alongside the materialized result. This is a post-hoc relaxation of edit
admission, and must be labeled as such wherever its build result is reported.

## Execution and interpretation

Write new output only under `runs/identity_edit_forensics_v1_20260914/`; refuse
directory reuse. Freeze evaluator source identity and exact canonical inputs
before interpretation, verify them after completion, and index every artifact.
Check the interpreter on unrelated synthetic fixtures before processing the
real responses, including identities, exact changed bytes, invalid identity
spans, overlapping operations, malformed inputs and actual Git application.

The two submitted edit lists are equal. Nevertheless, run two separate strict
validations, in fresh private copies, under
`runs/identity_edit_validation_v1_20260914/{first_fit,obligation_ranked}`.
Do not count identical patches or their two builds as independent migration
cases. Use the existing validator with unchanged exact apply, donor isolation,
frozen tool/config/kernel, release pin, fresh compiler, strict modpost, linker,
ELF/import and integrity requirements. Set `GIT_OPTIONAL_LOCKS=0`; the historical
donor is read-only. Retain failures, with infrastructure and patch errors
distinguished. The canonical v2 validation directory stays absent.

An independent replay must bind each materialized diff back to the entire raw
edit list, then audit the real validation commands and artifacts. Report
canonical admission, post-hoc application, compilation, linking and runtime as
separate observations. Physical Xilinx runtime remains untested. Neither a
successful post-hoc build nor the plausible type change proves device behavior,
provides a selection advantage or satisfies the unresolved equal-token-budget
comparison. Repeated use of this case for protocol development provides
exploratory evidence only; preserve the historical dataset splits and records.
