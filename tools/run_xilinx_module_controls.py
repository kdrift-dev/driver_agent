#!/usr/bin/env python3
"""Evaluation-only strict Xilinx module controls; no generation or module loading."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity
from run_pressure_build_eligibility import (
    Commands, COMPILER_ERROR, DISABLE_CONFIG, INFRA, PHASES,
    compiler_command_seen, config_values, dump, elf_object, fresh_output, sha,
)

PROTOCOL = "xilinx_strict_module_controls_v1"
CASE = "b2c15db74a37__drivers_crypto_xilinx_xilinx_trng_c"
OLD = "9b95f3a4c9b8baf9fc4c2b8776f8285c05e594f5"
NEW = "b2c15db74a379a50d723002799c3db0c6781c75a"
SOURCE = "drivers/crypto/xilinx/xilinx-trng.c"
STEM = SOURCE[:-2]
REQUIRED = {"MODULES": "y", "COMPILE_TEST": "y",
            "CRYPTO": "y", "CRYPTO_HW": "y", "CRYPTO_DEV_XILINX_TRNG": "m",
            "CRYPTO_DRBG_MENU": "y", "CRYPTO_DRBG_CTR": "y",
            "CRYPTO_DF80090A": "y", "CRYPTO_RNG": "y", "HW_RANDOM": "y",
            "DEBUG_INFO_NONE": "y"}
DISABLED = set(DISABLE_CONFIG) | {"LOCALVERSION_AUTO", "TRIM_UNUSED_KSYMS", "MODVERSIONS",
    "MODULE_ALLOW_MISSING_NAMESPACE_IMPORTS", "SECTION_MISMATCH_WARN_ONLY"}
# Deliberately pin build flags; inherited flags may silently weaken the oracle.
UNSAFE_ENV = ("MAKEFLAGS", "MFLAGS", "KBUILD_MODPOST_WARN", "KBUILD_NSDEPS",
              "KBUILD_EXTRA_SYMBOLS", "KBUILD_EXTMOD", "KBUILD_OUTPUT", "KCFLAGS",
              "CFLAGS_MODULE", "KCPPFLAGS", "LLVM", "CROSS_COMPILE")


def snapshot(tree, relative, dest):
    path = tree / relative
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"missing ordinary artifact: {path}")
    raw = path.read_bytes()
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {"sha256": sha(raw), "bytes": len(raw), "snapshot": str(target)}


def require(commands, argv, tree, label):
    record = commands.run(argv, tree, label)
    if not commands.ok(record):
        raise RuntimeError(f"{label} failed: {record['log']}")
    return record


def make(jobs, *goals):
    return ["make", f"-j{jobs}", "ARCH=x86_64", "CC=gcc", "HOSTCC=gcc",
            "HOSTCXX=g++", "LD=ld", "V=1", "KBUILD_MODPOST_WARN=", *goals]


def prepare(commands, tree, dest, jobs):
    require(commands, make(jobs, "defconfig"), tree, "defconfig")
    argv = ["scripts/config", "--set-str", "LOCALVERSION", ""]
    for key, value in sorted(REQUIRED.items()):
        argv += ["--module" if value == "m" else "--enable", key]
    for key in sorted(DISABLED):
        argv += ["--disable", key]
    require(commands, argv, tree, "config_edit")
    require(commands, make(jobs, "olddefconfig"), tree, "olddefconfig")
    config = snapshot(tree, ".config", dest)
    values = config_values((tree / ".config").read_bytes())
    if any(values.get(k, "n") != v for k, v in REQUIRED.items()) or any(
        values.get(k, "n") != "n" for k in DISABLED
    ):
        raise RuntimeError("resolved Kconfig does not satisfy the frozen recipe")
    result = {"config": config, "required_values": {k: values.get(k) for k in REQUIRED},
              "disabled_values": {k: values.get(k, "n") for k in sorted(DISABLED)}}
    dump(dest / "preparation.started.json", result)
    print(f"Building pristine kernel: {tree.name}", flush=True)
    result["build_command"] = require(commands, make(jobs, "vmlinux", "modules_prepare"), tree, "vmlinux")
    if sha((tree / ".config").read_bytes()) != config["sha256"]:
        raise RuntimeError("configuration changed during kernel build")
    # In-tree modpost reads vmlinux.o directly at these revisions. Keep both
    # the linked image and its complete exported-symbol table for provenance.
    result["artifacts"] = {name: snapshot(tree, name, dest) for name in (
        "vmlinux", "vmlinux.o", "vmlinux.symvers",
        ".vmlinux.o.cmd", ".vmlinux.symvers.cmd", "include/generated/utsrelease.h")}
    if not elf_object((tree / "vmlinux.o").read_bytes()):
        raise RuntimeError("vmlinux.o is not an x86_64 ELF relocatable object")
    symvers = (tree / "vmlinux.symvers").read_text()
    if not re.search(r"\bcrypto_drbg_ctr_df\s+vmlinux\s", symvers):
        raise RuntimeError("matching kernel does not export crypto_drbg_ctr_df")
    result["exported_symbols"] = len(symvers.splitlines())
    dump(dest / "preparation.json", result)
    return result


def inspect_modpost(text):
    lines = [line.strip() for line in text.splitlines()
             if re.match(r"^(?:\./)?scripts/mod/modpost\s", line.strip())]
    return {"commands": lines, "seen": bool(lines), "strict": bool(lines) and all(
        re.search(r"(?:^|\s)vmlinux\.o(?:\s|$)", line)
        and not re.search(r"(?:^|\s)-(?:w|n|N)(?:\s|$)", line) for line in lines)}


def phase(commands, tree, name, dest, prep, jobs):
    dest.mkdir(parents=True, exist_ok=False)
    paths = [STEM + suffix for suffix in (".o", ".ko", ".mod", ".mod.c", ".mod.o")]
    paths += [str(Path(p).with_name("." + Path(p).name + ".cmd")) for p in list(paths)]
    paths += ["Module.symvers", ".Module.symvers.cmd"]
    removed = {}
    for relative in paths:
        path = tree / relative
        if path.is_symlink():
            raise RuntimeError(f"symlink artifact: {path}")
        if path.exists():
            removed[relative] = sha(path.read_bytes())
            path.unlink()
    before = {p: sha((tree / p).read_bytes()) for p in (".config", "vmlinux.o", "vmlinux.symvers")}
    expected = {".config": prep["config"]["sha256"], **{
        p: prep["artifacts"][p]["sha256"] for p in ("vmlinux.o", "vmlinux.symvers")}}
    record = commands.run(make(jobs, STEM + ".ko"), tree, name)
    log = commands.text(record)
    after = {p: sha((tree / p).read_bytes()) for p in before}
    artifacts = {p: snapshot(tree, p, dest) for p in paths if (tree / p).is_file()}
    ko = tree / (STEM + ".ko")
    modpost = inspect_modpost(log)
    value = {"phase": name, "command": record, "removed_artifacts": removed,
        "source_sha256": sha((tree / SOURCE).read_bytes()), "artifacts": artifacts,
        "stable_inputs_before": before, "stable_inputs_after": after,
        "stable_inputs": before == after == expected,
        "compiler_seen": compiler_command_seen(log, SOURCE), "modpost": modpost,
        "compiler_diagnostic": bool(COMPILER_ERROR.search(log)),
        "infrastructure_marker": bool(INFRA.search(log)),
        "missing_symbol_warning": bool(re.search(r"WARNING: (?:vmlinux.o|.*Module.symvers) is missing", log)),
        "artifact_is_elf": ko.is_file() and elf_object(ko.read_bytes())}
    value["passed"] = bool(commands.ok(record) and value["stable_inputs"]
        and value["compiler_seen"] and modpost["strict"] and value["artifact_is_elf"]
        and not value["infrastructure_marker"] and not value["missing_symbol_warning"])
    if ko.is_file():
        value["module_details"] = [require(commands, argv, tree, name + "_inspect") for argv in (
            ["readelf", "-S", str(ko)], ["readelf", "-p", ".modinfo", str(ko)],
            ["nm", "-u", str(ko)])]
    dump(dest / "phase.json", value)
    print(f"{name}: {'PASS' if value['passed'] else 'FAIL'}", flush=True)
    return value


def classify(row):
    phases = row.get("phases", {})
    if row.get("exception") or set(phases) != set(PHASES):
        return "infrastructure_failure"
    if any(p["command"].get("timed_out") or p["command"].get("error")
           or p["infrastructure_marker"] or not p["stable_inputs"] for p in phases.values()):
        return "infrastructure_failure"
    if not all(phases[n]["passed"] for n in ("old_on_old", "new_on_new", "restored_new_on_new")):
        return "control_failure"
    p0 = phases["old_on_new"]
    if p0["passed"]:
        return "old_source_module_builds"
    if p0["command"]["returncode"] != 0 and p0["compiler_seen"] and p0["compiler_diagnostic"]:
        return "observed_module_compile_break"
    return "unresolved_module_failure"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--old-source", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout-sec", type=int, default=3600)
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout_sec < 1:
        parser.error("jobs and timeout must be positive")
    if any(os.environ.get(key) for key in UNSAFE_ENV):
        parser.error("unset inherited build override variables: " + ", ".join(k for k in UNSAFE_ENV if os.environ.get(k)))
    args.repo, args.old_source, args.out_dir = (p.absolute() for p in (args.repo, args.old_source, args.out_dir))
    fresh_output(args.out_dir)
    commands = Commands(args.out_dir / "logs", args.timeout_sec)
    started = {"protocol": PROTOCOL, "evaluation_only": True, "case_id": CASE,
        "old_commit": OLD, "new_commit": NEW, "source": SOURCE, "target": STEM + ".ko",
        "code": code_identity(), "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "generation_calls": 0, "runtime_tests": 0}
    dump(args.out_dir / "run_metadata.started.json", started)
    row = {"preparations": {}, "phases": {}}
    trees = {}
    try:
        if commands.git(args.repo, "rev-parse", NEW + "^1").decode().strip() != OLD:
            raise ValueError("incorrect kernel parent")
        old = commands.git(args.repo, "show", OLD + ":" + SOURCE)
        new = commands.git(args.repo, "show", NEW + ":" + SOURCE)
        if args.old_source.is_symlink() or args.old_source.read_bytes() != old:
            raise ValueError("packed old source differs from exact parent Git blob")
        row["source_hashes"] = {"old": sha(old), "new": sha(new)}
        (args.out_dir / "inputs").mkdir()
        (args.out_dir / "inputs/old.c").write_bytes(old)
        (args.out_dir / "inputs/new.evaluation-only.c").write_bytes(new)
        row["kernel_store_head_before"] = commands.git(args.repo, "rev-parse", "HEAD").decode().strip()
        for program in ("gcc", "g++", "make", "ld", "readelf", "nm", "git", "python3"):
            require(commands, [program, "--version"], args.repo, "version_" + program.replace("+", "p"))
        for version, commit, control in (("old", OLD, "old_on_old"), ("new", NEW, "new_on_new")):
            tree = args.out_dir / "worktrees" / version
            tree.parent.mkdir(exist_ok=True)
            commands.git(args.repo, "worktree", "add", "--detach", str(tree), commit)
            trees[version] = tree
            prep = prepare(commands, tree, args.out_dir / "kernels" / version, args.jobs)
            row["preparations"][version] = prep
            row["phases"][control] = phase(commands, tree, control, args.out_dir / control, prep, args.jobs)
            dump(args.out_dir / "progress.json", row)
        tree = trees["new"]
        try:
            (tree / SOURCE).write_bytes(old)
            row["phases"]["old_on_new"] = phase(commands, tree, "old_on_new", args.out_dir / "old_on_new", prep, args.jobs)
        finally:
            (tree / SOURCE).write_bytes(new)
        row["phases"]["restored_new_on_new"] = phase(commands, tree, "restored_new_on_new", args.out_dir / "restored_new_on_new", prep, args.jobs)
        row["kernel_store_head_after"] = commands.git(args.repo, "rev-parse", "HEAD").decode().strip()
        row["restored_worktrees"] = {v: not commands.git(t, "status", "--porcelain", "--untracked-files=no").strip() for v, t in trees.items()}
        row["source_input_unchanged"] = args.old_source.read_bytes() == old
    except Exception as exc:
        row["exception"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        row["classification"] = classify(row)
        row["commands"] = commands.records
        final = {**started, "finished_at_utc": datetime.now(timezone.utc).isoformat(), "code": code_identity()}
        final["integrity_verified"] = bool(
            started["code"]["commit"] == final["code"]["commit"]
            and started["code"]["source_digest"] == final["code"]["source_digest"]
            and not started["code"]["tracked_worktree_dirty"] and not final["code"]["tracked_worktree_dirty"]
            and row.get("source_input_unchanged") and all(row.get("restored_worktrees", {"missing": False}).values())
            and row.get("kernel_store_head_before") == row.get("kernel_store_head_after"))
        row["integrity_verified"] = final["integrity_verified"]
        dump(args.out_dir / "run_metadata.json", final)
        dump(args.out_dir / "summary.json", row)
        print(row["classification"], flush=True)
    return 0 if final["integrity_verified"] and row["classification"] in (
        "observed_module_compile_break", "old_source_module_builds") else 2


if __name__ == "__main__":
    raise SystemExit(main())
