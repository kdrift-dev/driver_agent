# M7 — Frozen PPL gate audit (2026-09-14)

The frozen result remains **116/200 gold passes (58%), 84 failures**, with
479 missing identifier occurrences and mean coverage **0.891058**. The audit
reproduces every historical value in **all 1,000 rows** (200 cases × five
conditions): coverage, pass, missing count, context characters and truncation.
There are zero mismatches. No model was called and no labels, historical CSV,
sample or pre-registration were changed.

The failures have multiple causes. They do not establish that 84 gold labels
are wrong or incomplete. Equally, this audit does not clear the pre-registered
stop-rule, certify the 116 passing labels as clean, or reinterpret the PPL
experiment as successful. A mention gate cannot establish those conclusions.

## Reproduction and provenance

`tools/audit_ppl_gate.py` independently pins the gate, comment/token handling,
evidence renderer, source concatenation and context-prefix rules from freeze
commit `924a8b5b3b0caa5bf4e1ae23a523bc37751a2af7`. It does not import the evolving
`ctxlab` metric. The exact four frozen source blob IDs are recorded in the
tool and report. Historical mismatches stop classification and invalidate any
previous aggregate in the same audit output directory.

Inputs are the exact 200 case directories copied from
`retro:/home/arina/work/driver_agent_ctx/runs/mined` into
`runs/gate_audit_input_20260914/` (1,000 files), together with
`runs/oacd_results/{ppl_sample_manifest.json,experiment_results.csv}`.
The aggregate records a SHA-256 for every copied file and these provenance hashes:

| Input | SHA-256 |
|---|---|
| Frozen manifest | `21dd28a584763c20cd4173c230fde385dc4e91106cfcbb8dd043dc978b53607c` |
| Historical CSV | `3ea05c81ac3ecc77f940ee520cae5bdb593a6e25f8320bd3eaf746a431e7313e` |
| Copied dataset content inventory | `5d24abea98ab14e85c39c9daeb184c0e5c090ba2c5e94fada8fdea95c989faae` |

The historical manifest's `dataset_digest` hashes all **579 eligible case IDs**,
not their contents. This audit does **not** claim to recompute that full-pool
digest from a 200-case copy. Exact selected IDs and historical output parity
are checked separately; content hashes make the audited copy identifiable.

```bash
python3 tools/audit_ppl_gate.py \
  --dataset-dir runs/gate_audit_input_20260914 \
  --manifest runs/oacd_results/ppl_sample_manifest.json \
  --historical-csv runs/oacd_results/experiment_results.csv \
  --out-dir runs/gate_audit_20260914

python3 tools/run_gate_audit_checks.py \
  --dataset-dir runs/gate_audit_input_20260914 \
  --manifest runs/oacd_results/ppl_sample_manifest.json \
  --audit-report runs/gate_audit_20260914/gate_audit.json
```

Both commands completed. The checks report **1,486 assertions/groups**: source
blob identity, parity against functions loaded from the actual frozen Git
objects on all 1,000 conditions, rendering parity, evidence locations for every
missing token, full-source/prefix separation, and a deliberately corrupted CSV
that must prevent conclusions. The expected mismatch message belongs to that
negative test, not the historical audit.

## Exhaustive triage of all 479 missing occurrences

Each occurrence means one `(case_id, identifier)` pair. Categories partition
occurrences; case counts overlap. Classification uses all packed references,
gold/distractor evidence and `kernel_side.diff`, with conservative regex
observations. Every record contains confidence, reason, exact added-driver
locations, kernel-diff locations when found, and full rendered evidence
locations. **No category claims proven semantic ownership.**

