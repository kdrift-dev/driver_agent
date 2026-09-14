# Xilinx substring v2 and post-hoc identity-edit results

The source proposed by both model responses passes exact application,
compilation and strict `.ko` linking in two private kernel copies under the
separate post-hoc identity policy. **Both canonical v2 edit sets remain rejected
for a no-op.** Each response contains the same two operations: one type change
and one unchanged allocation. The build result establishes a buildable source
change under the later policy, not successful original-protocol admission or
physical device correctness.

## Canonical v2 execution

Generation [protocol 22](22_exact_substring_pilot_protocol.md) and code were
frozen at `0e5ed41062c4973a5f0106a6c0d0dd667b5dde29` on
`codex/exact-substring-pilot`, then deployed to the clean retro checkout
`/home/arina/work/driver_agent_substring_edits`. The host launcher now defaults
to this checkout; its previous version is saved as
`/home/arina/bin/driver-agent-shell.before-substring-edits`.
The 116-file execution digest is
`d6de24d703b9af7f484d1832a94e5aa119280853084abae2b4875c1d73ff1a8d`.
Freeze SHA-256:
`f857319b43d210ce3c39f29b02f2a434c59cfb4c8bdb4fd1d8d2264d4decfb42`.

The new compiler permits exact unique original substrings. It preserves bytes
outside each span and still rejects no-op edits. The default whole-line v1
behavior and its historical request bodies reproduce unchanged. Both new arms
use the same full old target, minimal requirements and 62-block kernel pool, with
the original source/pool/requirement hashes. No earlier generated answer,
reference patch, new-driver source or manual replacement hint enters the model.

The newly rendered mandatory input is 15,942 characters, the full pool 62,565,
and the common quarter-pool cap 27,598. This counts canonical JSON containing
complete messages and the submitted schema. Both methods retain the changed
API block, selecting 18 and 20 blocks respectively. The frozen token-budget
flags remain false; native usage below is observed after generation.

| Observation | First-fit | Obligation-ranked |
|---|---:|---:|
| Messages/schema characters | 27,382 | 27,002 |
| Selected kernel blocks | 18 | 20 |
| Explicit client attempts | 1 | 1 |
| Provider input tokens | 12,079 | 11,915 |
| Provider output tokens | 595 | 659 |
| Provider-reported USD cost | 0.045162 | 0.045630 |
| Provider request / strict JSON | Succeeded / valid | Succeeded / valid |
| Submitted / changed / identity operations | 2 / 1 / 1 | 2 / 1 / 1 |
| Canonical edit admission | Rejected | Rejected |
| Canonical apply / compile / link / runtime | Not run | Not run |

Both responses report `anthropic/claude-sonnet-4.6`, `Anthropic`, `stop` and
native `end_turn`, without truncation. Reasoning, cache-write and cached token
counts are zero. The two explicit attempts cost **USD 0.090792** in returned
usage. There are no explicit retries or subsequent model requests; internal
provider retries are unobserved.

Both edit lists are identical. The substantive original span `[1776, 1904)`
begins at line 59 and changes the `aesctx` member from `struct crypto_aes_ctx *`
to `struct aes_enckey *`. The second span `[9588, 9662)`, line 352, repeats the
allocation unchanged. Both spans occur exactly once and do not overlap.
The model itself describes the second operation as redundant, but leaves it in
the edit list despite the explicit no-op prohibition. The canonical compiler
correctly reports `no-op edit is forbidden`; it creates no diff or build output.

## Separate post-hoc evaluation

[Protocol 23](23_identity_edit_forensic_protocol.md) was chosen after seeing the
v2 failure. It waives only the no-op prohibition, keeps every raw operation,
and applies the complete list simultaneously. Identity operations remain
audited and must satisfy the same path, nonempty/unique-match and disjointness
requirements as changed operations. No operation or replacement text is removed
or corrected. No reference patch or new-driver hint is used. This policy cannot
retroactively turn v2 into a successful generation experiment.

The evaluator and this new policy were committed as
`de8ef8f987eae2c982776e338666a3646d0e9873` before materialization. Its 118-file
execution digest is
`72f6e1bdea4eb6ad1a09db7a50ac0a7be44f74d02cf4a3f6561e0da6184fe6c4`.
The forensic freeze copies all 31 original generation files verbatim, plus old
source and the committed policy. Freeze SHA-256:
`c8f8f1144189f54d985fcd83bae680d558f9f03c5c87f659654e7ece5f8376db`.
Both complete lists materialize to the same diff, SHA-256
`207ba83d65f137a43158af130c8fb146edc2843208e643e427385ff7621b2abd`.
The resulting source-payload digest is
`db56785c12d3816884d51c2f607e6061aaf11e6ece224eaa6c925f0338ab1bdf`.
Each audit retains both submitted operations, marking one changed and one
identity. The evaluator makes zero model or build calls; builds run separately.

Both unchanged materialized diffs passed the existing strict validator at
`de8ef8f`; neither validator code nor model output was changed during validation.

