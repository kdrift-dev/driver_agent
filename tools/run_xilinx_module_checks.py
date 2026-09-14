#!/usr/bin/env python3
"""Negative controls for the strict module oracle's evidence gates."""
import copy
from run_xilinx_module_controls import PHASES, classify, inspect_modpost


def main():
    valid = "  scripts/mod/modpost -M -m -b -E -o Module.symvers -T modules.order vmlinux.o\n"
    assert inspect_modpost(valid)["strict"]
    for bad in ("", valid.replace(" vmlinux.o", ""),
                *(valid.replace(" -M", " " + flag + " -M") for flag in ("-w", "-n", "-N")),
                "make -f scripts/Makefile.modpost\n", valid + valid.replace(" -M", " -w -M")):
        assert not inspect_modpost(bad)["strict"], bad
    print("PASS modpost requires matching kernel object and rejects relaxed invocations")
    phase = {"passed": True, "stable_inputs": True, "infrastructure_marker": False,
             "compiler_seen": True, "compiler_diagnostic": False,
             "command": {"returncode": 0, "timed_out": False, "error": None}}
    baseline = {"phases": {name: copy.deepcopy(phase) for name in PHASES}}
    assert classify(baseline) == "old_source_module_builds"
    broken = copy.deepcopy(baseline)
    broken["phases"]["old_on_new"].update(passed=False, compiler_diagnostic=True)
    broken["phases"]["old_on_new"]["command"]["returncode"] = 2
    assert classify(broken) == "observed_module_compile_break"
    for control in ("old_on_old", "new_on_new", "restored_new_on_new"):
        row = copy.deepcopy(broken)
        row["phases"][control]["passed"] = False
        assert classify(row) == "control_failure", control
    for key, value in (("stable_inputs", False), ("infrastructure_marker", True)):
        row = copy.deepcopy(broken)
        row["phases"]["old_on_new"][key] = value
        assert classify(row) == "infrastructure_failure", key
    row = copy.deepcopy(broken)
    row["phases"]["old_on_new"]["command"]["timed_out"] = True
    assert classify(row) == "infrastructure_failure"
    row = copy.deepcopy(broken)
    row["phases"]["old_on_new"]["compiler_seen"] = False
    assert classify(row) == "unresolved_module_failure"
    row = copy.deepcopy(baseline)
    row["phases"]["old_on_new"]["passed"] = False
    assert classify(row) == "unresolved_module_failure"
    assert classify({}) == "infrastructure_failure"
    print("PASS failed controls, changed kernel/config, timeouts and missing evidence cannot establish a break")


if __name__ == "__main__":
    main()
