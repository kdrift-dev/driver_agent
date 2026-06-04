# In-tree Testable Hard-break Dataset v0

This dataset contains 20 in-tree candidates for workflow evaluation. Selection criteria:

- old ref: `v5.0`
- new ref: `v7.0`
- old target module/source staged into the new kernel fails to compile (`P0` hard-break)
- each case has a KUnit, kselftest, or close software/QEMU test entry
- no Driver-Agent patch generation or LLM call was used during selection

Evidence comes from `runs/intree_testable_hardbreak_search_20260601_202418/p0_screen/`.

Important caveat: this is a candidate dataset. Some cases are broad Kbuild directories rather than strict single-module cases, and some selftests are partial runtime evidence rather than complete functional oracles.
