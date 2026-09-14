# Xilinx paired character-budget diagnostic v1

This protocol is frozen before its two generation attempts. It is a separate,
exploratory continuation of the offline preparation in reports 16–17. The
strict token-budget protocol remains unfulfilled; its saved
`generation_ready=false` values and all prior artifacts remain unchanged.

## Admission and purpose

The available OpenRouter credential passes the read-only authentication check.
The public model catalog and endpoint listing include
`anthropic/claude-sonnet-4.6`, canonical slug
`anthropic/claude-4.6-sonnet-20260217`, with an Anthropic endpoint.
The official [OpenRouter API schema](https://openrouter.ai/openapi.json) has no
pre-generation token-count endpoint. Its `/models/count` counts models. The
[direct Anthropic counter](https://platform.claude.com/docs/en/build-with-claude/token-counting)
requires separate credentials and is documented as an estimate; it would not
verify OpenRouter's serialization. These findings do not justify substituting
the cached Qwen tokenizer for Claude.

The present run answers a narrower question: can the two already prepared
context selections produce applicable patches that pass the established strict
Xilinx module build? It fixes a common **character** cap and reports the actual
provider input-token usage afterward. It does not establish equal token budgets,
a context-selection advantage, an effect size, or a new confirmatory result.
The case and both selections are already known; this is not a held-out study.

## Immutable inputs and requests

Reuse `runs/paired_context_xilinx_chars_v1_20260914/`, prepared at `446e42c`.
Its artifact index SHA-256 is
`6a83a8933bbe06ef9f2fd08462f7d3d24bcd8fdd0df6db666382d315a7adc638`.
The verified 62-block pool and complete 430-line old source remain identical.
The old/new target refs are `9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5` and
`b2c15db74a379a50d723002799c3db0c6781c75a`.

First-fit retains 18 blocks and counts 26,316 message-JSON characters; ranked
retains 20 and counts 25,936. Their common cap is 26,532, from the already frozen
quarter-pool rule. Both retain the changed `crypto_drbg_ctr_df` block. Replay
selection and verify indexed artifact bytes before admitting either request.
No model output, target new-driver source, reference diff, sibling driver,
compiler diagnostic or hand-written replacement hint may enter these inputs.

Keep the exact prepared system/user messages, temperature zero and 4096 output
tokens. Add identical transport options to both requests: Anthropic-only
routing with fallbacks disabled and required-parameter support, reasoning
explicitly disabled, and `plugins: [{"id": "context-compression", "enabled": false}]`. The
[routing policy](https://openrouter.ai/docs/guides/routing/provider-selection),
[reasoning option](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
and [transform switch](https://openrouter.ai/docs/guides/features/message-transforms)
are documented by OpenRouter. These settings do not assert knowledge of its
internal chat framing. Freeze both complete outgoing JSON bodies and hashes,
code identity, input hashes and condition order before constructing an enabled
provider. Recheck the freeze before each dispatch.

## Request and result accounting

Attempt first-fit once, then ranked once. Each uses one candidate, zero retries
and a 300-second deadline. There are at most two explicit client HTTP requests;
server-internal work is not independently observable. No continuation, hunk
repair, response retry, second candidate or feedback iteration is admitted.
A shared authentication/quota failure skips the remaining arm and is recorded
as a provider failure. All attempted outcomes remain in the report. A dispatch-intent
record precedes each call; incomplete calls retain unknown attempt counts.

Save request bodies, raw redacted responses, parsed responses and complete
per-attempt audits. Record returned model/provider, native finish reason, input,
output, cached/reasoning usage when present, and returned cost. Missing usage
stays unknown. Stop/truncation, JSON/schema failure, no candidate, empty patch,
apply failure, compiler failure, link failure and infrastructure failure remain
separate outcomes. Raw output must be one strict JSON object; surrounding
prose/fences, duplicate keys and extra schema fields are rejected. An output limit or usage count is not evidence that the
patch is complete. Unexpected model/provider routing is an admission failure.

## Patch validation

Accept at most one unified diff for the sole target
`drivers/crypto/xilinx/xilinx-trng.c`. Apply its original bytes to the exact old
Git source in a fresh staging directory without repair. Hunk counts and
coordinates must match exactly, with no offset search or fuzz. Reject empty/no-op
patches, extra paths, renames, mode changes and binary patches. Save the patch
and resulting source hash independently of later build outcomes.

For applicable candidates, use an isolated ordinary copy of the already built
new-kernel control tree and verify the frozen donor configuration and kernel
artifacts. Do not hard-link writable build inputs or modify the donor. Remove
stale target build outputs and require a fresh compiler invocation, strict
modpost consuming the verified `vmlinux.o`, successful final link, and a new
ELF `.ko`. Reuse the configuration and gates in report 14; record compile and
link separately, plus input/artifact hashes and donor integrity afterward.
No new target/reference source enters generation through this evaluation step.

Report runtime as untested: this Xilinx hardware driver has no exercised device
path in the experiment. Inspect each generated change against the provided
kernel evidence and old-source behavior as a separate semantic review, without
turning reference similarity or a successful module build into full correctness.
Do not tune the prompts, caps or selection after observing either response.
