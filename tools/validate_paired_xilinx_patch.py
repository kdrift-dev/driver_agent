#!/usr/bin/env python3
"""Evaluate one frozen generated Xilinx diff in a private copy of NEW controls.

No generation, patch repair, source hints, reference comparison, or module loading.
The donor and historical control artifacts are read-only throughout this program.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity
from run_pressure_build_eligibility import (
    Commands, COMPILER_ERROR, INFRA, compiler_command_seen, dump, elf_object,
    fresh_output, safe_path, sha,
)
from run_xilinx_module_controls import (
    CASE, NEW, OLD, SOURCE, STEM, UNSAFE_ENV, make, require, snapshot,
    inspect_modpost,
)

PROTOCOL = "paired_xilinx_generated_patch_validation_v1"
ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*\n?$")
STABLE = (".config", "vmlinux", "vmlinux.o", "vmlinux.symvers",
          ".vmlinux.o.cmd", ".vmlinux.symvers.cmd", "include/generated/utsrelease.h")
KERNEL_RELEASE = re.compile(r"[0-9][0-9A-Za-z._+-]{0,63}\Z")


class PatchRejected(ValueError):
    pass


def apply_exact_patch(old, patch):
    """Apply an ordinary one-file unified diff with exact positions and bytes.

    Unlike patch/git-apply there is no context search, offset, fuzz, whitespace
    adjustment, path rewriting, or fallback. No-newline markers are supported.
    """
    if not isinstance(old, bytes) or not isinstance(patch, bytes):
        raise TypeError("old source and patch must be bytes")
    if not patch or b"\0" in patch or b"\r" in patch:
        raise PatchRejected("empty, binary, or CRLF patch is outside this protocol")
    lines = patch.splitlines(keepends=True)
    i = 0
    source = SOURCE.encode()
    if lines[0].startswith(b"diff --git "):
        if lines[0].rstrip(b"\n") != b"diff --git a/" + source + b" b/" + source:
            raise PatchRejected("diff must modify only the exact Xilinx target")
        i += 1
        if i < len(lines) and lines[i].startswith(b"index "):
            if not re.fullmatch(rb"index [0-9a-f]{7,40}\.\.[0-9a-f]{7,40}(?: 100644)?\n?", lines[i]):
                raise PatchRejected("unsupported index or mode header")
            i += 1
    for prefix, allowed in ((b"--- ", (source, b"a/" + source)),
                            (b"+++ ", (source, b"b/" + source))):
        if i >= len(lines) or not lines[i].startswith(prefix):
            raise PatchRejected("missing ordinary unified-diff file headers")
        path = lines[i][len(prefix):].rstrip(b"\n").split(b"\t", 1)[0]
        if path not in allowed:
            raise PatchRejected("rename, creation, deletion, or other target rejected")
        i += 1
    original = old.splitlines(keepends=True)
    output = []
    cursor = 0
    hunks = 0
    while i < len(lines):
        match = HUNK.fullmatch(lines[i])
        if not match:
            raise PatchRejected("expected hunk header; extra files or prose rejected")
        old_start, old_count, new_start, new_count = (
            int(match[1]), int(match[2] or b"1"), int(match[3]), int(match[4] or b"1"))
        old_index = old_start - 1 if old_count else old_start
        new_index = new_start - 1 if new_count else new_start
        if old_index < cursor or old_index > len(original):
            raise PatchRejected("hunk position overlaps or is outside the exact old source")
        output.extend(original[cursor:old_index])
        if new_index != len(output):
            raise PatchRejected("new hunk coordinates do not match preceding edits")
        i += 1
        body = []
        while i < len(lines) and not lines[i].startswith(b"@@ "):
            line = lines[i]
            if line == b"\\ No newline at end of file\n" or line == b"\\ No newline at end of file":
                if not body or not body[-1][1].endswith(b"\n"):
                    raise PatchRejected("misplaced no-newline marker")
                body[-1] = (body[-1][0], body[-1][1][:-1])
            elif line[:1] in (b" ", b"-", b"+"):
                body.append((line[:1], line[1:]))
            else:
                raise PatchRejected("unsupported patch content or additional file")
            i += 1
        before = [text for kind, text in body if kind != b"+"]
        after = [text for kind, text in body if kind != b"-"]
        if len(before) != old_count or len(after) != new_count:
            raise PatchRejected("hunk line counts do not match its header")
        if original[old_index:old_index + old_count] != before:
            raise PatchRejected("hunk bytes do not match the exact old source at declared position")
        output.extend(after)
        cursor = old_index + old_count
        hunks += 1
    output.extend(original[cursor:])
    result = b"".join(output)
    if not hunks or result == old:
        raise PatchRejected("no source change")
    return result, {"status": "passed", "hunks": hunks, "offsets": 0,
                    "fuzz": 0, "repairs": 0, "old_sha256": sha(old),
                    "patch_sha256": sha(patch), "patched_sha256": sha(result)}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(tree):
    """Fingerprint every donor file and mode, excluding only root Git metadata."""
    result = {}
    for directory, names, files in os.walk(tree, followlinks=False):
        base = Path(directory)
        if base == tree:
            names[:] = [name for name in names if name != ".git"]
            files = [name for name in files if name != ".git"]
        for name in sorted(names + files):
            path = base / name
            relative = path.relative_to(tree).as_posix()
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                target = os.readlink(path)
                if not path.resolve().is_relative_to(tree.resolve()):
                    raise ValueError(f"donor symlink escapes its tree: {relative}")
                result[relative] = {"kind": "symlink", "mode": mode, "target": target}
            elif path.is_file():
                result[relative] = {"kind": "file", "mode": mode,
                                    "bytes": info.st_size, "sha256": file_hash(path)}
            elif not path.is_dir():
                raise ValueError(f"unsupported donor artifact: {relative}")
    return result


def inventory_digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def verify_index(controls, catalog):
    anchors = {}
    for key in ("summary", "audit", "artifact_index"):
        item = catalog[key]
        path = controls / "summary.json" if key == "summary" else controls.parent / Path(item["path"]).name
        if path.is_symlink() or file_hash(path) != item["sha256"]:
            raise ValueError(f"frozen control {key} hash mismatch")
        anchors[key] = {"path": str(path), "sha256": item["sha256"]}
    index = json.loads(Path(anchors["artifact_index"]["path"]).read_bytes())
    if index["file_count"] != len(index["files"]):
        raise ValueError("control artifact index count mismatch")
    for name, expected in index["files"].items():
        path = controls / safe_path(name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["bytes"] or file_hash(path) != expected["sha256"]:
            raise ValueError(f"frozen control artifact changed: {name}")
    return {"anchors": anchors, "verified_files": len(index["files"])}


def exact_tracked_sources(commands, donor):
    if commands.git(donor, "rev-parse", "HEAD").decode().strip() != NEW:
        raise ValueError("donor is not the frozen NEW revision")
    if commands.git(donor, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("donor has tracked source modifications")
    # Hash each tracked file directly; status alone may trust cached stat data.
    records = commands.git(donor, "ls-tree", "-rz", "--full-tree", NEW).split(b"\0")
    hashes = {}
    for record in filter(None, records):
        header, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = header.split()
        name = raw_name.decode()
        path = donor / name
        if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise ValueError(f"unsupported tracked object: {name}")
        if mode == b"120000" and not path.is_symlink():
            raise ValueError(f"tracked symlink type changed: {name}")
        if mode != b"120000" and (not path.is_file() or path.is_symlink()):
            raise ValueError(f"tracked source type changed: {name}")
        data = os.readlink(path).encode() if mode == b"120000" else path.read_bytes()
        calculated = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if calculated != object_id.decode():
            raise ValueError(f"tracked source bytes differ from Git: {name}")
        hashes[name] = sha(data)
    return hashes


def verify_build_tools(commands, donor, controls):
    summary = json.loads((controls / "summary.json").read_bytes())
    versions = {}
    for program in ("gcc", "g++", "make", "ld", "readelf", "nm"):
        matches = [record for record in summary["commands"] if record["argv"] == [program, "--version"]]
        if len(matches) != 1:
            raise ValueError(f"missing unique frozen tool version: {program}")
        frozen_log = controls / "logs" / Path(matches[0]["log"]).name
        record = require(commands, [program, "--version"], donor, "version_" + program.replace("+", "p"))
        if commands.content(record) != frozen_log.read_bytes():
            raise ValueError(f"tool version differs from frozen controls: {program}")
        versions[program] = {"version_sha256": sha(commands.content(record)), "command": record}
    return versions


def stable_hashes(tree):
    return {name: file_hash(tree / name) for name in STABLE}


def frozen_kernel_release(tree):
    """Read matching release files after the caller verifies frozen UTS bytes."""
    paths = [tree / name for name in ("include/generated/utsrelease.h", "include/config/kernel.release")]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("frozen kernel release inputs must be ordinary files")
    header, release = (path.read_bytes() for path in paths)
    match = re.fullmatch(rb'#define UTS_RELEASE "([0-9][0-9A-Za-z._+-]{0,63})"\n', header)
    if not match or release != match[1] + b"\n":
        raise ValueError("frozen utsrelease.h and kernel.release disagree or are malformed")
    return match[1].decode("ascii")


def generated_make(jobs, goal, release):
    """Preserve the donor release when building a private copy without Git."""
    if not isinstance(release, str) or not KERNEL_RELEASE.fullmatch(release):
        raise ValueError("invalid frozen KERNELRELEASE")
    if goal not in (STEM + ".o", STEM + ".ko"):
        raise ValueError("generated build goal must be the frozen module target")
    return make(jobs, "KERNELRELEASE=" + release, goal)


def stage_build_result(commands, record, tree, stage, expected, patched_hash):
    text = commands.text(record)
    artifact = tree / (STEM + (".o" if stage == "compile" else ".ko"))
    modpost = inspect_modpost(text)
    value = {"command": record, "stable_inputs": stable_hashes(tree) == expected,
             "source_verified": file_hash(tree / SOURCE) == patched_hash,
             "compiler_seen": compiler_command_seen(text, SOURCE), "modpost": modpost,
             "compiler_diagnostic": bool(COMPILER_ERROR.search(text)),
             "infrastructure_marker": bool(INFRA.search(text)),
             "missing_symbol_warning": bool(re.search(r"WARNING: (?:vmlinux.o|.*Module.symvers) is missing", text)),
             "artifact_is_elf": artifact.is_file() and not artifact.is_symlink() and elf_object(artifact.read_bytes())}
    value["linker_seen"] = any(re.search(r"(?:^|[\s/])(?:[\w.+-]*-)?ld(?:\.bfd|\.lld)?\s", line)
                                  and re.search(r"(?:^|\s)-o\s+" + re.escape(STEM + ".ko") + r"(?:\s|$)", line)
                                  for line in text.splitlines())
    value["modpost_diagnostic"] = bool(re.search(r"^ERROR:\s*modpost:", text, re.M))
    value["linker_diagnostic"] = bool(re.search(
        r"^(?:[^\n\s]*/)?(?:[\w.+-]*-)?ld(?:\.bfd|\.lld)?:.*(?:undefined reference|relocation.*(?:truncated|against)|error:)", text, re.M))
    infrastructure = (record.get("error") or record.get("timed_out") or value["infrastructure_marker"]
                      or not value["stable_inputs"] or not value["source_verified"] or value["missing_symbol_warning"])
    evidence = value["compiler_seen"] if stage == "compile" else modpost["strict"] and value["linker_seen"]
    passed = commands.ok(record) and evidence and value["artifact_is_elf"] and not infrastructure
    observed_failure = record.get("returncode") not in (None, 0) and (
        value["compiler_seen"] and value["compiler_diagnostic"] if stage == "compile" else
        (modpost["strict"] and value["modpost_diagnostic"])
        or (value["linker_seen"] and value["linker_diagnostic"]))
    value["observed_failure"] = bool(observed_failure)
    value["status"] = "passed" if passed else "failed" if observed_failure and not infrastructure else "infrastructure_failure"
    return value


def remove_module_outputs(tree):
    paths = [STEM + suffix for suffix in (".o", ".ko", ".mod", ".mod.c", ".mod.o")]
    paths += [str(Path(path).with_name("." + Path(path).name + ".cmd")) for path in list(paths)]
    paths += ["Module.symvers", ".Module.symvers.cmd"]
    removed = {}
    for name in paths:
        path = tree / name
        if path.is_symlink():
            raise ValueError(f"symlink build output: {name}")
        if path.exists():
            removed[name] = file_hash(path)
            path.unlink()
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--controls-dir", type=Path, required=True)
    parser.add_argument("--control-catalog", type=Path, default=ROOT / "datasets/xilinx_module_controls_v1.json")
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout-sec", type=int, default=600)
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout_sec < 1:
        parser.error("jobs and timeout must be positive")
    if any(os.environ.get(key) for key in UNSAFE_ENV):
        parser.error("unset inherited build overrides: " + ", ".join(key for key in UNSAFE_ENV if os.environ.get(key)))
    for key in ("repo", "controls_dir", "control_catalog", "patch", "out_dir"):
        setattr(args, key, getattr(args, key).absolute())
    donor = args.controls_dir / "worktrees/new"
    if args.out_dir.resolve().is_relative_to(args.controls_dir.resolve()):
        parser.error("output must be outside the preserved controls directory")
    fresh_output(args.out_dir)
    commands = Commands(args.out_dir / "logs", args.timeout_sec)
    started = {"protocol": PROTOCOL, "case_id": CASE, "old_commit": OLD, "new_commit": NEW,
               "source": SOURCE, "target": STEM + ".ko", "evaluation_only": True,
               "generation_calls": 0, "runtime_tests": 0, "repairs": 0,
               "code": code_identity(), "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}}
    dump(args.out_dir / "run_metadata.started.json", started)
    row = {"apply": {"status": "not_run"}, "compile": {"status": "not_run"}, "link": {"status": "not_run"}}
    donor_before = None
    patch = catalog_bytes = None
    try:
        if args.patch.is_symlink() or args.control_catalog.is_symlink():
            raise ValueError("patch and catalog must be ordinary files")
        patch = args.patch.read_bytes()
        catalog_bytes = args.control_catalog.read_bytes()
        catalog = json.loads(catalog_bytes)
        if (catalog["case_id"], catalog["old_commit"], catalog["new_commit"], catalog["classification"]) != (
                CASE, OLD, NEW, "observed_module_compile_break") or catalog["integrity_verified"] is not True:
            raise ValueError("catalog does not describe the successful frozen controls")
        (args.out_dir / "inputs").mkdir()
        (args.out_dir / "inputs/generated.patch").write_bytes(patch)
        (args.out_dir / "inputs/control_catalog.json").write_bytes(catalog_bytes)
        row["kernel_store_head_before"] = commands.git(args.repo, "rev-parse", "HEAD").decode().strip()
        if commands.git(args.repo, "rev-parse", NEW + "^1").decode().strip() != OLD:
            raise ValueError("incorrect kernel parent")
        old = commands.git(args.repo, "show", OLD + ":" + SOURCE)
        if sha(old) != catalog["source_hashes"]["old"]:
            raise ValueError("old source does not match the frozen control")
        (args.out_dir / "inputs/old.c").write_bytes(old)
        try:
            patched, row["apply"] = apply_exact_patch(old, patch)
        except PatchRejected as exc:
            row["apply"] = {"status": "rejected", "reason": str(exc), "patch_sha256": sha(patch), "repairs": 0}
        else:
            (args.out_dir / "inputs/patched.c").write_bytes(patched)
            row["controls_before"] = verify_index(args.controls_dir, catalog)
            row["build_tools"] = verify_build_tools(commands, donor, args.controls_dir)
            tracked = exact_tracked_sources(commands, donor)
            dump(args.out_dir / "donor_tracked_sources.json", tracked)
            donor_before = inventory(donor)
            dump(args.out_dir / "donor_inventory.before.json", donor_before)
            total = sum(item.get("bytes", 0) for item in donor_before.values())
            row["copy"] = {"donor": str(donor), "files": len(donor_before), "bytes": total,
                           "source_files": len(tracked), "before_sha256": inventory_digest(donor_before),
                           "method": "cp -a --reflink=auto; no shared mutable inodes"}
            print(f"Verified donor: {len(tracked)} tracked sources, {len(donor_before)} artifacts, {total / 2**30:.2f} GiB; creating private copy", flush=True)
            tree = args.out_dir / "kernel"
            require(commands, ["cp", "-a", "--reflink=auto", str(donor), str(tree)], args.out_dir, "copy_kernel")
            if (tree / ".git").is_dir() or (tree / ".git").is_symlink():
                raise ValueError("expected a Git worktree metadata file in private copy")
            (tree / ".git").unlink()
            if inventory(tree) != donor_before:
                raise ValueError("private copy differs from exact donor inventory")
            for name, item in donor_before.items():
                if item["kind"] == "file" and os.path.samestat((tree / name).stat(), (donor / name).stat()):
                    raise ValueError(f"shared mutable donor inode: {name}")
            prep = json.loads((args.controls_dir / "kernels/new/preparation.json").read_bytes())
            expected = {".config": prep["config"]["sha256"], **{
                name: prep["artifacts"][name]["sha256"] for name in STABLE if name != ".config"}}
            if stable_hashes(tree) != expected or file_hash(tree / SOURCE) != catalog["source_hashes"]["new"]:
                raise ValueError("private copy differs from frozen NEW config/kernel/source hashes")
            row["stable_inputs_expected"] = expected
            release = frozen_kernel_release(tree)
            row["kernel_release_pin"] = {
                "value": release, "source": "frozen include/generated/utsrelease.h",
                "kernel_release_sha256": file_hash(tree / "include/config/kernel.release"),
                "reason": "preserve the frozen release after removing private-copy Git metadata"}
            row["removed_artifacts"] = remove_module_outputs(tree)
            (tree / SOURCE).write_bytes(patched)
            record = commands.run(generated_make(args.jobs, STEM + ".o", release), tree, "generated_compile")
            row["compile"] = stage_build_result(commands, record, tree, "compile", expected, sha(patched))
            if (tree / (STEM + ".o")).is_file():
                row["compile"]["artifact"] = snapshot(tree, STEM + ".o", args.out_dir / "compile")
            dump(args.out_dir / "progress.json", row)
            if row["compile"]["status"] == "passed":
                record = commands.run(generated_make(args.jobs, STEM + ".ko", release), tree, "generated_link")
                row["link"] = stage_build_result(commands, record, tree, "link", expected, sha(patched))
                if (tree / (STEM + ".ko")).is_file():
                    row["link"]["artifact"] = snapshot(tree, STEM + ".ko", args.out_dir / "link")
                    row["link"]["inspection"] = [require(commands, argv, tree, "module_inspect") for argv in (
                        ["readelf", "-S", str(tree / (STEM + ".ko"))],
                        ["readelf", "-p", ".modinfo", str(tree / (STEM + ".ko"))],
                        ["nm", "-u", str(tree / (STEM + ".ko"))])]
            row["stable_inputs_after"] = stable_hashes(tree)
            row["kernel_release_preserved"] = frozen_kernel_release(tree) == release
            if not row["kernel_release_preserved"]:
                raise ValueError("build changed the pinned frozen kernel release")
            # All tracked source bytes other than the generated target must remain exact.
            changed = [name for name, digest in tracked.items() if name != SOURCE and (
                sha(os.readlink(tree / name).encode()) if (tree / name).is_symlink() else file_hash(tree / name)) != digest]
            row["other_source_changes"] = changed
            if changed:
                raise ValueError("build changed other tracked source files")
    except Exception as exc:
        row["exception"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        integrity = False
        try:
            row["patch_input_unchanged"] = patch is not None and args.patch.read_bytes() == patch
            row["catalog_input_unchanged"] = catalog_bytes is not None and args.control_catalog.read_bytes() == catalog_bytes
            row["kernel_store_head_after"] = commands.git(args.repo, "rev-parse", "HEAD").decode().strip()
            if donor_before is not None:
                after = inventory(donor)
                dump(args.out_dir / "donor_inventory.after.json", after)
                row["donor_unchanged"] = after == donor_before
                row["controls_after"] = verify_index(args.controls_dir, json.loads(catalog_bytes))
            final_code = code_identity()
            integrity = (row["patch_input_unchanged"] and row["catalog_input_unchanged"]
                         and row.get("kernel_store_head_before") == row["kernel_store_head_after"]
                         and (donor_before is None or row["donor_unchanged"])
                         and started["code"]["source_digest"] == final_code["source_digest"]
                         and started["code"]["commit"] == final_code["commit"]
                         and not started["code"]["tracked_worktree_dirty"] and not final_code["tracked_worktree_dirty"])
        except Exception as exc:
            row["integrity_error"] = f"{type(exc).__name__}: {exc}"
            final_code = code_identity()
        row["integrity_verified"] = bool(integrity)
        row["classification"] = ("infrastructure_failure" if row.get("exception") or not integrity else
                                 "patch_rejected" if row["apply"]["status"] != "passed" else
                                 "module_build_passed" if row["link"]["status"] == "passed" else
                                 "compile_failed" if row["compile"]["status"] == "failed" else
                                 "link_failed" if row["link"]["status"] == "failed" else "infrastructure_failure")
        row["commands"] = commands.records
        dump(args.out_dir / "summary.json", row)
        dump(args.out_dir / "run_metadata.json", {**started, "code": final_code,
             "finished_at_utc": datetime.now(timezone.utc).isoformat(), "integrity_verified": bool(integrity)})
        files = {}
        for path in sorted(args.out_dir.rglob("*")):
            relative = path.relative_to(args.out_dir)
            if relative.parts[0] == "kernel" or not path.is_file():
                continue
            files[relative.as_posix()] = {"bytes": path.stat().st_size, "sha256": file_hash(path)}
        dump(args.out_dir / "artifact_index.json", {"files": files, "file_count": len(files),
             "excluded": ["kernel/", "artifact_index.json"]})
        print(row["classification"], flush=True)
    return 0 if row["classification"] == "module_build_passed" else 3 if row["classification"] == "infrastructure_failure" else 2


if __name__ == "__main__":
    raise SystemExit(main())
