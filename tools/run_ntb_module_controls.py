#!/usr/bin/env python3
"""Recipe-driven strict composite-module controls; NTB recipe, evaluation only.

Reuses the existing command, configuration, snapshot and make infrastructure.
Pristine NEW driver files are evaluation-only controls, never generation input.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity
from run_pressure_build_eligibility import (
    Commands, COMPILER_ERROR, INFRA, config_values, dump, elf_object,
    fresh_output, safe_path, sha,
)
from run_xilinx_module_controls import DISABLED, UNSAFE_ENV, make, require, snapshot
from validate_paired_xilinx_patch import frozen_kernel_release

PROTOCOL = "strict_owned_module_controls_v1"
PHASES = ("old_on_old", "new_on_new", "old_on_new", "restored_new_on_new")
KERNEL_INPUTS = ("vmlinux", "vmlinux.o", "vmlinux.symvers", ".vmlinux.o.cmd",
                 ".vmlinux.symvers.cmd", "include/generated/utsrelease.h",
                 "include/config/kernel.release", "include/generated/autoconf.h",
                 "include/config/auto.conf", "scripts/module-common.c", "scripts/Makefile.modfinal")


@dataclass(frozen=True)
class ModuleRecipe:
    case_id: str
    old: str
    new: str
    module_target: str
    source_files: tuple[str, ...]
    local_headers: tuple[str, ...]
    kernel_headers: tuple[str, ...]
    metadata_files: tuple[str, ...]
    required_config: dict[str, str]
    owner_config: str
    disabled_config: tuple[str, ...]
    excluded_builtin_exports: tuple[str, ...] = ()

    @property
    def stem(self):
        return self.module_target[:-3]

    @property
    def owned(self):
        return self.source_files + self.local_headers

    @property
    def constituent_objects(self):
        return tuple(name[:-2] + ".o" for name in self.source_files)


NTB = ModuleRecipe(
    case_id="7bd27439a578__drivers_ntb_msi_c",
    old="b1f4077465b2110d8a486cd6f1aed0c2569c339a",
    new="7bd27439a578bb7724a9f09f240337ab95d68d2b",
    module_target="drivers/ntb/ntb.ko",
    source_files=("drivers/ntb/core.c", "drivers/ntb/msi.c"), local_headers=(),
    kernel_headers=("include/linux/ntb.h",),
    metadata_files=("drivers/ntb/Makefile", "drivers/ntb/Kconfig"),
    required_config={"MODULES": "y", "COMPILE_TEST": "y", "WERROR": "y", "PCI": "y",
                     "PCI_MSI": "y", "NTB": "m", "NTB_MSI": "y", "DEBUG_INFO_NONE": "y"},
    owner_config="NTB",
    disabled_config=tuple(sorted(set(DISABLED) | {"NTB_TRANSPORT", "NTB_NETDEV", "NTB_AMD",
        "NTB_EPF", "NTB_IDT", "NTB_INTEL", "NTB_SWITCHTEC", "NTB_PINGPONG", "NTB_TOOL",
        "NTB_PERF", "NTB_MSI_TEST", "MODULE_SIG", "MODULE_SIG_ALL", "MODULE_SIG_FORCE",
        "GCC_PLUGINS"})),
    excluded_builtin_exports=("ntb_register_device",),
)


def validate_recipe(recipe):
    if not re.fullmatch(r"[a-zA-Z0-9_]+", recipe.case_id) or not all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (recipe.old, recipe.new)):
        raise ValueError("recipe needs a safe case id and exact commit hashes")
    paths = (recipe.module_target,) + recipe.owned + recipe.kernel_headers + recipe.metadata_files
    for name in paths:
        safe_path(name)
    if not recipe.module_target.endswith(".ko") or not recipe.source_files or len(set(recipe.owned)) != len(recipe.owned):
        raise ValueError("recipe must name one module and unique owned sources")
    root = Path(recipe.module_target).parent
    if any(not Path(name).is_relative_to(root) for name in recipe.owned) or any(not name.endswith(".c") for name in recipe.source_files) or any(not name.endswith(".h") for name in recipe.local_headers):
        raise ValueError("owned source scope must stay within the module directory")
    if set(recipe.owned) & (set(recipe.kernel_headers) | set(recipe.metadata_files)):
        raise ValueError("owned source and kernel/metadata boundaries overlap")
    if set(recipe.required_config) & set(recipe.disabled_config) or any(value not in ("y", "m") for value in recipe.required_config.values()):
        raise ValueError("invalid or contradictory configuration")
    if recipe.required_config.get("MODULES") != "y" or recipe.required_config.get("WERROR") != "y" or recipe.required_config.get(recipe.owner_config) != "m":
        raise ValueError("strict module recipe requires MODULES, WERROR and a modular owner")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hashes(tree, names):
    result = {}
    for name in names:
        path = tree / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("expected ordinary source/kernel input: " + name)
        result[name] = file_hash(path)
    return result


def output_paths(recipe):
    outputs = set(recipe.constituent_objects)
    outputs.update(recipe.stem + suffix for suffix in (".o", ".ko", ".mod", ".mod.c", ".mod.o"))
    outputs.update(str(Path(name).with_name("." + Path(name).name + ".cmd")) for name in list(outputs))
    outputs.update(("Module.symvers", ".Module.symvers.cmd"))
    return sorted(outputs)


def remove_outputs(tree, recipe):
    removed = {}
    for name in output_paths(recipe):
        path = tree / name
        if path.is_symlink():
            raise ValueError("symlink module output: " + name)
        if path.exists():
            if not path.is_file():
                raise ValueError("non-file module output: " + name)
            removed[name] = file_hash(path)
    for name in removed:
        (tree / name).unlink()
    return removed


@contextmanager
def transplanted_sources(tree, old_sources, new_sources):
    """Restore every owned file even when writing or the negative build fails."""
    if set(old_sources) != set(new_sources) or hashes(tree, new_sources) != {name: sha(raw) for name, raw in new_sources.items()}:
        raise ValueError("transplant requires the exact complete pristine NEW owned scope")
    try:
        for name, raw in old_sources.items():
            (tree / name).write_bytes(raw)
        yield
    finally:
        for name, raw in new_sources.items():
            (tree / name).write_bytes(raw)


def command_segments(text):
    for line in text.splitlines():
        try:
            parser = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            parser.whitespace_split = True
            parser.commenters = ""
            tokens = list(parser)
        except ValueError:
            continue
        part = []
        for token in tokens + [";"]:
            if token and all(char in ";&|" for char in token):
                if part:
                    yield part
                part = []
            else:
                part.append(token)


def output_is(tokens, name):
    return any(token == "-o" and i + 1 < len(tokens) and tokens[i + 1].removeprefix("./") == name for i, token in enumerate(tokens))


def evidence(text, recipe):
    compilers = {name: [] for name in recipe.source_files}
    composite, final, modpost = [], [], []
    for tokens in command_segments(text):
        program = Path(tokens[0]).name
        if re.fullmatch(r"(?:[\w.+-]+-)?gcc(?:-[\d.]+)?", program) and "-c" in tokens:
            for name in compilers:
                if name in tokens and output_is(tokens, name[:-2] + ".o"):
                    compilers[name].append(tokens)
        elif program in ("ld", "ld.bfd", "ld.lld"):
            inputs = set(token.removeprefix("./") for token in tokens)
            if output_is(tokens, recipe.stem + ".o") and "-r" in tokens and (
                    "@" + recipe.stem + ".mod" in inputs or set(recipe.constituent_objects) <= inputs):
                composite.append(tokens)
            if output_is(tokens, recipe.module_target) and "-r" in tokens and {recipe.stem + ".o", recipe.stem + ".mod.o"} <= inputs:
                final.append(tokens)
        elif tokens[0].removeprefix("./") == "scripts/mod/modpost":
            modpost.append(tokens)
    strict = bool(modpost) and all(
        "vmlinux.o" in tokens and "-M" in tokens and "-E" in tokens
        and output_is(tokens, "Module.symvers") and "-T" in tokens
        and tokens[tokens.index("-T") + 1:tokens.index("-T") + 2] == ["modules.order"]
        and not any(token in ("-w", "-n", "-N", "-e") or token.startswith("-i") for token in tokens[1:])
        for tokens in modpost)
    return {"compiler_commands": compilers, "all_constituents_compiled": all(compilers.values()),
            "composite_link_commands": composite, "final_link_commands": final,
            "modpost_commands": modpost, "strict_modpost": strict}


def owned_compiler_diagnostics(text, recipe):
    paths = "|".join(re.escape(name) for name in recipe.source_files)
    pattern = re.compile(r"^(?:\./)?(?:" + paths + r"):\d+(?::\d+)?:\s*(?:fatal )?error:")
    return [line for line in text.splitlines() if pattern.search(line)]


def prepare_kernel(commands, tree, dest, recipe, jobs, expected_config_sha256=None):
    require(commands, make(jobs, "defconfig"), tree, tree.name + "_defconfig")
    edit = ["scripts/config", "--set-str", "LOCALVERSION", ""]
    for name, value in sorted(recipe.required_config.items()):
        edit.extend(("--module" if value == "m" else "--enable", name))
    for name in recipe.disabled_config:
        edit.extend(("--disable", name))
    require(commands, edit, tree, tree.name + "_config_edit")
    require(commands, make(jobs, "olddefconfig"), tree, tree.name + "_olddefconfig")
    config = snapshot(tree, ".config", dest)
    resolved = config_values((tree / ".config").read_bytes())
    if any(resolved.get(name, "n") != value for name, value in recipe.required_config.items()) or any(resolved.get(name, "n") != "n" for name in recipe.disabled_config):
        raise ValueError("resolved Kconfig does not satisfy the frozen module recipe")
    if expected_config_sha256 is not None and config["sha256"] != expected_config_sha256:
        raise ValueError("OLD and NEW resolved configuration bytes differ before kernel build")
    result = {"config": config, "required_values": {name: resolved[name] for name in recipe.required_config},
              "disabled_values": {name: resolved.get(name, "n") for name in recipe.disabled_config}}
    dump(dest / "preparation.started.json", result)
    print("Building pristine " + tree.name + " kernel for " + recipe.module_target, flush=True)
    result["build_command"] = require(commands, make(jobs, "vmlinux", "modules_prepare"), tree, tree.name + "_kernel_build")
    if file_hash(tree / ".config") != config["sha256"] or not elf_object((tree / "vmlinux.o").read_bytes()):
        raise ValueError("kernel preparation changed configuration or lacks x86_64 vmlinux.o")
    result["kernel_release"] = frozen_kernel_release(tree)
    result["artifacts"] = {name: snapshot(tree, name, dest) for name in KERNEL_INPUTS + recipe.kernel_headers + recipe.metadata_files}
    exports = {line.split()[1] for line in (tree / "vmlinux.symvers").read_text().splitlines() if len(line.split()) >= 3}
    if not exports or exports.intersection(recipe.excluded_builtin_exports):
        raise ValueError("vmlinux export table is empty or contains the supposedly modular owner")
    result["exported_symbols"] = len(exports)
    result["stable_hashes"] = {".config": config["sha256"], **{name: item["sha256"] for name, item in result["artifacts"].items()}}
    dump(dest / "preparation.json", result)
    return result


def phase(commands, tree, dest, recipe, prep, name, jobs, expected_sources):
    dest.mkdir(parents=True, exist_ok=False)
    before_sources = hashes(tree, recipe.owned)
    before_stable = hashes(tree, prep["stable_hashes"])
    if before_sources != expected_sources or before_stable != prep["stable_hashes"]:
        raise ValueError("phase source or kernel inputs differ before build")
    removed = remove_outputs(tree, recipe)
    release = frozen_kernel_release(tree)
    if release != prep["kernel_release"]:
        raise ValueError("kernel release differs from pristine preparation")
    record = commands.run(make(jobs, "KERNELRELEASE=" + release, recipe.module_target), tree, name)
    log = commands.text(record)
    observed = evidence(log, recipe)
    stable_after = hashes(tree, prep["stable_hashes"])
    source_after = hashes(tree, recipe.owned)
    # Kbuild also links a shared kernel-side support object. Preserve it and
    # its command record separately without calling it an owned source.
    phase_artifacts = output_paths(recipe) + [".module-common.o", "..module-common.o.cmd", "scripts/module.lds"]
    artifacts = {path: snapshot(tree, path, dest) for path in phase_artifacts if (tree / path).is_file()}
    ko = tree / recipe.module_target
    members_path = tree / (recipe.stem + ".mod")
    members = members_path.read_text().split() if members_path.is_file() else []
    elf_files = {path: elf_object((tree / path).read_bytes()) for path in recipe.constituent_objects + (recipe.stem + ".o", recipe.module_target) if (tree / path).is_file()}
    infrastructure = bool(record["error"] or record["timed_out"] or INFRA.search(log)
        or stable_after != before_stable or source_after != before_sources
        or re.search(r"WARNING: (?:vmlinux.o|.*Module.symvers) is missing", log))
    result = {"phase": name, "command": record, "source_hashes_before": before_sources,
              "source_hashes_after": source_after, "expected_source_hashes": expected_sources,
              "stable_inputs_before": before_stable, "stable_inputs_after": stable_after,
              "source_verified": before_sources == source_after == expected_sources,
              "stable_kernel_inputs": before_stable == stable_after == prep["stable_hashes"],
              "removed_artifacts": removed, "artifacts": artifacts, "module_members": members,
              "expected_module_members": list(recipe.constituent_objects), "elf_artifacts": elf_files,
              "evidence": observed, "infrastructure_marker": infrastructure,
              "compiler_diagnostic": bool(COMPILER_ERROR.search(log)),
              "owned_compiler_diagnostics": owned_compiler_diagnostics(log, recipe),
              "kernel_release_pin": release,
              "modpost_diagnostic": bool(re.search(r"^ERROR:\s*modpost:", log, re.M)),
              "diagnostics": [line for line in log.splitlines() if "error:" in line or line.startswith("ERROR: modpost:")]}
    result["passed"] = bool(commands.ok(record) and not infrastructure and observed["all_constituents_compiled"]
        and observed["composite_link_commands"] and observed["final_link_commands"] and observed["strict_modpost"]
        and members == list(recipe.constituent_objects)
        and all(elf_files.get(path) for path in recipe.constituent_objects + (recipe.stem + ".o", recipe.module_target)))
    if result["passed"]:
        inspections = [require(commands, argv, tree, name + "_inspect") for argv in (
            ["readelf", "-S", str(ko)], ["readelf", "-p", ".modinfo", str(ko)], ["nm", "-u", str(ko)])]
        imports = re.findall(r"^\s*U\s+(\S+)\s*$", commands.text(inspections[2]), re.M)
        exports = {line.split()[1] for line in (tree / "vmlinux.symvers").read_text().splitlines() if len(line.split()) >= 3}
        modinfo = commands.text(inspections[1])
        module_name = re.escape(Path(recipe.stem).name.replace("-", "_"))
        identity = bool(re.search(r"(?:^|\s)name=" + module_name + r"(?:\s|$)", modinfo)
                        and "vermagic=" + release + " " in modinfo)
        result.update({"inspections": inspections, "strong_imports": imports,
                       "unresolved_imports": sorted(set(imports) - exports), "module_identity_verified": identity,
                       "kernel_release": release})
        result["passed"] = bool(imports and not result["unresolved_imports"] and identity)
    dump(dest / "phase.json", result)
    print(name + ": " + ("PASS" if result["passed"] else "not passed"), flush=True)
    return result


def classify(result):
    if result.get("exception") or not result.get("integrity_verified"):
        return "infrastructure_failure"
    phases = result.get("phases", {})
    if set(phases) != set(PHASES) or any(row["infrastructure_marker"] for row in phases.values()):
        return "infrastructure_failure"
    if not result.get("restored_module_matches_new") or not all(phases[name]["passed"] for name in PHASES if name != "old_on_new"):
        return "control_failure"
    old = phases["old_on_new"]
    if old["passed"]:
        return "old_owned_module_builds_on_new"
    failed_command = type(old["command"]["returncode"]) is int and old["command"]["returncode"] > 0
    if failed_command and old["evidence"]["all_constituents_compiled"] and old["owned_compiler_diagnostics"]:
        return "observed_module_compile_break"
    if failed_command and old["evidence"]["strict_modpost"] and old["modpost_diagnostic"]:
        return "observed_module_link_break"
    return "unresolved_module_failure"


def require_equal_configs(preparations):
    same = preparations["old"]["config"]["sha256"] == preparations["new"]["config"]["sha256"]
    if not same:
        raise ValueError("OLD and NEW resolved configuration bytes differ")
    return same


def restored_module_matches(phases, recipe):
    original = phases["new_on_new"]["artifacts"].get(recipe.module_target)
    restored = phases["restored_new_on_new"]["artifacts"].get(recipe.module_target)
    return bool(original and restored and original["sha256"] == restored["sha256"] and original["bytes"] == restored["bytes"])


def write_artifact_index(out):
    files = {}
    for directory, directories, names in os.walk(out, followlinks=False):
        base = Path(directory)
        if base == out:
            directories[:] = [name for name in directories if name != "worktrees"]
        for name in directories + names:
            path = base / name
            if path.is_symlink():
                raise ValueError("symlink in recorded artifacts")
            if path.is_file() and path != out / "artifact_index.json":
                files[path.relative_to(out).as_posix()] = {"bytes": path.stat().st_size, "sha256": file_hash(path)}
    dump(out / "artifact_index.json", {"files": files, "file_count": len(files), "excluded": ["worktrees/", "artifact_index.json"]})


def run(args, recipe=NTB):
    validate_recipe(recipe)
    if args.jobs < 1 or args.timeout_sec < 1:
        raise ValueError("jobs and timeout must be positive")
    if any(os.environ.get(name) for name in UNSAFE_ENV):
        raise ValueError("unset inherited build overrides: " + ", ".join(name for name in UNSAFE_ENV if os.environ.get(name)))
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    repo, out = args.repo.absolute(), args.out_dir.absolute()
    code = code_identity()
    if code["tracked_worktree_dirty"]:
        raise ValueError("commit code before module-control execution")
    fresh_output(out)
    commands = Commands(out / "logs", args.timeout_sec)
    started = {"protocol": PROTOCOL, "evaluation_only": True, "recipe": asdict(recipe), "code": code,
               "generation_calls": 0, "runtime_tests": 0, "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "args": {"repo": str(repo), "out_dir": str(out), "jobs": args.jobs, "timeout_sec": args.timeout_sec}}
    dump(out / "run_metadata.started.json", started)
    result = {"preparations": {}, "phases": {}, "source_hashes": {}, "recipe": asdict(recipe)}
    trees, sources = {}, {}
    try:
        code_root = Path(__file__).resolve().parents[1]
        frozen_paths = ("tools/run_ntb_module_controls.py", "tools/run_ntb_module_checks.py",
                        "docs-impl/26_ntb_module_control_protocol.md")
        result["frozen_code_files"] = {}
        for path in frozen_paths:
            committed = commands.git(code_root, "show", code["commit"] + ":" + path)
            if file_hash(code_root / path) != sha(committed):
                raise ValueError("runner, checks and protocol must be committed before execution")
            result["frozen_code_files"][path] = snapshot(code_root, path, out / "freeze")
        result["kernel_store_head_before"] = commands.git(repo, "rev-parse", "HEAD").decode().strip()
        if commands.git(repo, "rev-parse", recipe.new + "^1").decode().strip() != recipe.old:
            raise ValueError("recipe kernel commits are not a parent/child pair")
        for label, ref in (("old", recipe.old), ("new", recipe.new)):
            sources[label] = {name: commands.git(repo, "show", ref + ":" + name) for name in recipe.owned}
            result["source_hashes"][label] = {name: sha(raw) for name, raw in sources[label].items()}
            for name, raw in sources[label].items():
                path = out / "inputs" / ("old_owned" if label == "old" else "new_owned_evaluation_only") / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            result.setdefault("metadata_hashes", {})[label] = {name: sha(commands.git(repo, "show", ref + ":" + name)) for name in recipe.metadata_files}
        if result["metadata_hashes"]["old"] != result["metadata_hashes"]["new"]:
            raise ValueError("target Kbuild/Kconfig changed; this frozen recipe requires identical target metadata")
        for program in ("gcc", "g++", "make", "ld", "readelf", "nm", "git", "python3"):
            require(commands, [program, "--version"], repo, "version_" + program.replace("+", "p"))
        for label, ref, phase_name in (("old", recipe.old, "old_on_old"), ("new", recipe.new, "new_on_new")):
            tree = out / "worktrees" / label
            tree.parent.mkdir(exist_ok=True)
            commands.git(repo, "worktree", "add", "--detach", str(tree), ref)
            trees[label] = tree
            if hashes(tree, recipe.owned) != result["source_hashes"][label]:
                raise ValueError("fresh worktree owned source differs from immutable Git")
            if hashes(tree, recipe.metadata_files) != result["metadata_hashes"][label]:
                raise ValueError("fresh worktree target metadata differs from immutable Git")
            expected_config = result["preparations"]["old"]["config"]["sha256"] if label == "new" else None
            prep = prepare_kernel(commands, tree, out / "kernels" / label, recipe, args.jobs, expected_config)
            result["preparations"][label] = prep
            if label == "new":
                result["equal_config_bytes"] = require_equal_configs(result["preparations"])
            result["phases"][phase_name] = phase(commands, tree, out / phase_name, recipe, prep, phase_name, args.jobs, result["source_hashes"][label])
            dump(out / "progress.json", result)
        tree = trees["new"]
        with transplanted_sources(tree, sources["old"], sources["new"]):
            result["phases"]["old_on_new"] = phase(commands, tree, out / "old_on_new", recipe, prep, "old_on_new", args.jobs, result["source_hashes"]["old"])
        result["phases"]["restored_new_on_new"] = phase(commands, tree, out / "restored_new_on_new", recipe, prep, "restored_new_on_new", args.jobs, result["source_hashes"]["new"])
        result["restored_module_matches_new"] = restored_module_matches(result["phases"], recipe)
    except Exception as exc:
        result["exception"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        final_code = code_identity()
        try:
            result["kernel_store_head_after"] = commands.git(repo, "rev-parse", "HEAD").decode().strip()
            result["restored_sources"] = {name: hashes(tree, recipe.owned) == result["source_hashes"][name] for name, tree in trees.items()}
            result["restored_worktrees"] = {name: not commands.git(tree, "status", "--porcelain", "--untracked-files=no").strip() for name, tree in trees.items()}
            result["integrity_verified"] = bool(code == final_code and not final_code["tracked_worktree_dirty"]
                and result.get("kernel_store_head_before") == result["kernel_store_head_after"]
                and result.get("equal_config_bytes") is True
                and all(result["restored_sources"].values()) and all(result["restored_worktrees"].values()))
        except Exception as exc:
            result["integrity_verified"] = False
            result["integrity_error"] = f"{type(exc).__name__}: {exc}"
        result["commands"] = commands.records
        result["classification"] = classify(result)
        dump(out / "summary.json", result)
        dump(out / "run_metadata.json", {**started, "code": final_code,
             "finished_at_utc": datetime.now(timezone.utc).isoformat(), "integrity_verified": result["integrity_verified"]})
        write_artifact_index(out)
        print(result["classification"], flush=True)
    return 0 if result["classification"] == "observed_module_compile_break" else 3 if result["classification"] == "infrastructure_failure" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout-sec", type=int, default=3600)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
