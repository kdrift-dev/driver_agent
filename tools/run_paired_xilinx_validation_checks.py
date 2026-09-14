#!/usr/bin/env python3
"""Offline patch and oracle negative controls; no kernel builds or generation."""
import difflib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

import validate_paired_xilinx_patch as validator


def reject(function, *args):
    try:
        function(*args)
    except (ValueError, RuntimeError):
        return
    raise AssertionError("unsafe input was accepted")


def patch_checks():
    source = validator.SOURCE
    old = b"one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\n"
    new = old.replace(b"two\n", b"replacement\nextra\n").replace(b"eight\n", b"EIGHT\n")
    patch = "".join(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True),
                                    fromfile="a/" + source, tofile="b/" + source, n=1)).encode()
    result, audit = validator.apply_exact_patch(old, patch)
    assert result == new and audit["hunks"] == 2 and audit["repairs"] == 0
    git_patch = (f"diff --git a/{source} b/{source}\nindex 1234567..abcdef0 100644\n".encode() + patch)
    assert validator.apply_exact_patch(old, git_patch)[0] == new
    headers = f"--- a/{source}\n+++ b/{source}\n".encode()
    assert validator.apply_exact_patch(b"last", headers + b"@@ -1 +1 @@\n-last\n\\ No newline at end of file\n+end\n\\ No newline at end of file\n")[0] == b"end"
    assert validator.apply_exact_patch(b"line\n", headers + b"@@ -0,0 +1 @@\n+first\n")[0] == b"first\nline\n"
    assert validator.apply_exact_patch(b"line\n", headers + b"@@ -1 +0,0 @@\n-line\n")[0] == b""
    bad = [b"", patch.replace(b"two", b"two "), patch.replace(b"@@ -1,3 +1,4", b"@@ -2,3 +2,4"),
           patch.replace(b"@@ -1,3 +1,4", b"@@ -1,4 +1,4"), patch.replace(b"+++ b/", b"+++ c/"),
           patch.replace(source.encode(), b"../escape.c"), patch.replace(source.encode(), b"other.c"),
           patch + patch, b"```diff\n" + patch + b"```\n", patch + b"trailing prose\n",
           patch.replace(b"\n", b"\r\n"), patch + b"\0", headers,
           headers + b"@@ -1 +1 @@\n one\n", headers + b"@@ -1 +1 @@\n-one\n+ONE\n\\ No newline at end of file\n\\ No newline at end of file\n",
           git_patch.replace(b"100644", b"100755"), git_patch.replace(b"index 1234567", b"new file mode 100644\nindex 1234567"),
           headers.replace(b"+++ b/" + source.encode(), b"+++ /dev/null") + b"@@ -1 +0,0 @@\n-one\n"]
    for item in bad:
        reject(validator.apply_exact_patch, old, item)
    reject(validator.apply_exact_patch, b"prefix\n" + old, patch)
    print(f"PASS exact patches: 5 valid fixtures and {len(bad) + 1} malformed/offset/out-of-scope rejections")


def inventory_checks(root):
    donor = root / "donor"
    donor.mkdir()
    (donor / ".git").write_text("immutable metadata\n")
    (donor / "source.c").write_text("source\n")
    (donor / "generated.o").write_bytes(b"object bytes\0")
    (donor / "source-link").symlink_to("source.c")
    before = validator.inventory(donor)
    assert ".git" not in before and len(before) == 3
    private = root / "private"
    shutil.copytree(donor, private, symlinks=True)
    assert validator.inventory(private) == before
    (private / "source.c").write_text("generated patch\n")
    assert validator.inventory(donor) == before
    assert not os.path.samestat((donor / "generated.o").stat(), (private / "generated.o").stat())
    (private / "escape").symlink_to(root / "outside")
    reject(validator.inventory, private)
    controls = root / "controls"
    controls.mkdir()
    (controls / "summary.json").write_text("{}\n")
    (controls / "artifact").write_bytes(b"frozen")
    index = {"file_count": 1, "files": {"artifact": {"bytes": 6, "sha256": validator.sha(b"frozen")}}}
    (root / "index.json").write_text(json.dumps(index))
    (root / "audit.json").write_text("{}\n")
    catalog = {key: {"path": str(path), "sha256": validator.file_hash(path)} for key, path in (
        ("summary", controls / "summary.json"), ("audit", root / "audit.json"), ("artifact_index", root / "index.json"))}
    assert validator.verify_index(controls, catalog)["verified_files"] == 1
    (controls / "artifact").write_bytes(b"broken")
    reject(validator.verify_index, controls, catalog)
    (controls / "artifact").write_bytes(b"frozen")
    (root / "audit.json").write_text("changed\n")
    reject(validator.verify_index, controls, catalog)
    print("PASS donor isolation, escaping symlinks, frozen index and audit integrity")


