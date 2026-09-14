# Xilinx generation pilot: preparation, not frozen

Status: **NOT_FROZEN**. No generation request has been sent. This document
records the generation boundary and the implementation gaps that must be
resolved before comparing policies. It does not alter the reserved pressure
manifest or retrospectively preregister a result.

## Unit and allowed input

The target is `drivers/crypto/xilinx:xilinx-trng.ko`, from parent
`9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5` to migration commit
`b2c15db74a379a50d723002799c3db0c6781c75a`.
The preceding translation-unit census demonstrated an old-on-new interface
mismatch. Admission to a generation pilot also requires the separate strict
module-link controls; a produced `.o` alone is insufficient.

Generation may consume the complete old target source, machine-derived
kernel-side definitions/diffs, obligations, and guidance derived only from
those inputs. The target new source, reference diff, sibling driver source,
reference-based gate labels and hand-written replacement hints remain
evaluation-only. Build diagnostics are evaluation-only in the initial
one-shot pilot; a later repair-feedback condition would be a separate arm.

Use the production Kbuild target resolver and retain its source list and
local-include risks. Do not substitute the three blocks from the mined
comparison pack for a production-derived candidate pool. Its frozen
3692/4201-token costs refer to another renderer and Qwen tokenizer.

## Why a direct flag comparison is not ready

At implementation `22b0604`, `DRIVER_AGENT_CONTEXT_PRUNER` operates on
`old_module_sources`. It ranks optional read-only old-source blocks; it does
not select arbitrary kernel-definition/diff evidence blocks. In `full_module`
mode each full file is mandatory, so a one-file target offers no optional
source choice to that selector.

The OFF path always caps source blocks and measures compact payload JSON
characters. The ON path preserves mandatory source and measures the complete
rendered prompt in characters. The OFF path also embeds budget diagnostics
in model input, whereas ON records them outside it. Merely assigning both
paths the same environment-variable value does not establish equal actual
prompt budgets or isolate ranking. In focused mode the flag can additionally
alter graph expansion and extraction caps, before the final selection step.

## Observed production dry-run, 2026-09-14

The production dry-run completed on retro at `22b0604`. The CLI returned 2
because it had no generated patch candidate; its patch-generation status is
`dry_run`, and every validation stage is `SKIPPED`. No worktree build or model
request was made. The source resolver returned exactly one 430-line C file,
no headers, no unresolved targets and no local-include risks. Its bytes equal
the frozen packed old source, SHA-256
`088cbd398390ad9651d4066d50bf9f413e5ea6991ca49cc7a9b03a89c8863e5f`.

One kernel signature change produced the required obligation for
`crypto_drbg_ctr_df`; no reference patch was supplied to this analysis.
The four renders below reuse the same extraction and deterministic dry-run
pre-spec/reasoning artifacts. The ON focused re-render reproduces the original
production prompt byte for byte. The configured character cap is 180,000 in
all four rows, and is not binding here.

| Source mode / flag | Actual prompt chars | Qwen user-prompt tokens, diagnostic | Source blocks | Source truncated |
|---|---:|---:|---:|---|
| focused / OFF | 26,063 | 8,201 | 5 | Yes |
| focused / ON | 38,514 | 12,025 | 10 | No |
| full_module / OFF | 16,466 | 4,320 | 1 | Yes |
| full_module / ON | 34,885 | 11,278 | 1 | No |

These token counts use the cached Qwen tokenizer snapshot
`8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301` with Transformers 4.57.1. They count
the exact user prompt without a system message or chat framing. They are
neither Claude input-token counts nor a claim that the old 4096-token pressure
budget admits a production generation prompt.

Focused ON has four mandatory blocks and six optional blocks. All six
optional blocks have zero overlap with the obligation symbols, so their
ranking in this case is driven entirely by the size penalty. The mandatory
prompt alone is 32,245 characters. A diagnostic 35,000-character cap yields
34,665 characters and keeps three optional blocks; at 32,245 it keeps none.
At 32,244 the exact same intact mandatory prompt is retained and the outcome
is `input_budget_exceeded`. This verifies budget refusal on the real input,
but supplies no evidence of useful obligation-based selection.

The full-module ON payload preserves all 14,056 line-numbered source
characters. OFF reduces it to 2,409 characters with an explicit truncation
marker, despite the nonbinding global cap. A generated-patch difference
between these rows would therefore be confounded with input-source retention.

Full artifacts and offline re-render/count scripts are in
`runs/xilinx_generation_dryrun_20260914/`, both locally and under
`/home/arina/work/driver_agent_build_eligibility` on retro. The index pins 93
files; `artifact_index.json` SHA-256 is
`d9155ece71c195106aedfd501f956b1bb9ce5ccd7471b30880956a02600547d7`.
`prompt_diagnostics.json` SHA-256 is
`e8d6be9b896653075a5ed1c830dc75c5f7a83dcfd2806bbaa989196eaf69c90a`.
`qwen_token_diagnostics.json` SHA-256 is
`c49e14f70b565fba686d0a831d8a66ce6e43da9405326d4665046390a2ffec96`.
The preserved source-policy rows are diagnostics, not frozen experimental
conditions or generated-patch results.

