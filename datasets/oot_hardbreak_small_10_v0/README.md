# OOT Hard-Break Small 10 v0

Purpose: small out-of-tree driver candidates for Driver-Agent OOT workflow iteration.

Selection rules:
- Target kernel is fixed to `v7.0`.
- Old kernel baseline is `v6.1` for all rows in this dataset.
- The OOT source builds against `v6.1` and fails against `v7.0` in the P0 screen.
- Source input is small: selected rows are <= 3 source files and <= 260 nonblank source LOC.
- Most failures are explicit kernel API evolution signals such as `class_create` signature changes or timer helper removal.

Caveat: several rows are tutorial-style drivers. They are useful for fast OOT workflow debugging, not final paper-scale evidence by themselves. Use `v4l2loopback` or another real maintained OOT driver as a separate larger case study.