| Category | Occurrences | Cases | Confidence / interpretation |
|---|---:|---:|---|
| New driver declaration candidate | 231 | 52 | Medium: declaration, definition, parameter or label syntax; no complete C scope resolution |
| Indirection or external dependency candidate | 192 | 56 | Low: member/type/call/macro syntax; actual ownership and dependency remain unproven |
| Unresolved | 17 | 16 | Low: insufficient packed evidence for the preceding categories |
| Legacy lexical artifact | 16 | 6 | High: only numeric/character literal or angle-bracket include-path occurrences |
| Packed kernel diff omitted from gold context | 15 | 6 | High for the exact mention omission; semantic necessity is not established |
| Gold context budget truncation | 8 | 2 | High: present in full rendered gold, absent after the 8,000-character cutoff |

Examples illustrate why these cannot be collapsed into one label-completeness
diagnosis:

- `02d851b46b36__sound_soc_sdca_sdca_functions_c`: the missing
  `find_sdca_control_reset` is introduced as a static driver function at
  `reference.diff:9`. A newly chosen local name need not have appeared in the
  old driver or a kernel definition.
- `3a17ba6557e2__drivers_mfd_sec_acpm_c`: `x0d` comes from the numeric literal
  `0x0d` at `reference.diff:80`. The frozen identifier regex extracts this
  substring. Other examples include `dma`/`buf` from `<linux/dma-buf.h>`.
- In the `b626b1a1c9cc` bridge cases, callback names such as
  `hdmi_write_audio_infoframe` occur in the packed `include/drm/drm_bridge.h`
  diff but are absent from scored gold. This is directly observable omitted
  evidence, not a guess that a helper must exist elsewhere.
- `156442eb6e44__drivers_mfd_rk8xx_i2c_c` loses
  `RK801_SYS_CFG3_OTP_REG` at the context cutoff. Seven more cutoff misses
  belong to `589e934d2735__drivers_net_phy_phy_device_c`, including
  `phy_port_alloc` and `attach_mdi_port`. Twelve cases have truncated gold in
  total, but only these two have missing identifiers explained by that cutoff.

Indirection observations are recorded independently: **42** occurrences have
member-access/designated-initializer syntax, **14** have type-tag/typedef
spelling, and **42** have macro-argument/token-pasting syntax. These counts
overlap and are not resolved kernel dependency counts. Remaining cases include
callback objects, scoped macro variables and names absent from the packed
kernel excerpts. They require name resolution or additional old/new kernel
definitions before assigning a definite cause. No network lookup or kernel
re-mining was performed in this audit.

## Two limits of the previous interpretation

First, the failures are not concentrated in very large treewide changes.
They span **54 commits**; **83/84** failing cases have `treewide_scale ≤ 12`.
All **41** cases with coverage below 0.75 have scale at most 12. The only
failure with a larger recorded scale has scale 6,182. The earlier claim that
the 41 low-coverage cases were large treewide edits is unsupported by these
case metadata. The complete histograms are in the machine-readable summary.

Second, the historical gate used **the full old source**, while the PPL prompt
builder first kept only **12,000 source characters**. Of 200 cases, 153 exceed
that prefix; applying the same lexical gate to the prefix adds **405 missing
occurrences across 80 cases**. It changes 45 historical gold passes into
failures, leaving **71/200** prefix-gate passes. This is a diagnostic of the
input mismatch, **not** a replacement historical gate result or recomputed
PPL. It also does not model the backend's subsequent tokenizer/window limits.

## Artifacts and boundary

`runs/gate_audit_20260914/gate_audit.json` contains the aggregate, provenance,
all 200 case records and all 479 classified occurrences.
`historical_reconciliation.json` contains all 1,000 row comparisons;
`missing_identifiers.csv` is the flat occurrence table. Each `cases/<case_id>/`
contains `audit.json` and the full gold/distractor rendering for line citations.
These generated artifacts remain under ignored `runs/`.

All reference-derived observations are **evaluation-only**. They are not
added to agent input, used to repair labels automatically, or excluded to
manufacture an adjusted pass rate. Future metric or label changes require
separate, versioned evaluation; the frozen experiment and its unmet stop-rule
remain intact.