An independent input audit verified all 93 indexed files, ten saved input/prompt
pairs, 44 old-source sections, and 126 old/new kernel excerpts (1748 numbered
lines) against the corresponding Git blobs. It found no reference diff,
driver diff, sibling-driver source path, commit subject or commit message in
the checked inputs. Ref hashes remain in `case_id`. One generic label says
"in-tree migrations", although its recorded helper evidence comes from the
public kernel header; that wording should be corrected separately. Two OFF
truncations split source lines, with retained fragments matching old Git bytes.
This provenance audit does not prove the absence of semantic leakage.
The audit is `runs/xilinx_generation_input_audit_20260914.json`, SHA-256
`a8436ffce54b4cec932c6f2a6b9356fa63ffd65967668e568b403eb924bb0894`.

## Offline preparation

Run production analysis with `--dry-run`, one candidate and one iteration,
strict prompting, Kbuild target scope, commit information disabled, and both
private fixup flags unset. The candidate model string below is only metadata:
`--dry-run` disables the provider and therefore does not read credentials or
send model requests.

```bash
env -u DRIVER_AGENT_ALLOW_SYNTHETIC_REPLACEMENT_HINTS \
  -u DRIVER_AGENT_ALLOW_CASE_SPECIFIC_FIXUPS \
  DRIVER_AGENT_CONTEXT_PRUNER=1 \
  DRIVER_AGENT_PATCH_INPUT_BUDGET_CHARS=180000 \
  python3 pregen_driver_agent.py \
    --repo /home/arina/repos/linux \
    --old-ref 9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5 \
    --new-ref b2c15db74a379a50d723002799c3db0c6781c75a \
    --module-path drivers/crypto/xilinx --kbuild-target xilinx-trng.ko \
    --out-dir runs/xilinx_generation_dryrun_20260914/focused_on \
    --jobs 4 --dry-run --llm-provider openrouter \
    --llm-model anthropic/claude-sonnet-4.6 \
    --llm-max-tokens 4096 --llm-max-retries 0 \
    --patch-candidates 1 --max-patch-iterations 1 \
    --patch-source-mode focused --patch-prompt-strength strict
```

Reuse the resulting frozen extracted data and deterministic dry-run guidance
to render the remaining policy/source-mode combinations without regenerating
upstream guidance. Retain exact `input.json`, `prompt.md`, source coverage,
selection audits, code identity and artifact hashes. A Qwen tokenizer count
can be an explicitly named diagnostic; it is not the selected provider's
input-token count.

## Smallest useful next implementation

Create one shared selection input containing immutable mandatory source and
a production-derived list of optional kernel-evidence blocks. Define the
block boundaries and stable order before observing generated patches. Keep
obligations and deterministic guidance identical across conditions. Compare
the same whole-block first-fit control with obligation-ranked whole-block
selection using one shared prompt renderer and budget counter. This is an
explicit new kernel-evidence experiment, not a claim that the current native
flag already implements that comparison.

Freeze an actual generation-model token counter or a provider-supported
counting endpoint that includes system/user messages and framing. Retain the
counting implementation/version and require the final request to satisfy the
same input-token cap in both conditions. Keep requested output tokens separate
and equal. No padding is needed: report actual consumed input tokens as well
as the common cap. Choose a cap from source/candidate costs before generation,
where mandatory input fits and at least two feasible optional blocks compete.
If no such cap exists, this case cannot test the selection mechanism.

The candidate provider/model is `openrouter/anthropic/claude-sonnet-4.6`,
following the tracked README and four-layer experiment defaults. The general
pipeline CLI still defaults to `openai/gpt-4.1`, so an explicit model argument
is required. Neither alias availability nor a fixed provider snapshot has
been checked in this offline preparation. Before freezing, retain requested
and returned model/provider identity, request parameters, full response,
usage and finish reason. The current `LLMClient` returns content only and
discards response metadata, so it cannot supply that audit by itself.
Its OpenRouter subprocess path also catches a completed provider error and
falls back to an in-process request. Consequently, `--llm-max-retries 0`
alone does not guarantee one provider attempt. Before dispatching the pilot,
separate process-setup failure from an attempted request failure and record
each actual attempt; do not silently resubmit after a provider error.

## Bounded generation and validation after freeze

The initial unit is one case, one candidate per condition, temperature zero,
one generation iteration and a fixed equal output cap. Disable retries that
could silently add samples; record any explicitly repeated infrastructure
attempts. Use frozen deterministic pre-spec/reasoning artifacts rather than
making fresh upstream model calls independently for each condition. Pin
condition order before dispatch. Provider/auth/quota failures and output
truncation are distinct outcomes, not patch failures or no-op successes.

Retain raw output, parsed patch, any generic line-number repair and the exact
final patch. Require apply-to-old success, effective code changes and edits
confined to the resolved target boundary. Validate the patched old target
against the same strict target-kernel configuration and dependencies used by
the module controls, with fresh object/module artifacts and no ignored
modpost errors. Report apply, object compile, module link and semantic review
separately. Reference comparison begins only after generation and cannot feed
back into prompt selection or patch fixes. Xilinx hardware behavior remains
unexercised unless a suitable separate hardware test exists.

This one-case pilot checks the path from allowed evidence through generated
patch to controlled validation. It cannot establish an effect size, method
superiority, a benchmark pass rate, or hardware correctness.
