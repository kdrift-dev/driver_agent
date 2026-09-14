#!/usr/bin/env python3
"""Offline composite-module control checks; synthetic logs/files, no builds."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import run_ntb_module_controls as controls

RECIPE = controls.ModuleRecipe(
    case_id="synthetic_composite", old="a" * 40, new="b" * 40,
    module_target="drivers/demo/demo.ko", source_files=("drivers/demo/core.c", "drivers/demo/helper.c"),
    local_headers=("drivers/demo/private.h",), kernel_headers=("include/linux/fixture.h",),
    metadata_files=("drivers/demo/Makefile", "drivers/demo/Kconfig"),
    required_config={"MODULES": "y", "WERROR": "y", "DEMO": "m"}, owner_config="DEMO", disabled_config=("MODVERSIONS",),
)
COMPILE_CORE = "gcc -Wall -Werror -c -o drivers/demo/core.o drivers/demo/core.c"
COMPILE_HELPER = "/fixture/bin/x86_64-linux-gnu-gcc-14 -Wall -Werror -c -o drivers/demo/helper.o drivers/demo/helper.c"
COMPOSITE = "ld -m elf_x86_64 -r -o drivers/demo/demo.o @drivers/demo/demo.mod"
MODPOST = "scripts/mod/modpost -M -E -o Module.symvers -T modules.order vmlinux.o"
FINAL = "ld -r -o drivers/demo/demo.ko drivers/demo/demo.o drivers/demo/demo.mod.o"
LOG = "\n".join((COMPILE_CORE, COMPILE_HELPER, COMPOSITE, MODPOST, FINAL)) + "\n"
EXPECTED_OUTPUTS = {
    "drivers/demo/core.o", "drivers/demo/helper.o", "drivers/demo/demo.o", "drivers/demo/demo.ko",
    "drivers/demo/demo.mod", "drivers/demo/demo.mod.c", "drivers/demo/demo.mod.o",
    "drivers/demo/.core.o.cmd", "drivers/demo/.helper.o.cmd", "drivers/demo/.demo.o.cmd",
    "drivers/demo/.demo.ko.cmd", "drivers/demo/.demo.mod.cmd", "drivers/demo/.demo.mod.c.cmd",
    "drivers/demo/.demo.mod.o.cmd", "Module.symvers", ".Module.symvers.cmd",
}


def rejects(call, exception=ValueError):
    try:
        call()
    except exception:
        return
    raise AssertionError("expected explicit refusal")


def put(root, name, data):
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)


def check_evidence():
    controls.validate_recipe(RECIPE)
    evidence = controls.evidence(LOG, RECIPE)
    assert evidence["all_constituents_compiled"] and evidence["strict_modpost"]
    assert len(evidence["compiler_commands"]["drivers/demo/core.c"]) == 1
    assert len(evidence["compiler_commands"]["drivers/demo/helper.c"]) == 1
    assert evidence["composite_link_commands"] and evidence["final_link_commands"]
    for missing in (COMPILE_CORE, COMPILE_HELPER):
        assert not controls.evidence(LOG.replace(missing, ""), RECIPE)["all_constituents_compiled"]
    for fake in ("make V=1 drivers/demo/demo.ko", "echo '" + COMPILE_CORE + "'", "printf '%s\\n' '" + COMPILE_HELPER + "'"):
        assert not controls.evidence(fake, RECIPE)["all_constituents_compiled"]
    wrong_output = LOG.replace("-o drivers/demo/helper.o", "-o drivers/demo/unowned.o")
    assert not controls.evidence(wrong_output, RECIPE)["all_constituents_compiled"]
    assert not controls.evidence("echo '" + FINAL + "'", RECIPE)["final_link_commands"]
    for invalid in (COMPOSITE.replace(" -r", ""),
                    "ld -r -o drivers/demo/demo.o drivers/demo/core.o"):
        assert not controls.evidence(invalid, RECIPE)["composite_link_commands"]
    for invalid in (FINAL.replace(" -r", ""), FINAL.replace(" drivers/demo/demo.mod.o", ""),
                    FINAL.replace(" drivers/demo/demo.o ", " ")):
        assert not controls.evidence(invalid, RECIPE)["final_link_commands"]
    for invalid in (MODPOST.replace(" -E", ""), MODPOST.replace(" -M", ""),
                    MODPOST.replace(" vmlinux.o", ""), MODPOST.replace("modules.order", "other.order"),
                    MODPOST.replace("Module.symvers", "other.symvers"),
                    *(MODPOST + " " + flag for flag in ("-w", "-n", "-N", "-e", "-i external.symvers", "-iexternal.symvers"))):
        assert not controls.evidence(invalid, RECIPE)["strict_modpost"], invalid
        assert not controls.evidence(MODPOST + "\n" + invalid, RECIPE)["strict_modpost"]
    rejects(lambda: controls.validate_recipe(replace(RECIPE, source_files=("drivers/other/unowned.c",))))
    rejects(lambda: controls.validate_recipe(replace(RECIPE, local_headers=("drivers/demo/core.c",))))
    print("PASS explicit constituent compile/link evidence, no echo/outer-make substitution, strict modpost inputs/options and source-scope recipe validation")


def check_outputs_and_restoration():
    assert set(controls.output_paths(RECIPE)) == EXPECTED_OUTPUTS
    with tempfile.TemporaryDirectory(prefix="module-controls-files-") as directory:
        root = Path(directory)
        tree = root / "tree"
        tree.mkdir()
        for name in EXPECTED_OUTPUTS:
            put(tree, name, ("stale " + name).encode())
        preserved = {"drivers/demo/core.c": b"new core\n", "drivers/demo/helper.c": b"new helper\n",
                     "drivers/demo/private.h": b"new header\n", "vmlinux.o": b"kernel object\n",
                     "include/linux/fixture.h": b"new public header\n", "drivers/demo/sibling.o": b"unrelated object\n"}
        for name, data in preserved.items():
            put(tree, name, data)
        removed = controls.remove_outputs(tree, RECIPE)
        assert set(removed) == EXPECTED_OUTPUTS
        assert all(not (tree / name).exists() for name in EXPECTED_OUTPUTS)
        assert all((tree / name).read_bytes() == data for name, data in preserved.items())
        assert controls.remove_outputs(tree, RECIPE) == {}
        external = root / "external"
        external.write_bytes(b"keep")
        link = tree / "drivers/demo/core.o"
        link.symlink_to(external)
        put(tree, "Module.symvers", b"must not be removed on symlink refusal")
        rejects(lambda: controls.remove_outputs(tree, RECIPE))
        assert external.read_bytes() == b"keep"
        assert (tree / "Module.symvers").read_bytes() == b"must not be removed on symlink refusal"
        link.unlink()
        (tree / "Module.symvers").unlink()
        old = {name: ("old " + name + "\n").encode() for name in RECIPE.owned}
        new = {name: preserved[name] for name in RECIPE.owned}
        try:
            with controls.transplanted_sources(tree, old, new):
                assert all((tree / name).read_bytes() == data for name, data in old.items())
                assert (tree / "include/linux/fixture.h").read_bytes() == preserved["include/linux/fixture.h"]
                raise RuntimeError("synthetic negative-phase interruption")
        except RuntimeError:
            pass
        assert all((tree / name).read_bytes() == data for name, data in new.items())
        original_write = Path.write_bytes
        def fail_second_old(file, data):
            if file == tree / "drivers/demo/helper.c" and data == old["drivers/demo/helper.c"]:
                raise OSError("synthetic partial transplant")
            return original_write(file, data)
        with patch.object(Path, "write_bytes", fail_second_old):
            def partial_transplant():
                with controls.transplanted_sources(tree, old, new):
                    raise AssertionError("partial transplant must not yield")
            rejects(partial_transplant, OSError)
        assert all((tree / name).read_bytes() == data for name, data in new.items())
        fresh = root / "fresh"
        controls.fresh_output(fresh)
        rejects(lambda: controls.fresh_output(fresh))
        broken = root / "broken-link"
        broken.symlink_to(root / "missing")
        rejects(lambda: controls.fresh_output(broken))
    print("PASS every constituent/aggregate object and command file invalidated, unrelated/kernel inputs retained, symlink/output reuse refused and complete exception restoration")


def check_classification():
    owned = "drivers/demo/helper.c:18:4: error: synthetic incompatible interface"
    unrelated = "scripts/mod/helper.c:18:4: error: synthetic tool failure"
    log = LOG + unrelated + "\n" + owned + "\n./drivers/demo/core.c:5: error: synthetic owned error\n"
    assert controls.owned_compiler_diagnostics(log, RECIPE) == [owned, "./drivers/demo/core.c:5: error: synthetic owned error"]
    assert not controls.owned_compiler_diagnostics(unrelated + "\ninclude/linux/other.h:1: error: unrelated\n", RECIPE)
    phases = {name: {"passed": True, "infrastructure_marker": False} for name in controls.PHASES}
    phases["old_on_new"] = {"passed": False, "infrastructure_marker": False, "command": {"returncode": 2},
                             "evidence": controls.evidence(LOG, RECIPE), "compiler_diagnostic": True,
                             "owned_compiler_diagnostics": controls.owned_compiler_diagnostics(owned, RECIPE),
                             "modpost_diagnostic": False}
    result = {"integrity_verified": True, "restored_module_matches_new": True, "phases": phases}
    assert controls.classify(result) == "observed_module_compile_break"
    for status in (None, -9, 0, True):
        altered = copy.deepcopy(result)
        altered["phases"]["old_on_new"]["command"]["returncode"] = status
        assert controls.classify(altered) == "unresolved_module_failure"
    altered = copy.deepcopy(result)
    altered["phases"]["old_on_new"]["owned_compiler_diagnostics"] = controls.owned_compiler_diagnostics(unrelated, RECIPE)
    assert controls.classify(altered) == "unresolved_module_failure"
    altered["phases"]["old_on_new"]["modpost_diagnostic"] = True
    assert controls.classify(altered) == "observed_module_link_break"
    altered["phases"]["old_on_new"]["evidence"]["strict_modpost"] = False
    assert controls.classify(altered) == "unresolved_module_failure"
    altered = copy.deepcopy(result)
    altered["phases"]["old_on_new"]["evidence"] = controls.evidence(LOG.replace(COMPILE_HELPER, ""), RECIPE)
    assert controls.classify(altered) == "unresolved_module_failure"
    for name in ("old_on_old", "new_on_new", "restored_new_on_new"):
        altered = copy.deepcopy(result)
        altered["phases"][name]["passed"] = False
        assert controls.classify(altered) == "control_failure"
    altered = copy.deepcopy(result)
    altered["restored_module_matches_new"] = False
    assert controls.classify(altered) == "control_failure"
    for reason in ("integrity", "exception", "infrastructure", "missing-phase"):
        altered = copy.deepcopy(result)
        if reason == "integrity":
            altered["integrity_verified"] = False
        elif reason == "exception":
            altered["exception"] = {"type": "OSError"}
        elif reason == "infrastructure":
            altered["phases"]["old_on_new"]["infrastructure_marker"] = True
        else:
            del altered["phases"]["old_on_old"]
        assert controls.classify(altered) == "infrastructure_failure"
    altered = copy.deepcopy(result)
    altered["phases"]["old_on_new"]["passed"] = True
    assert controls.classify(altered) == "old_owned_module_builds_on_new"
    assert controls.require_equal_configs({"old": {"config": {"sha256": controls.sha(b"CONFIG_A=y\n")}},
                                           "new": {"config": {"sha256": controls.sha(b"CONFIG_A=y\n")}}})
    rejects(lambda: controls.require_equal_configs({"old": {"config": {"sha256": controls.sha(b"CONFIG_A=y\n")}},
                                                    "new": {"config": {"sha256": controls.sha(b"CONFIG_A=m\n")}}}))
    binary = {"sha256": controls.sha(b"same module bytes"), "bytes": len(b"same module bytes")}
    restored = {"new_on_new": {"artifacts": {RECIPE.module_target: binary}},
                "restored_new_on_new": {"artifacts": {RECIPE.module_target: copy.deepcopy(binary)}}}
    assert controls.restored_module_matches(restored, RECIPE)
    restored["restored_new_on_new"]["artifacts"][RECIPE.module_target]["sha256"] = controls.sha(b"different module bytes")
    assert not controls.restored_module_matches(restored, RECIPE)
    restored["restored_new_on_new"]["artifacts"] = {}
    assert not controls.restored_module_matches(restored, RECIPE)
    print("PASS owned diagnostic attribution, failed/incomplete/unstable controls, strict link classification and equal-config/restored-binary gates")


class FakeCommands:
    """Simulate one build's filesystem effects without invoking any program."""
    def __init__(self, *, missing_member=False, drift=False):
        self.missing_member, self.drift, self.records = missing_member, drift, []

    def run(self, argv, cwd, label):
        tree = Path(cwd)
        if argv[0] == "make":
            assert "KERNELRELEASE=6.0.0-fixture" in argv
            assert all(not (tree / name).exists() for name in EXPECTED_OUTPUTS)
            for name in ("drivers/demo/core.o", "drivers/demo/helper.o", "drivers/demo/demo.o",
                         "drivers/demo/demo.ko", "drivers/demo/demo.mod.o"):
                put(tree, name, b"synthetic output; ELF decoder stubbed in this membership test")
            members = "drivers/demo/core.o\n" if self.missing_member else "drivers/demo/core.o drivers/demo/helper.o\n"
            put(tree, "drivers/demo/demo.mod", members.encode())
            if self.drift:
                put(tree, ".config", b"CONFIG_DEMO=n\n")
            text = LOG
        elif argv[:2] == ["readelf", "-S"]:
            text = "synthetic section inspection\n"
        elif argv[:3] == ["readelf", "-p", ".modinfo"]:
            text = "name=demo\nvermagic=6.0.0-fixture SMP\n"
        elif argv[:2] == ["nm", "-u"]:
            text = "                 U fixture_api\n"
        else:
            raise AssertionError(argv)
        record = {"argv": argv, "returncode": 0, "error": None, "timed_out": False, "fixture_text": text}
        self.records.append(record)
        return record

    @staticmethod
    def text(record):
        return record["fixture_text"]

    @staticmethod
    def ok(record):
        return record["returncode"] == 0 and not record["error"] and not record["timed_out"]


