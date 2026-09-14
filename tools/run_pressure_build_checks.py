#!/usr/bin/env python3
"""Synthetic negative controls for the isolated file-build screening protocol."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_pressure_build_eligibility import (
    Commands, PHASES, classify, compile_phase, config_values, fresh_output,
    run_case, sha, validate_mapping,
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def rejects(call, message):
    try:
        call()
    except ValueError:
        return
    raise AssertionError(message)


def classification_controls():
    phase = {"passed": True, "config_unchanged": True, "infrastructure_marker": False,
             "compiler_diagnostic": False, "compiler_command_seen": True,
             "command": {"returncode": 0, "timed_out": False, "error": None}}
    config = {"ok": True, "required_active": True, "recipe_satisfied": True, "config_unchanged_after_prepare": True}
    baseline = {"validation_status": "ok", "configs": {"old": dict(config), "new": dict(config)},
                "phases": {key: copy.deepcopy(phase) for key in PHASES}}
    check(classify(baseline) == "old_source_compiles", "working P0 was mislabeled a break")
    broken = copy.deepcopy(baseline)
    broken["phases"]["old_on_new"].update({"passed": False, "compiler_diagnostic": True})
    broken["phases"]["old_on_new"]["command"]["returncode"] = 2
    check(classify(broken) == "observed_file_compile_break", "controlled compiler break missed")
    for control in ("old_on_old", "new_on_new", "restored_new_on_new"):
        row = copy.deepcopy(broken)
        row["phases"][control]["passed"] = False
        check(classify(row) == "control_failure", f"failed {control} accepted as break")
    row = copy.deepcopy(broken)
    row["phases"]["old_on_new"]["command"]["timed_out"] = True
    check(classify(row) == "infrastructure_failure", "P0 timeout accepted as compiler break")
    row = copy.deepcopy(broken)
    row["phases"]["old_on_new"]["infrastructure_marker"] = True
    check(classify(row) == "infrastructure_failure", "infrastructure error accepted as break")
    row = copy.deepcopy(broken)
    row["configs"]["new"]["required_active"] = False
    check(classify(row) == "config_unavailable", "disabled config accepted as selected")
    row = copy.deepcopy(broken)
    row["phases"]["restored_new_on_new"]["config_unchanged"] = False
    check(classify(row) == "configuration_changed", "changed control configuration accepted")
    row = copy.deepcopy(baseline)
    row["phases"]["old_on_new"]["passed"] = False
    check(classify(row) == "infrastructure_failure", "successful no-op P0 accepted")
    row = copy.deepcopy(broken)
    row["phases"]["old_on_new"]["compiler_command_seen"] = False
    check(classify(row) == "infrastructure_failure", "unrelated diagnostic accepted without source compile")
    check(config_values(b'CONFIG_A=m\n# CONFIG_B is not set\n')["B"] == "n", "disabled configuration parse")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT).decode().strip()


def fixture(root):
    repo = root / "kernel"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Synthetic fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "drivers/demo").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "scripts/config").write_text("#!/usr/bin/env python3\nimport pathlib, sys\np=pathlib.Path('.config')\ns=p.read_text()\nfor action,key in zip(sys.argv[1::2],sys.argv[2::2]):\n s+='CONFIG_'+key+'='+('y' if action=='--enable' else 'n')+'\\n'\np.write_text(s)\n")
    (repo / "scripts/config").chmod(0o755)
    # Apple clang can emit an ELF object without a Linux sysroot for this
    # freestanding fixture, so the same strict object checks run on macOS.
    compiler = "CC = cc --target=x86_64-unknown-linux-gnu\n" if sys.platform == "darwin" else ""
    (repo / "Makefile").write_text(compiler + ".PHONY: allmodconfig olddefconfig modules_prepare\nallmodconfig:\n\t@printf 'CONFIG_DEMO=m\\n' > .config\nolddefconfig modules_prepare:\n\t@true\ndrivers/demo/demo.o: drivers/demo/demo.c\n\t$(CC) -Werror -c -o $@ $<\n\t@printf 'cmd_demo := $(CC) -Werror -c -o $@ $<\\n' > drivers/demo/.demo.o.cmd\n")
    source = b'#include "api.h"\nint demo(void) { return old_api(); }\n'
    (repo / "drivers/demo/demo.c").write_bytes(source)
    (repo / "drivers/demo/api.h").write_text("static inline int old_api(void) { return 1; }\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old API")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "drivers/demo/demo.c").write_bytes(source.replace(b"old_api", b"new_api"))
    (repo / "drivers/demo/api.h").write_text("static inline int new_api(void) { return 1; }\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "new API")
    new = git(repo, "rev-parse", "HEAD")
    pack = root / "pack"
    (pack / "old_module_sources/drivers/demo").mkdir(parents=True)
    (pack / "old_module_sources/drivers/demo/demo.c").write_bytes(source)
    case = {"case_id": "fixture", "meta": {"commit": new}, "old_sources_by_path": {"drivers/demo/demo.c": source.decode()}}
    mapping = {"case_id": "fixture", "old_commit": old, "new_commit": new,
               "source_file": "drivers/demo/demo.c", "object_target": "drivers/demo/demo.o",
               "owner_target": None, "required_config": ["DEMO"], "config_enable": [], "config_disable": []}
    return repo, pack, case, mapping


def filesystem_and_build_controls(root):
    repo, pack, case, mapping = fixture(root)
    validate_mapping({"cases": [mapping]}, [case])
    bad = {**mapping, "source_file": "../escape.c"}
    rejects(lambda: validate_mapping({"cases": [bad]}, [case]), "path traversal accepted")
    rejects(lambda: validate_mapping({"cases": [mapping, mapping]}, [case]), "duplicate mapping accepted")
    rejects(lambda: validate_mapping({"cases": []}, [case]), "incomplete mapping accepted")
    rejects(lambda: validate_mapping({"cases": [{**mapping, "object_target": "drivers/demo/"}]}, [case]), "non-object make target accepted")
    out = fresh_output(root / "run")
    rejects(lambda: fresh_output(out), "output reuse accepted")
    result = run_case(repo, pack, case, mapping, out, 1, 30)
    check(result["classification"] == "observed_file_compile_break", json.dumps({"classification": result["classification"], "exception": result.get("exception"), "phases": result["phases"]}, indent=2))
    check(all(result["phases"][name]["passed"] for name in ("old_on_old", "new_on_new", "restored_new_on_new")), "positive controls did not actually compile")
    check(result["pristine_new_source_sha256"] == result["restored_source_sha256"], "new source was not restored")
    check(git(repo, "status", "--porcelain") == "", "original checkout mutated")
    # A pre-existing ELF object cannot turn a no-op recipe into a passing compile.
    tree = Path(result["worktrees"]["new"])
    obj = tree / mapping["object_target"]
    check(obj.read_bytes().startswith(b"\x7fELF"), "fixture control object was not ELF")
    (tree / "Makefile").write_text("drivers/demo/demo.o:\n\t@true\n")
    commands = Commands(root / "noop_logs", 30)
    no_op = compile_phase(commands, tree, mapping, "noop", root / "noop_phase", result["configs"]["new"]["sha256"], 1)
    check(not no_op["passed"] and no_op["artifact_bytes"] == 0 and no_op["removed_prior_artifact_sha256"], "stale ELF survived a no-op compile")
    # Exact packed-parent byte binding must be checked before worktree creation.
    (pack / "old_module_sources/drivers/demo/demo.c").write_bytes(b"/* wrong source */\n")
    mismatch = run_case(repo, pack, case, mapping, fresh_output(root / "bad_source"), 1, 30)
    check(mismatch["classification"] == "input_invalid" and "worktrees" not in mismatch, "source mismatch compiled")
    # A genuinely deleted target is recorded without rebuilding an invented target.
    (pack / "old_module_sources/drivers/demo/demo.c").write_bytes((tree / "drivers/demo/demo.c").read_bytes())
    previous = mapping["new_commit"]
    (repo / "drivers/demo/demo.c").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-qm", "remove target")
    removed_commit = git(repo, "rev-parse", "HEAD")
    removed = {**mapping, "old_commit": previous, "new_commit": removed_commit, "skip_reason": "target deleted"}
    removed_case = {**case, "meta": {"commit": removed_commit}}
    result = run_case(repo, pack, removed_case, removed, fresh_output(root / "removed"), 1, 30)
    check(result["classification"] == "target_removed" and "worktrees" not in result, "deleted target was compiled")


def timeout_control(root):
    commands = Commands(root / "timeout_logs", 1)
    result = commands.run([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"], root, "timeout")
    check(result["timed_out"] and not commands.ok(result), "timeout process was not stopped")
    check("started" in commands.text(result), "full timeout log missing")


def main():
    classification_controls()
    print("PASS classification negative controls", flush=True)
    if not shutil.which("cc") or not shutil.which("make"):
        raise RuntimeError("synthetic compile checks require cc and make")
    with tempfile.TemporaryDirectory(prefix="pressure-build-checks-") as directory:
        root = Path(directory)
        filesystem_and_build_controls(root)
        print("PASS isolated Git/config/compile/restore, stale artifact, mapping, source hash, deleted target controls", flush=True)
        timeout_control(root)
        print("PASS bounded process timeout and full log", flush=True)
    print("3 pressure-build contract groups passed")


if __name__ == "__main__":
    main()
