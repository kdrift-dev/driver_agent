#!/usr/bin/env python3
"""Synthetic Git extraction, source-boundary and no-inference/build checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_kernel_evidence as extract


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def init(repo):
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Extraction fixture")
    git(repo, "config", "user.email", "extraction@example.invalid")


def rejects(fn):
    try:
        fn()
    except extract.BoundaryViolation:
        return
    raise AssertionError("boundary operation was admitted")


def checks():
    passed = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "kernel"
        init(repo)
        old_source = "#include <linux/demo.h>\nint demo(void)\n{\n    return api_a();\n}\n"
        files = {"drivers/demo/Makefile": "obj-y += demo.o\n", "drivers/demo/demo.c": old_source,
                 "drivers/demo/sibling/private.h": "int unrelated_driver_marker;\n",
                 "drivers/other/other.c": "int unrelated_driver_marker;\n",
                 "arch/example/drivers/private.c": "int unrelated_driver_marker;\n",
                 "include/linux/demo.h": "int api_a(void);\n"}
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "old synthetic kernel")
        old_ref = git(repo, "rev-parse", "HEAD")
        (repo / "drivers/demo/demo.c").write_text("int unrelated_driver_marker;\n")
        (repo / "include/linux/demo.h").write_text("long api_a(void);\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "new synthetic kernel")
        new_ref = git(repo, "rev-parse", "HEAD")

        accessor = extract.GuardedAccessor(repo=repo, old_ref=old_ref, new_ref=new_ref, module_path="drivers/demo")
        accessor.authorize_sources(["drivers/demo/demo.c"])
        assert accessor.read_file("old", "drivers/demo/demo.c") == old_source
        rejects(lambda: accessor.read_file("new", "drivers/demo/demo.c"))
        rejects(lambda: accessor.read_file("old", "drivers/demo/sibling/private.h"))
        rejects(lambda: accessor.read_file("old", "drivers/other/other.c"))
        rejects(lambda: accessor.read_file("new", "arch/example/drivers/private.c"))
        rejects(lambda: accessor.read_file("new", "include/../drivers/demo/demo.c"))
        rejects(lambda: accessor.refs_diff(["drivers/demo/demo.c"]))
        rejects(lambda: accessor.log_s("api_a"))
        rejects(lambda: accessor.list_module_files("old", "drivers/demo"))
        assert accessor.search("new", "unrelated_driver_marker", pathspecs=None) == []
        assert accessor.search("new", "unrelated_driver_marker", pathspecs=["arch/*"]) == []
        assert [hit.path for hit in accessor.search("new", "api_a", pathspecs=None)] == ["include/linux/demo.h"]
        non_driver_target = extract.GuardedAccessor(repo=repo, old_ref=old_ref, new_ref=new_ref, module_path="include/linux")
        rejects(lambda: non_driver_target.read_file("new", "include/linux/demo.h"))
        rejects(lambda: non_driver_target.refs_diff(["include/linux/demo.h"]))
        assert non_driver_target.search("new", "api_a", pathspecs=None) == []
        passed.append("forbidden contents blocked before Git; broad/nested searches constrained")

        guard = extract.ProcessGuard()
        guard("subprocess.Popen", ("git", ["git", "-C", str(repo), "show", old_ref + ":drivers/demo/demo.c"], None, None))
        assert guard.process_count == 1
        rejects(lambda: guard("subprocess.Popen", ("make", ["make", "modules"], None, None)))
        rejects(lambda: guard("subprocess.Popen", ("git", ["git", "-C", str(repo), "worktree", "add", "/tmp/no"], None, None)))
        rejects(lambda: guard("socket.connect", (None, ("example.invalid", 443))))
        passed.append("process/network guard rejects builds, worktrees and inference transport")

        # Exercise the actual CLI and freeze check in a fresh committed snapshot
        # of precisely its executable dependencies, without relaxing identity().
        code = root / "code"
        init(code)
        for name in extract.CODE_FILES:
            destination = code / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extract.ROOT / name, destination)
        git(code, "add", ".")
        git(code, "commit", "-qm", "synthetic extraction code freeze")
        output = root / "output"
        argv = [sys.executable, str(code / "tools/extract_kernel_evidence.py"), "--repo", str(repo),
                "--old-ref", old_ref, "--new-ref", new_ref, "--module-path", "drivers/demo",
                "--kbuild-target", "demo.ko", "--out-dir", str(output), "--jobs", "2"]
        env = {**os.environ, "DRIVER_AGENT_ROOT": str(code)}
        result = subprocess.run(argv, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads((output / "summary.json").read_text())
        assert summary["status"] == "prepared", summary
        assert summary["candidate_count"] > 0 and summary["obligation_signal_count"] > 0
        assert summary["stages"] == ["module_source_loader", "symbol_scanner", "definition_locator", "kernel_diff_builder"]
        definitions = json.loads((output / "extracted/kernel_definition_diffs.json").read_text())["items"]
        locations = {(row["symbol"], row["kind"]): row for row in json.loads(
            (output / "extracted/kernel_definition_locations.json").read_text())["items"]}
        for row in definitions:
            for side in ("old", "new"):
                selected = row[side + "_definition_location"]
                options = locations[(row["symbol"], row["kind"])][side + "_locations"]
                assert selected in options if options else selected is None
        assert summary["model_call_count"] == summary["build_call_count"] == 0
        assert summary["blocked_operations"] == summary["forbidden_modules_loaded"] == []
        assert all(summary[name] for name in ("code_unchanged", "kernel_head_unchanged", "freeze_unchanged", "protocol_unchanged", "old_source_inventory_unchanged"))
        assert set(summary["source_inventory"]) == {"drivers/demo/demo.c"}
        assert (output / "extracted/old_module_sources/drivers/demo/demo.c").read_text() == old_source
        assert not (output / "worktrees").exists() and not (output / "llm_calls").exists()
        access = json.loads((output / "access_audit.json").read_text())
        assert all(row["side"] == "old" and row["path"] in {"drivers/demo/Makefile", "drivers/demo/demo.c"}
                   for row in access if row["operation"] == "read" and row["path"].startswith("drivers/"))
        index = json.loads((output / "artifact_index.json").read_text())["files_sha256"]
        assert all(hashlib.sha256((output / name).read_bytes()).hexdigest() == digest for name, digest in index.items())
        passed.append("real frozen CLI stages1–4 produce verified full-source kernel pool without LLM/build")

        before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
        replay = subprocess.run(argv, env=env, capture_output=True, text=True)
        assert replay.returncode != 0 and "refusing to reuse" in replay.stderr
        assert before == {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
        passed.append("existing output refused without modifying any artifact")

        # Mutate a protocol, rather than an import, to reach the integrity check.
        (code / extract.PROTOCOL).write_text("changed protocol\n")
        dirty_argv = list(argv)
        dirty_argv[dirty_argv.index(str(output))] = str(root / "dirty_output")
        dirty = subprocess.run(dirty_argv, env=env, capture_output=True, text=True)
        assert dirty.returncode != 0 and "clean committed" in dirty.stderr
        assert not (root / "dirty_output").exists()
        passed.append("uncommitted protocol refused before creating extraction output")
    return passed


if __name__ == "__main__":
    names = checks()
    print(json.dumps({"status": "PASS", "check_count": len(names), "checks": names}, indent=2))