def check_phase_integrity():
    with tempfile.TemporaryDirectory(prefix="module-controls-phase-") as directory:
        root = Path(directory)
        for case, missing, drift, expected in (("valid", False, False, True), ("missing-member", True, False, False), ("config-drift", False, True, False)):
            tree = root / case
            tree.mkdir()
            fixture_files = {"drivers/demo/core.c": b"int core;\n", "drivers/demo/helper.c": b"int helper;\n",
                             "drivers/demo/private.h": b"int private;\n", ".config": b"CONFIG_DEMO=m\n",
                             "include/linux/fixture.h": b"int fixture_api(void);\n",
                             "include/generated/utsrelease.h": b'#define UTS_RELEASE "6.0.0-fixture"\n',
                             "include/config/kernel.release": b"6.0.0-fixture\n",
                             "vmlinux.symvers": b"0x00000000 fixture_api vmlinux EXPORT_SYMBOL\n"}
            for name, data in fixture_files.items():
                put(tree, name, data)
            for name in EXPECTED_OUTPUTS:
                put(tree, name, b"stale object/command must disappear")
            stable_names = [name for name in fixture_files if name not in RECIPE.owned]
            prep = {"stable_hashes": controls.hashes(tree, stable_names), "kernel_release": "6.0.0-fixture"}
            commands = FakeCommands(missing_member=missing, drift=drift)
            with patch.object(controls, "elf_object", return_value=True), redirect_stdout(io.StringIO()):
                phase = controls.phase(commands, tree, root / (case + "-result"), RECIPE, prep,
                                       "fixture", 2, controls.hashes(tree, RECIPE.owned))
            assert phase["passed"] is expected
            assert phase["source_verified"] is True
            assert phase["stable_kernel_inputs"] is (not drift)
            assert phase["infrastructure_marker"] is drift
            assert set(phase["removed_artifacts"]) == EXPECTED_OUTPUTS
            if missing:
                assert phase["module_members"] != phase["expected_module_members"]
                assert len(commands.records) == 1  # No successful module inspection path.
            if expected:
                assert phase["module_identity_verified"] and phase["unresolved_imports"] == []
    print("PASS simulated phase requires exact composite members, fresh outputs and stable configuration; release pin recorded (ELF decoding isolated)")


def main():
    for check in (check_evidence, check_outputs_and_restoration, check_classification, check_phase_integrity):
        check()
    print(json.dumps({"status": "PASS", "check_groups": 4, "real_build_commands": 0,
                      "real_provider_requests": 0, "fixture_scope": "synthetic composite module logs, files and phase effects"}))


if __name__ == "__main__":
    main()
