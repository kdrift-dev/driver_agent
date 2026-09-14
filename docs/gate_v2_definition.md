# Gate v2: external-identifier availability, calibration definition

This is a separate evaluation-only metric. It measures availability of
identified external names in the exact supplied source and context, while
exposing unresolved reference occurrences. It does not prove that a patch is
expressible, semantically correct, buildable, or required for compatibility.
The frozen legacy gate and its historical stop-rule remain unchanged.

## Inputs and boundary

The evaluator accepts the exact rendered context, the exact old source visible
to the model, the reference diff, and optionally a content-bound sparse review.
Reference-derived classifications and review notes are evaluation labels; they
must never be passed to a selector or placed in a production prompt.

The 24-case review was selected deterministically from known historical gate
failures, covering the six audit categories with distinct migration commits.
It is calibration material. Its review scope is the legacy-missing identifiers;
it is not a complete semantic annotation of every added line. Unsupported
occurrences outside that scope remain explicit in the evaluator's output.

## What is counted

C tokens are read as tokens, with comments, strings, character literals,
numbers and include-path spellings handled separately from identifier names.
This prevents artifacts such as `x0d` from `0x0d`, or `dma` and `buf` from
`<linux/dma-buf.h>`, from becoming artificial external-symbol requirements.
Ignoring an include-path spelling here does not certify include correctness.

Each added identifier occurrence is assessed at its file and source location.
A name introduced locally can be exempted only by a supported lexical-scope
proof or an explicit reviewed occurrence. A name declared elsewhere in the
patch does not automatically exempt uses in other functions or scopes.
Ambiguous macro expansion, members, conditional compilation and unsupported
syntax remain unknown. The implementation is deliberately not a complete C
compiler or name resolver.

A confirmed external requirement may be available from either exact old source
or exact context. Availability remains a lexical observation. It does not prove
that a declaration has the right type, signature, ownership or semantics.
Labels carry evidence locations, hashes, rationale and confidence; sparse or
stale annotations cannot silently become a complete correctness judgment.

External means external to the packed target file, not automatically admissible
kernel evidence. A sibling driver's new header/helper is recorded with its
specific provenance and remains evaluation-only collateral. A confirmed
dependency of that kind must not be copied into a generation prompt.

## Outcomes and denominators

| Status | Meaning |
|---|---|
| `fail` | At least one confirmed external requirement is absent. Uncertainty may also remain. |
| `unknown` | No confirmed requirement is missing, but unresolved occurrences remain. |
| `pass` | At least one confirmed external requirement exists, all are available, and no unresolved occurrences remain. |
| `not_applicable` | No confirmed external requirements or unresolved occurrences remain. |

Coverage is reported over confirmed external identifier occurrences only; with none,
coverage is null. Unknown and not_applicable are never successes. Every report
must retain these counts and its annotation scope. A higher rate than the old
mention gate is not evidence that the frozen experiment now passes its rule.
There is no target pass percentage for this calibration.

## Validation and next use

Adversarial contracts cover lexical artifacts, declaration scope, same-name
local/external collisions, unexpanded macros, exact context truncation,
annotation provenance and all outcome branches. A diagnostic runner evaluates
the 24 cases with full gold, the legacy 8000-character gold prefix, and no
context, using full old source consistently for both old and new gates.
The prefix condition isolates a historical extraction issue; it is not a new
model-token budget or a length-matched PPL control.

A future experiment must freeze its own protocol and specify annotation
completeness, unknown handling, model/tokenizer revisions and commit-level
splits before scoring. The pressure-set census identifies budget tradeoffs
without using gate outcomes or method scores. Required-repair scope, Kbuild
mapping and old-on-new build failures still need confirmation before a
production patch-quality experiment.
