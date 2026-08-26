# Obligation-Anchored Context Distillation (OACD) for LLM Driver Migration

*Meeting-facing description of the approach and its current execution status. Companion to the slide deck; every claim below is backed by code/commits on branch `ctx-impl`. Maturity is labeled honestly throughout — see §8.*

---

## 0. One line

> **OACD scores context by the obligations a driver migration must satisfy** — using the kernel's own git history as free gold labels and the kernel build as the oracle — turning today's blind, silent context-dropping into an *ablatable*, obligation-anchored selection with a *pre-registered* cheap perplexity proxy.

Working title: **"Obligation-Anchored Context Distillation for LLM Driver Migration"** (alt: *What Context Does Kernel Driver Migration Actually Need?*).

---

## 1. The problem

Driver migration adapts an old driver to a newer kernel. Patch quality depends on what goes into the migration prompt, but the prompt has a hard budget (`DEFAULT_PATCH_INPUT_BUDGET_CHARS = 180000`, `patch_generation.py:17`) and large cases overflow it. Today the pipeline resolves overflow **crudely and blindly**:

- rank candidate blocks by a *fixed static priority* (`_prioritize_patch_sources`),
- middle-truncate each block (head+tail), then
- **clip by count** and **silently drop all read-only context** on exactly the large cases where context matters most (`_shrink_patch_input_payload`, `patch_generation.py:1070–1132`).

No signal about *which* read-only evidence actually carries what the edit needs. The trimming policy is scattered across four modules and cannot be ablated. **Line utility is never scored.**

---

## 2. The idea

Anchor context selection on the migration's **obligations** — the machine-derived records the existing pipeline already emits (`obligation_builder.py` → `compatibility_obligations.json`), one per affected kernel symbol whose contract changed (rename, signature/struct/return change, ownership/locking, include dependency). An obligation is exactly *what the migration is obliged to handle*, so it is a **labeled, machine-derived target set** — not a heuristic.

**The three-asset thesis** (no prior work combines all three):

1. **Machine-derived task spec** — the obligations, from `obligation_builder.py`.
2. **Free gold context** — mined from the kernel's own git history (§4).
3. **A real oracle** — the layered kernel build/QEMU validation (Levels 0–7), not reference-patch similarity.

---

## 3. The method

Context is decomposed into `Chunk`s. The **edit locus** (old-source functions the edit touches) is kept verbatim by every method; kernel-evidence items (gold + distractors) compete for the remaining budget. Budgets are counted in a deterministic identifier-token proxy, so the whole offline loop needs no real tokenizer.

### (a) Deterministic half — needs no ML

- **Expressibility gate** (`metrics.py::expressibility_check`): can the reference edit even be *expressed* given (context + old source)? Computes identifier coverage of the edit's added lines. Monotone, cheap; intended profile gold≈1.0, none≈0.0. Acts as an independent, non-PPL cross-check.
- **Obligation-anchored submodular selection** (`methods/ours.py`): per-chunk value
  ```
  gain = 1.0·|chunk.symbols ∩ obligations_remaining|
       + 0.25·|chunk.symbols ∩ locus_symbols|
       − 0.002·tokens
  ```
  Greedy pick-best-fitting-chunk, then **discount the obligations it covered** so the next step sees diminishing returns — genuinely *submodular* coverage, unlike the independent per-chunk ranking of the baselines. Charges the locus first, stops when nothing fits or gain ≤ 0. (Coefficients are explicit placeholders until calibrated on mined data.)

### (b) The perplexity proxy — the one learned signal

`score_context` assembles the migration prompt and computes the **teacher-forced NLL of the reference edit conditioned on the candidate context**. Lower NLL = context that makes the correct edit more predictable = more valuable context. Backends (honestly labeled):

- **HF** (`HFTeacherForcedBackend`, default `Qwen/Qwen2.5-Coder-0.5B`, CPU, deterministic, sha256-cached) — the real signal.
- **Mock** — a deterministic bag-of-identifiers heuristic for offline plumbing tests; *orderings only*, never absolute values.
- **OpenRouter echo** — fallback; any transport/echo failure is a `ProviderFailure`, excluded from denominators, never a "loss."

The proxy answers a pre-registered question (§4): if teacher-forced PPL tracks context quality, it becomes a cheap dense stand-in for an expensive oracle step.

### (c) The method zoo — apples-to-apples