def build_gate_checks(root):
    tree = root / "build"
    (tree / validator.SOURCE).parent.mkdir(parents=True)
    (tree / validator.SOURCE).write_bytes(b"patched\n")
    for name in validator.STABLE:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    expected = validator.stable_hashes(tree)
    elf = bytearray(64)
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", elf, 16, 1, 62)
    for suffix in (".o", ".ko"):
        (tree / (validator.STEM + suffix)).write_bytes(elf)
    class FixtureCommands:
        text = staticmethod(lambda record: record["text"])
        ok = staticmethod(lambda record: not record["error"] and not record["timed_out"] and record["returncode"] == 0)
    compile_text = f"gcc -I./include -c -o {validator.STEM}.o {validator.SOURCE}\n"
    modpost_text = "./scripts/mod/modpost -M -E -o Module.symvers -T modules.order vmlinux.o\n"
    link_text = modpost_text + f"ld -r -o {validator.STEM}.ko {validator.STEM}.o\n"
    record = {"text": compile_text, "error": None, "timed_out": False, "returncode": 0}
    def outcome(stage, update=None):
        item = {**record, "text": compile_text if stage == "compile" else link_text, **(update or {})}
        return validator.stage_build_result(FixtureCommands(), item, tree, stage, expected, validator.sha(b"patched\n"))["status"]
    assert outcome("compile") == "passed" and outcome("link") == "passed"
    assert outcome("compile", {"text": compile_text + f"{validator.SOURCE}:2:3: error: fixture type mismatch\n", "returncode": 2}) == "failed"
    assert outcome("link", {"text": link_text + 'ERROR: modpost: "fixture" [target.ko] undefined!\n', "returncode": 2}) == "failed"
    linker_text = f"ld -r -o {validator.STEM}.ko {validator.STEM}.o\nld: input.o: undefined reference to `fixture'\n"
    assert outcome("link", {"text": linker_text, "returncode": 2}) == "failed"
    for stage in ("compile", "link"):
        assert outcome(stage, {"text": ""}) == "infrastructure_failure"
        assert outcome(stage, {"returncode": 2}) == "infrastructure_failure"
        for update in ({"timed_out": True}, {"error": "fixture"}, {"text": "No space left on device\n"},
                       {"text": "WARNING: vmlinux.o is missing\n"}):
            assert outcome(stage, update) == "infrastructure_failure"
    for flag in ("-w", "-n", "-N"):
        assert outcome("link", {"text": link_text.replace(" -M", f" {flag} -M")}) == "infrastructure_failure"
    assert outcome("link", {"text": link_text.replace(" vmlinux.o", "")}) == "infrastructure_failure"
    assert outcome("link", {"text": modpost_text}) == "infrastructure_failure"
    assert outcome("compile", {"text": f"{validator.SOURCE}:2:3: error: fixture\n", "returncode": 2}) == "infrastructure_failure"
    assert outcome("link", {"text": 'ERROR: modpost: "fixture" undefined!\n', "returncode": 2}) == "infrastructure_failure"
    (tree / ".config").write_bytes(b"changed")
    assert outcome("compile") == "infrastructure_failure" and outcome("link") == "infrastructure_failure"
    (tree / ".config").write_bytes(b".config")
    (tree / validator.SOURCE).write_bytes(b"repaired behind evaluator\n")
    assert outcome("compile") == "infrastructure_failure" and outcome("link") == "infrastructure_failure"
    print("PASS separate compile/link evidence, strict modpost, config/source drift, timeout and infrastructure gates")


def kernel_release_checks(root):
    tree = root / "kernel_release"
    header = tree / "include/generated/utsrelease.h"
    release = tree / "include/config/kernel.release"
    header.parent.mkdir(parents=True)
    release.parent.mkdir(parents=True)
    frozen = "6.19.0-rc5+"
    header.write_text(f'#define UTS_RELEASE "{frozen}"\n')
    release.write_text(frozen + "\n")
    assert validator.frozen_kernel_release(tree) == frozen
    for goal in (validator.STEM + ".o", validator.STEM + ".ko"):
        command = validator.generated_make(8, goal, validator.frozen_kernel_release(tree))
        assert command == validator.make(8, "KERNELRELEASE=" + frozen, goal)
        assert command.count("KERNELRELEASE=" + frozen) == 1
        assert command[-1] == goal and "KBUILD_MODPOST_WARN=" in command
    for malformed in ("6.19.0-rc5\n", frozen, frozen + "\nextra\n"):
        release.write_text(malformed)
        reject(validator.frozen_kernel_release, tree)
    release.write_text(frozen + "\n")
    header.write_text(f'#define UTS_RELEASE "{frozen}"\n/* extra metadata */\n')
    reject(validator.frozen_kernel_release, tree)
    header.write_text(f'#define UTS_RELEASE "{frozen}"\n')
    release.unlink()
    release.symlink_to(header)
    reject(validator.frozen_kernel_release, tree)
    for malformed in (None, "", frozen + "\n", "$(shell false)", "a" * 65):
        reject(validator.generated_make, 8, validator.STEM + ".o", malformed)
    reject(validator.generated_make, 8, "vmlinux", frozen)
    print("PASS frozen kernel release agreement, mismatch refusal and exact compile/link pins")


def tracked_checks(root):
    tree = root / "git_fixture"
    tree.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True).stdout
    git("init", "-q")
    (tree / "source.c").write_bytes(b"tracked source\n")
    git("add", "source.c")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    previous = validator.NEW
    validator.NEW = git("rev-parse", "HEAD").decode().strip()
    try:
        commands = validator.Commands(root / "git_logs", 30)
        assert validator.exact_tracked_sources(commands, tree) == {"source.c": validator.sha(b"tracked source\n")}
        git("update-index", "--assume-unchanged", "source.c")
        (tree / "source.c").write_bytes(b"modified source\n")
        assert not git("status", "--porcelain", "--untracked-files=no").strip()
        reject(validator.exact_tracked_sources, commands, tree)
    finally:
        validator.NEW = previous
    print("PASS direct Git blob validation detects edits hidden from git status")


def main():
    patch_checks()
    with tempfile.TemporaryDirectory(prefix="paired-xilinx-checks-") as directory:
        root = Path(directory)
        inventory_checks(root)
        build_gate_checks(root)
        kernel_release_checks(root)
        tracked_checks(root)
    print("PASS 5 offline groups; zero builds and zero model calls")


if __name__ == "__main__":
    main()