| Post-hoc observation | First-fit | Obligation-ranked |
|---|---:|---:|
| Exact application, offsets/fuzz/repairs | Passed, 0/0/0 | Passed, 0/0/0 |
| Fresh target `.o` compilation | Passed, 6.131 s | Passed, 5.332 s |
| Strict `.ko` link | Passed, 2.270 s | Passed, 2.321 s |
| Resulting module bytes | 23,176 | 23,176 |
| Original donor/config/kernel preserved | Yes | Yes |
| Physical device runtime | Not run | Not run |

The two modules are byte-identical, SHA-256
`95ada500f723bf8f73da2a1e12941510d54fe1b8d68656950ca0029786a4c8c9`.
Both have 26 imports checked against the frozen kernel. Logs show a fresh target
compiler command, strict modpost consuming `vmlinux.o` without warning-only
flags, and a fresh final linker command. This is two executions of one identical
patch on one case, not two independent migration successes.

Each private copy starts from 92,203 tracked source files and 101,876 donor
artifacts (2,369,441,931 bytes). All other tracked source bytes, the frozen
configuration, `vmlinux`/`vmlinux.o` and associated symbol/command files remain
unchanged. No mutable inode is shared with the donor. Both builds preserve the
original `6.19.0-rc5+` release and module vermagic, exercising the release pin
introduced in the previous package. The frozen configuration disables symbol
CRC versioning; that feature and physical device runtime are untested. Private
kernel trees remain on retro; all indexed non-kernel artifacts are copied locally.

## Verification and scope

All 27 focused offline groups pass locally and on retro: v1/v2 exact-edit
contracts, paired-context invariants, strict build validation and both bounded
generation runners. V2 fixtures use unrelated synthetic substrings and verify
independently expected source bytes with actual Git application. An initial
remote preparation-directory layout error was corrected before any model call;
all 12 copied input files retained their exact bytes, and the failed setup log
is preserved alongside the passing rerun.

Four additional identity-interpreter groups pass locally and on retro, using
only synthetic edit lists and a synthetic pinned generation directory. They
cover identity-inclusive ambiguity/overlap, complete-list preservation,
independently expected bytes, real Git application, pinned input tampering,
output reuse and interrupted evaluation. These make 31 focused groups across
the generation and evaluation packages.

The canonical generation audit passes 599 checks, reproduces selection and
counts independently, binds raw responses to the recorded requests and confirms
both no-op rejections. Its SHA-256 is
`c5d5f70b8a39e3d93329d41145c2adf0004af223c523d6e1f64404f081ef6a3c`.
The separate canonical no-build audit passes 343 checks and verifies unchanged
controls/donor plus absence of canonical build artifacts. Its SHA-256 is
`b9ecd4161d0e7900953999399f0935ed17d2c40d0b6ad70dd66fcedc5b2d0ff9`.
Read-only preflight passes 172 checks, including tool versions, all 139 frozen
control artifacts and the complete donor inventory.

Independent post-hoc reconstruction passes 791 checks, including direct UTF-8
byte application of every operation, exact raw copies, committed protocol,
source/diff hashes and real Git application. Its SHA-256 is
`5a567a04b3dc7aa22f2e740eb10236eec288e9d5f93b4edc705d1a1e4833525f`.

The independent build audit passes 996 checks, including the frozen raw-list
chain, fresh compiler/modpost/linker evidence, ELF metadata, all 26 resolved
imports per module and donor preservation. Its SHA-256 is
`664748cc87dafff3d6350e6784d4ce9d6a4f78c574630ec87c0e822585d0f45b`.
Both module binaries also match the frozen positive control byte for byte;
the proposed source is not byte-identical to that control's source. The first
build-audit report wrongly counted `make --version` as a build command. That
report and script are retained with a correction record; the corrected audit
excludes the version probe. No build or model request was repeated.

Canonical generation: `runs/substring_edit_generation_v2_20260914/`.
Offline checks: `runs/substring_edit_checks_20260914/`.
Independent audits: `runs/substring_edit_independent_audit_20260914/`.
Post-hoc materialization: `runs/identity_edit_forensics_v1_20260914/`.
Post-hoc validation: `runs/identity_edit_validation_v1_20260914/`.
The [result catalog](../datasets/substring_edit_pilot_v2.json) pins these records
and their exact hashes, with canonical and post-hoc outcomes separated.

This is one repeatedly inspected development case. Different context sets have
not produced a demonstrated selection advantage, and character limits do not
establish equal provider-token budgets. Kernel module build evidence does not
establish physical Xilinx runtime behavior. The prior failed protocols, original
dataset splits, previous clean checkouts and dirty evolve work remain preserved.

Next, choose an explicit production identity-operation policy and freeze it
before fresh cases. This diagnostic supports investigating that policy, not
retroactive relabeling of v1/v2 failures. Qualify additional independent migration
commits and establish provider-bound token accounting before a confirmatory
context-selection comparison.
