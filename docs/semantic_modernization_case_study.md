# Deferred Case Study: Source-Compatible Semantic Modernization

This note records a paper case study and future-work item. It is not part of the default Driver-Agent workflow.

## Case Pattern

Release pair: `v6.19 -> v7.0-rc1`

Example modules:

- `drivers/soc/ux500`
- `drivers/pmdomain/st`
- `drivers/video/fbdev/mmp/panel`

Observed upstream driver-side pattern:

```c
kzalloc(sizeof(*x), GFP_KERNEL)
```

becomes:

```c
kzalloc_obj(*x)
```

## Why This Is Not a Required Repair by Default

This is a source-compatible semantic or idiomatic modernization:

- The old API still exists.
- The new API also exists.
- The old call pattern can still compile.
- A direct kernel-side definition-level break is not necessarily present.
- Upstream migration may reflect a newer allocation idiom or safety preference rather than a compatibility failure.

Therefore, omitting this edit should not be counted as an incorrect patch in the default required-repair setting.

## Why It Is Still Useful for the Paper

This case is useful to explain a boundary condition:

- Not every upstream driver patch corresponds to a required compatibility repair.
- Some patches reflect source-compatible semantic modernization.
- A tool that only claims to repair kernel-interface compatibility should not over-claim on these cases.

Suggested paper framing:

```text
We observed source-compatible semantic modernizations where upstream migrates
drivers to a newer helper even though the old API remains available. For example,
some drivers replace kzalloc(sizeof(*x), GFP_KERNEL) with kzalloc_obj(*x).
This is useful as a case study, but we do not treat it as a required repair unless
kernel-side evidence shows a concrete contract change.
```

## Future Optional Mode

A future modernization mode could support this class explicitly, but it should be separated from the main workflow.

Possible additional evidence sources:

- Kernel commit messages.
- Kernel-side Coccinelle scripts.
- Broader treewide evolution patterns.
- Maintainer-visible migration notes.

Policy for that future mode:

- It must be opt-in.
- It must report results separately from required compatibility repair.
- It must not use target driver new-version diff as generation input.
- It should mark edits as modernization, not correctness-required repair, unless stronger kernel-side contract evidence exists.
