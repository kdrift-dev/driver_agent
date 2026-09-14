# Paired kernel-context preparation v1

This preparation protocol is fixed before its canonical Xilinx replay. It
implements the prerequisites identified in report 15. It sends no generation
requests and does not change the native source-pruner flag or its default.
The selected model remains a candidate; generation admission is a separate
explicit check, not implied by a successful offline preparation.

## Shared input and renderer

Use the existing production extraction from parent
`9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5` to target
`b2c15db74a379a50d723002799c3db0c6781c75a` for `xilinx-trng.ko`.
Load only the three production source/definition documents and complete old
target files. Verify target inventory against the production Kbuild resolution,
all old source bytes against the parent Git blobs, and each admitted kernel
excerpt against the corresponding old/new blob. Reject unsupported source
scope, driver-side kernel evidence, commit information and reference metadata.
Hash the bytes actually loaded before selecting context; check them again at end.

This explicit experiment uses `full_module`: all 430 old source lines remain
unchanged in both conditions. It does not redefine production's focused default.
Mandatory guidance contains only generic patch instructions, changed-symbol
identifiers and their old target use locations. No pre-spec/reasoning prose,
replacement annotations or expected new argument types are copied into that
mandatory section. This prevents selected-out kernel details from remaining
available through duplicated guidance.

Each optional unit is a complete production excerpt pair. Equal old/new
excerpts are rendered once; a focused diff is recomputed from their actual
source text. Deduplicate equal excerpt pairs across scanner kinds. IDs bind
symbol, refs, paths, ranges, bytes and diff; order is path/range/symbol order.
Record missing/ambiguous extraction explicitly rather than inventing evidence.
These are locator excerpts, not a guarantee of complete C definitions.

## Two selection conditions

Both conditions share mandatory source, requirements, candidate pool, model
string, output limit, instructions, request builder, counter and common cap:

- `first_fit`: try each complete block in frozen pool order; skip a block if
  the complete rendered request exceeds the cap.
- `obligation_ranked`: prioritize uncovered obligation-symbol overlap, then
  smaller measured marginal input count, then frozen index. Recompute overlap
  after each accepted block. No fitted coefficients or reference labels enter.

After selection, render blocks in the original pool order. Therefore equal
selected sets give byte-identical messages; ranking order itself is not a
second experimental difference. Keep method names, decisions, counts and
scores outside model input. Never clip an old source file or evidence block.

For the canonical preparation, derive the cap before evaluating either policy:

`mandatory + ceil(max(0, full_pool - mandatory) / 4)`.

This reserves one quarter of the full pool's incremental measured size. The
rule is a fixed diagnostic pressure setting, not quality tuning. Also support
an explicit positive common cap. If mandatory input exceeds it, retain the
whole source, record refusal and forbid dispatch. Pressure eligibility requires
mandatory input to fit, the full pool not to fit, and at least two individual
blocks to fit. Record all outcomes even if both policies select the same set.

## Counters and generation boundary

The character counter measures canonical JSON of the complete system/user
messages and labels that quantity as characters. The HF counter measures the
entire message sequence with the pinned chat template and assistant generation
prefix after every selection trial and again at final render. Do not add
individual block-token counts: tokenization is nonadditive.

Canonical HF snapshot: `Qwen/Qwen2.5-Coder-0.5B` at
`8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301`, local files only. Save tokenizer-file,
template and library identities. The candidate request is for
`anthropic/claude-sonnet-4.6`, temperature zero, one candidate, 4096 output
tokens. **Qwen chat-template counts are diagnostic for this Claude request.**
Even matching an alias would not prove matching server-side serialization.
Both built-in offline counters therefore report `generation_ready=false`.

A future provider adapter must verify model/revision/framing compatibility.
The dispatch guard also requires the same counter identity, cap, request hash
and final measured input count. Mismatches refuse dispatch. The current CLI
has no inference switch and never constructs an enabled provider.

## Request accounting

`build_chat_request()` is shared by preparation and both existing providers.
Each stage now saves `request_body.json` and `provider_audit.json`, including
the full response, requested/returned model, returned provider, usage, finish
reason, and each actual HTTP/SDK attempt. Credentials are redacted and auth
headers are not part of the request body. Transport failure, JSON parsing,
empty content and output truncation remain separate observations.

The configured retry loop is the only retry mechanism: OpenAI SDK retries are
disabled; after an OpenRouter worker starts, errors cannot resubmit in the
parent. The parent drains IPC while enforcing its deadline, including partial
frames. Timeout/worker-crash records preserve observed attempts and explicit
count uncertainty; these are not claims about server receipt or billing.
Existing `complete() -> str` and parsed `call_json()` return shapes remain.
Future pilot validation must inspect response metadata, not parse success alone.

## Verification and preservation

Use synthetic negative controls for forbidden evidence, source tampering,
nonadditive counts, equal-set rendering, whole-source overflow, mutated counter
requests, changed dispatch data, retries, missing metadata and IPC deadlines.
Run existing pruner and ctxlab regressions. Replay the frozen production input
on retro in a fresh checkout/output directory, hash every artifact, and verify
the old input index remains unchanged. No kernel rebuild, PPL scoring or
hardware execution is required for these prompt/client changes.

Record canonical results in a separate report after execution. This one case
can validate implementation contracts and reveal selection differences; it
cannot demonstrate generated-patch quality, effect size or method superiority.
