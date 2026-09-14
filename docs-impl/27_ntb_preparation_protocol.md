# NTB production preparation, no inference

This preparation tests the existing production evidence path on a second
migration commit after fixing Kbuild-owned header scope. Freeze the executable
code before running it in a clean checkout on retro. No model requests, patch
generation, PPL scoring or runtime checks are authorized by this protocol.

The case identity is `7bd27439a578__drivers_ntb_msi_c`, now scoped to the
complete owning target `drivers/ntb/ntb.ko`:

- OLD: `b1f4077465b2110d8a486cd6f1aed0c2569c339a`.
- NEW: `7bd27439a578bb7724a9f09f240337ab95d68d2b`.
- `module_path=drivers/ntb`, `kbuild_target=ntb.ko`.
- Owned OLD translation units: `core.c` and `msi.c`, 18,147 bytes/725 lines
  combined. There are no local quoted headers or custom include directories
  in this target's old Makefile. Public `<linux/...>` includes remain kernel
  evidence at their respective revisions.

Run `tools/extract_kernel_evidence.py`, a bounded wrapper of the existing
production loader, symbol scanner, definition locator and kernel diff builder.
It never enters patch generation or validation: the ordinary CLI's `--dry-run`
alone does not guarantee that a no-op candidate cannot reach a build.
No LLM client or credentials are loaded. The wrapper freezes the executable
source hashes, this protocol and all arguments before extraction, requires
immutable commit IDs and a fresh output directory, and records an artifact index.
The invocation in the clean remote checkout is:

```sh
python3 tools/extract_kernel_evidence.py \
  --repo /home/arina/repos/linux \
  --old-ref b1f4077465b2110d8a486cd6f1aed0c2569c339a \
  --new-ref 7bd27439a578bb7724a9f09f240337ab95d68d2b \
  --module-path drivers/ntb --kbuild-target ntb.ko --jobs 8 \
  --out-dir runs/ntb_production_preparation_20260914/extraction_v1
```

The wrapper uses complete old module source, `kernel_only`, no commit
information and no semantic table. Its accessor restricts underlying Git
searches to the evidence adapter's permitted kernel roots before results are
read, excluding any nested `drivers` component. Source content reads are
guarded to OLD owned translation units and their reachable local headers,
old ownership Makefile/Kbuild, and permitted OLD/NEW kernel files. An operation
guard rejects networking and non-read-only-Git subprocesses. At completion,
check code/protocol/freeze hashes, kernel checkout HEAD and original source
bytes again; preserve any boundary refusal without weakening the adapter.
Scope resolution warns on unsupported custom include-search/forced-include
flags in the old Makefile, unresolved quoted includes or include macros. It
does not infer the full Kbuild/compiler include environment.

Preserve the production invocation, source inventory and all resulting artifacts.
The loader must read only complete OLD owned sources and resolved OLD local
headers. Definition/diff evidence may read only permitted kernel-side paths
at OLD/NEW. Do not consult target NEW driver code, reference diffs, historical
model outputs, sibling-driver examples or manually authored repair hints.

Feed only the existing allowlisted extraction documents and exact OLD source
bytes to `tools/prepare_paired_context.py`. Verify source/excerpt lines against
immutable Git blobs. Use the default full-source quarter-pool rule, full
messages, `message_chars`, model identity `anthropic/claude-sonnet-4.6` and
4096 output cap. This is preparation metadata, not a model invocation.

Record the complete candidate pool, source and requirement hashes, source
ownership, signal availability, counts, selected units and boundary refusals.
Do not pad the pool, add hand-derived obligations or weaken the adapter to
force admission. If extraction cannot represent this case or yields no useful
changed-interface signal, preserve that limitation as the result. Any later
extractor correction needs a fresh code freeze and fresh output directory.

Keep `generation_ready=false` and `actual_token_budget_comparison=false`.
The character counter has no verified provider-token binding. Neither context
pressure nor a completed extraction establishes repairability or a selection
advantage. Evaluation-only module controls have a separate protocol and output
directory; their target NEW source and compiler diagnoses are not model input.
No real NTB generation follows automatically from this preparation.
