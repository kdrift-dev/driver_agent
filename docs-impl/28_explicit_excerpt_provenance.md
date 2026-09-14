# Explicit excerpt provenance: second NTB preparation

The first frozen extraction (`c092786`, protocol 27) completed stages 1–4 but
the strict pool refused an ambiguous excerpt path. Preserve that run and its
refusal. This protocol adds producer-recorded provenance; it does not repair
or relabel the first extraction, guess a path from text, discard a candidate,
or add migration guidance.

## Changed representation

For each definition row, the existing production kernel diff builder records
`old_definition_location` and `new_definition_location` directly from the
locations it actually passes to `file_excerpt()`. Each is the selected locator
object, or null when no location exists. These fields carry source provenance,
not a new evidence source. They do not change the chosen excerpt, location
selection order, symbol discovery, helper search or obligation policy.

The evidence pool accepts these fields only together. A selected location must
equal an entry from the matching OLD/NEW locator row after its existing strict
path/line/match validation. Null requires no location and no excerpt on that
side. Bind the complete numbered excerpt to that selected file and verify
every line against the immutable Git blob. Reject absent/forged locations,
foreign driver paths, unsupported metadata, out-of-range or changed bytes.
Never choose among ambiguous legacy paths heuristically. Rows with neither
fields retain the original conservative inference and its ambiguity refusal.

Identical text in multiple kernel files is not proof of which file the producer
read. The explicit location supplies that missing link; the source check still
does not prove the approximate excerpt is a semantically complete definition.
Preserve this limitation, especially for comments or architecture-specific
forward declarations found by a lightweight locator.

## Frozen second run

Retain every boundary and no-inference/no-build rule from protocol 27. The
wrapper now freezes this protocol and uses `kernel_evidence_extraction_v2`.
Run it from a second clean committed checkout, leaving the c092786 module
control checkout and first extraction unchanged. Use the same repository,
refs, `drivers/ntb`, `ntb.ko`, full OLD module and eight workers; the new output
is `runs/ntb_production_preparation_20260914/extraction_v2`.

Before execution, verify unrelated synthetic fixtures with identical excerpts
in two files: legacy ambiguity still refuses, a producer-recorded location
binds to the corresponding exact Git bytes, and malformed/missing/forged
bindings refuse. Exercise the real extraction wrapper in a fresh synthetic
Git repository with no model, network, worktree or build calls.

Only if the strict pool is admitted, prepare the existing first-fit/ranked
character diagnostic in a fresh output using its unchanged quarter-pool rule.
Report all signal and pressure limitations. Do not inject hand-derived
requirements or compiler diagnostics. No real model requests follow this run;
`generation_ready=false` and `actual_token_budget_comparison=false` remain.
If another boundary check fails, retain that outcome without dropping rows or
changing its meaning. First-run failures and second-run results stay distinct.
