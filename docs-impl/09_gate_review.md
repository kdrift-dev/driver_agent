# M9 — Sparse semantic review for gate-v2 calibration

**24 cases, 24 distinct commits, 113 legacy-missing names, 223 reviewed
occurrences.** This is an outcome-informed calibration sample from the frozen
200-case audit. It is **not held out**, does not revise the historical result,
and does not certify complete labels or complete reference edits. No PPL,
model-based A/B experiment, build, or compiler name-resolution pass was run.

## Selection and annotation contract

`tools/review_gate_cases.py` deterministically ranks failing cases by
`SHA256("gate-review-v2:0" + NUL + case_id)`. It first selects an unused-commit
case for each of the six old categories, ordered by increasing category case
count, then fills to 24 with unused commits in hash order. All six categories
are represented. Selection did not use the new semantic judgments.

The committed evaluation artifact is `datasets/gate_review_v2.json`:

- `schema_version: 1`, `evaluation_only: true`, and explicit `held_out: false`.
- Exact source-audit SHA-256 and selection recipe.
- Per-case SHA-256 bindings to the full old source and reference diff, plus
  packed kernel-evidence hash, parent/commit IDs and target old/new Git blob IDs.
- Occurrence-scoped judgments identified by `symbol`, `file`, `new_line`,
  `diff_line`, and zero-based same-symbol-on-line `occurrence`.
- Classification (`local`, `external`, `non_requirement`, `unknown`), subtype,
  confidence, rationale, residual uncertainty and precise evidence locations.
  Evidence includes short exact quotes and Git path/ref/blob hashes; reviewed
  local definitions include complete function, macro, object or type scopes.

Coverage is explicitly `legacy_missing_occurrences_only`. Every missing name
from the selected old-gate cases is reviewed, including repeated occurrences.
Other added identifiers are **unreviewed**, even if the old gate mentioned
them as covered. The consumer must not silently turn those into confirmed
external dependencies. Corrected lexing can discard reviewed include-path
fragments and record them as lexical exclusions.

## Review method and trace

The review inspected reference edits, old-source bindings, relevant complete
new target scopes and packed kernel evidence. Read-only Git object retrieval
from `retro:/home/arina/repos/linux` supplied exact parent/new target files and
scoped kernel or sibling-driver definitions. All 24 packed old sources match
their parent Git objects byte for byte. The ignored cache and lookup trace are
under `runs/gate_review_20260914/`; the index has 139 entries: 130 valid old/new
objects and 9 failed exploratory path lookups. Those failures are retained as
lookup failures, not evidence that a definition does not exist.

The labels are assistant-authored source-review judgments, not a human
annotation study or acceptance of the old regex's 231 declaration candidates.
Full scopes distinguish uses from declarations;
members are tied to their enclosing type. For example, `netkit.c` introduces
both a `struct netkit::pair` field and a function-local `pair` variable.
`nk->pair = pair` has two occurrence records with separate declaration scopes.
No global subtraction of the spelling `pair` is justified by one declaration.

Git line locations count newline characters only. `st.c` contains formfeeds:
Python `splitlines()` would incorrectly add line breaks. The review checker
uses LF-only indexing and validates quotes against those exact locations.

## Reviewed findings

Counts below partition individual code/header occurrences; they are **not
gate-v2 pass rates**.

| Source-review classification | Occurrences |
|---|---:|
| Target-owned local name | 111 |
| External to the packed target file | 106 |
| Not an identifier requirement | 5 |
| Unknown | 1 |

Of the 113 `(case, symbol)` pairs, 40 are uniformly local, 68 uniformly
external, 3 uniformly non-requirements and 1 unknown. One pair is mixed:
IB's `resv` is a non-requirement inside `#include <linux/dma-resv.h>` but an
external field name in member accesses. The labels therefore cannot be
applied by globally subtracting identifier spellings.

Several cases are useful counterexamples to purely lexical ownership rules:

- The firmware `kernel_image_offset`, `kernel_image_addr` and `dpy` are
  target-owned declarations. In contrast, `primary_display_table` in `efi.c`
  is an **extern declaration**; storage is defined in sibling `efi-init.c`.
- `PHYLINK_DISABLE_REPLAY` is a new target enum constant and
  `force_major_config` a new field of the target-private `struct phylink`.
  `WARN` resolves to a kernel macro. These three should not receive one
  undifferentiated “missing kernel evidence” label.
- IB's `ib_uverbs_dmabuf_file`, its `comp`, `dmabuf`, `dmabufs_elm` and
  `revoked` fields, and `ib_uverbs_dmabuf_done` resolve through sibling
  `drivers/infiniband/core/uverbs.h`. The pointer type selects the relevant
  struct; unrelated fields with the same spelling are not the proof.
- `CREATE_TRACE_POINTS` is defined in the target but its exact spelling is
  consumed by `include/trace/define_trace.h`. It is an external preprocessor
  protocol name, not a freely chosen local macro. The wrapper `DMA_BUF_TRACE`
  and its formal `FUNC` are target-owned. `__VA_ARGS__` is a preprocessor
  built-in and not a kernel identifier requirement.
- `SPINAND_OP(spinand, set_feature, ...)` constructs
  `spinand_fill_set_feature_op`; the macro selector is an external dependency,
  not a free variable. `VMA_NORESERVE_BIT` is generated by
  `DECLARE_VMA_BIT(NORESERVE, 21)`. Exact grep of the complete name misses its
  declaration. The trace calls likewise resolve through
  `DEFINE_EVENT` → `DECLARE_TRACE_EVENT` → `trace_##name`.
- `_enabled` in `FUNC##_enabled` remains **unknown** as a standalone gate
  requirement. Its composed name depends on the invocation; this review does
  not discard the suffix or pretend it has a standalone kernel declaration.

The external class is deliberately relative to the **packed target file**.
**31 external occurrences** have subtype `external_to_pack_sibling_driver`.
They include IB, i915/Xe interface objects and parent helpers. Their new
driver-source definitions are **evaluation-only collateral**, not admissible
kernel-definition retrieval examples. The annotation records this boundary;
none of the retrieved target/collateral source is added to generation context.

## Verification and use

```bash
python3 tools/review_gate_cases.py \
  --annotations datasets/gate_review_v2.json \
  --negative-controls \
  --out runs/gate_review_20260914/verification.json
```

The command verifies selection, all 113-name/223-occurrence coverage, input
hashes, exact parent-source equality, cached Git-object hashes, line/quote
locations and local-scope bounds. Five negative controls must fail: changed
source fingerprint, omitted judgment, wrong same-line occurrence, invalid
local scope, and assigning the external `resv` member classification to the
same-spelled include-path fragment. No judgment is generated by these checks.

The new metric may support fewer binding forms than this source review. A
reviewed static helper, private field, macro or conditional declaration must
remain `unknown` when the metric cannot prove the needed scope. Consumers
should report matched, honored and unsupported annotation counts separately;
source-review classification must not be changed merely to fit the implementation.

The frozen audit, corpus, CSV and pre-registration remain unchanged. These
annotations can calibrate a versioned gate, but cannot clear the historical
stop-rule or establish generalization without separately specified evaluation.