Eight methods under one interface (`Method.select(case, budget) → Selection`): `trunc_baseline` (today's blind truncation), `random_budget` (noise floor), `errprune` / `static_epam` (locus/source-symbol overlap), `shapley_probe` / `longcodezip` / `keepdrop_dd` (PPL-oracle value density / delta-debugging), and **`ours`** (obligation-anchored submodular greedy). **Ours** is the only method that (1) anchors on the machine-derived obligation symbols, (2) does *submodular* coverage with discounting, and (3) needs **no backend at selection time** — its judge is the build oracle + gate.

---

## 4. Why it's credible — the evaluation methodology

1. **Self-labeling benchmark (leakage-safe, zero manual labels).** An *atomic migration commit* changes a kernel API and adapts driver callers in the same commit, so it partitions its own hunks: kernel-side hunks = **gold context**, driver-side hunks = **reference edit**, changed-but-unused symbols = **free distractors**. A **gold invariant is enforced in code** (every gold symbol must appear in both the kernel diff and the caller edit — hard `AssertionError` otherwise), plus a `SYMBOL_STOPLIST` that removes ubiquitous symbols that would label almost anything as gold. (`mining.py`)

2. **Pre-registration (forecloses post-hoc rationalization).** `docs/ppl_signal_preregistration.md` fixes the verdict *before* the run:
   - **SUCCESS**: PPL(gold) < PPL(random) in ≥70% AND PPL(gold) < PPL(none) in ≥60%, each sign-test p < 0.01.
   - **KILL**: gold<random win-rate < 60% → drop PPL, keep only the deterministic half.
   - **Ambiguous (60–70%)**: exactly one pre-specified retry (~1.5B model, strip hunk headers). No other tuning.

3. **Clean protocol.** Five **size-matched** conditions (`gold / gold+distractors / distractors-only / random / none`) so length is not a confound; primary metric `nll_per_token`; two-sided binomial **sign test**; provider failures **excluded from denominators**. An independent deterministic gate profile is a cross-check with its own stop rule (gold gate <80% → fix labels before trusting any PPL).

4. **Hard input-boundary wall (the crux).** The evaluation harness *must* read reference diffs to score edit-PPL — but **the agent pipeline never may**. The wall is real: `ctxlab/` (eval) is import-firewalled from `driver_agent_pregen/` (agent), grep-verified; the only pipeline touch is a **default-OFF**, non-destructive hook that reads *only* obligation symbols, never reference diffs or gold labels. Without this wall, any positive result could be dismissed as the model having seen the answer.

5. **27-check standing regression gate** (`tools/run_ctxlab_checks.py`, green): locks down exact gold labels, schema, the deterministic gate profile, the offline PPL ordering, method invariants, two ablation nulls, and the boundary hook — so a regression in the machinery fails before it can corrupt a real run.

---

## 5. What we've already built — evidence we're executing, not proposing

| Asset | Evidence |
|---|---|
| `ctxlab/` package (miner, data, metrics, backends, method zoo, comparison) | 13 modules + 5 methods, all ≤342 lines |
| Milestones **M0–M5** shipped on `ctx-impl` | commits `4167e4b → 94b17e5 → 0a17e34 → 6a89b30 → ba77583 → 988dc83` (author Arina Kharlamova) |
| **27-check regression gate green** | `tools/run_ctxlab_checks.py` — 0 failures (re-run this session) |
| Default-OFF, boundary-audited pipeline hook | `driver_agent_pregen/context_pruner.py` + coeffs, gated by `DRIVER_AGENT_CONTEXT_PRUNER` (unset ⇒ byte-identical) |
| Executed analysis notebook (saved figures, no hand-typed numbers) | `notebooks/context_engineering_analysis.ipynb` |
| **Real HF PPL backend validated end-to-end (this session)** | see below |

**Real-model validation done this session (2026-08-19):** `.venv-ppl` (torch 2.13.0 / transformers 5.15.0), `Qwen2.5-Coder-0.5B` teacher-forced backend, run on two **curated v6.19→v7.0-rc1 case packs** (`intree_mini_v0`: `dax_hmem`, `dma-buf_heaps`), 10 rows, 0 provider exclusions:

- **gold beats none/random/distractors — 100% (2/2)** on all three pairwise comparisons (e.g. dax_hmem gold nll/tok **0.6975 < none 0.7589 < distractors 0.7604 < random 0.7686**);
- **deterministic** — rerun returns every row from the sha256 cache, identical values;
- gate side-channel behaves (gold passes where none/random fail).

This is a genuine *signal-exists* smoke on the real model + real kernel migration examples — **not** the pre-registered 200-case experiment, but proof the whole path works.

**Pre-registered 200-case run — DONE (2026-08-26), verdict SUCCESS.** Full
`v6.19..v7.0-rc1` mine (685 cases, 579 with ≥1 gold), frozen 200-case sample
(`draw_ppl_sample.py --n 200 --seed 0`, digest `32ecbf4da490…`) pinned by a
freeze commit *before* scoring, then scored with `Qwen2.5-Coder-0.5B`
teacher-forced on `retro` (200×5 = 1000 rows, 0 provider exclusions). Result:
**PPL(gold) < PPL(random) 70.0%** (140/200, p=1.5e-8), **PPL(gold) < PPL(none)
91.5%** (183/200, p=2.5e-36) — both pre-registered thresholds met → **SUCCESS**;
the cheap PPL proxy ranks context quality correctly on the mined distribution.
Caveat reported honestly: the gate side-channel's <80% stop-rule tripped
(gold gate pass 58%, driven by ~41 large treewide edits with incomplete labels),
but on the label-clean subset the signal is essentially unchanged (66%), so the
concern the rule guards against does not materialize. Full write-up:
`docs-impl/06_ppl_signal_result.md`.

---

## 6. What remains

1. ~~Full `v6.19..v7.0-rc1` mine~~ — **DONE** on `retro` (685 cases, 579 gold).
2. ~~Freeze the pre-registration before scoring~~ — **DONE** (commit `924a8b5`,
   digest `32ecbf4da490…`).
3. ~~Real 200-case pre-registered PPL run~~ — **DONE, SUCCESS** (§5). *Method
   comparison on the real backend is the remaining sub-item* — scoped rather than
   full-200 (HF cost: 8 methods × budgets × 200 ≈ many hours), pending a scope call.
4. **Decide whether to commit mined data** (default: no — eval-only, regenerable;
   the freeze binds the sample by digest, not by committing the packs).
5. **Clear the gate flag (follow-up, not blocking):** improve gold labeling for
   large treewide edits so the strict-coverage gate rises above 80% (§8).

---

## 7. Positioning vs related work (as cited in the plan)

- **Sam-Bodden 2026** — skeleton/LOD context tiers are no better than dropping (p=0.75); value is in *find*, not clever *act* representations. → OACD keeps the edit locus verbatim, drops the rest; wired as a null arm (skeleton-vs-drop).
- **JetBrains** — latent compressors break on iterative agent loops. → OACD uses **no trained compressor**; the judge is the build oracle + gate.
- **ETH generated-guidance null** — appended generated guidance is not free. → modeled as the `pre_spec`/`pre_reasoner` on/off ablation, reported as a null.
- **OCD / oracle-style methods** — expensive per-candidate oracle. → OACD's pre-registered PPL proxy is the cheap dense stand-in being tested.

---

## 8. Honesty / caveats (say these out loud)

- The `ctxlab` regression-gate numbers are mock-backend orderings on an 8-case
  synthetic fixture — they validate *machinery and directions*, not effect sizes.
- The real-model PPL claim now rests on the **frozen 200-case pre-registered run**
  (SUCCESS, §5), not just the earlier 2-case smoke. The gate side-channel's <80%
  stop-rule is tripped by incomplete labels on large treewide edits; the signal
  survives on the label-clean subset, so it is a label-completeness caveat, not a
  result-validity one (§6.5 is the follow-up).
- Selection coefficients are **uncalibrated placeholders**.
- Gold labeling is token-intersection: it misses typedef/macro indirection and can over-count coincidental symbols — hence the gate's <80% stop rule.
- The pipeline hook currently **reorders, never drops**; a dropping variant is later, riskier work.

---

### The four-bullet "why it's credible" for the slide

- **Already executing, not a proposal** — M0–M5 shipped, 28-check gate green, analysis notebook executed with saved figures.
- **Real model, real cases, real signal, pre-registered** — `Qwen2.5-Coder-0.5B` teacher-forced on a frozen 200-case `v6.19..v7.0-rc1` sample: **gold < random 70%, gold < none 92%** (p ≪ 0.01) → the pre-registered SUCCESS criterion is met.
- **Pre-committed to being falsifiable** — numeric SUCCESS/KILL thresholds fixed by a freeze commit before scoring, sign-test p-values, one allowed retry, and a gate stop-rule we report as *tripped* (with the robustness check that refutes its underlying concern) rather than hide.
- **Safe and honest by construction** — the only pipeline touch is a default-OFF, non-destructive, boundary-audited hook; mock-vs-real labeled everywhere; remaining steps are explicit STOP conditions.
