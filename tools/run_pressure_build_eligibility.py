#!/usr/bin/env python3
"""Evaluation-only, controlled file-object compilation of frozen reserved cases.

This is a file-level diagnostic, not module/link/runtime eligibility. Each case
gets retained, isolated old/new Git worktrees; no production inputs are changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import struct
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_context_pressure_set import load_pressure_cases, verify_pressure_set
from ctxlab.provenance import case_index, code_identity

PROTOCOL = "pressure_file_build_eligibility_v1"
PHASES = ("old_on_old", "new_on_new", "old_on_new", "restored_new_on_new")
DISABLE_CONFIG = ("DEBUG_INFO", "DEBUG_INFO_BTF", "DEBUG_INFO_BTF_MODULES", "GDB_SCRIPTS",
                  "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF5")
INFRA = re.compile(r"No rule to make target|command not found|cannot execute|Permission denied|"
                   r"No space left on device|Cannot allocate memory|out of memory|"
                   r"Killed signal terminated program|unrecognized command.line option|"
                   r"unable to execute|Exec format error|internal compiler error", re.I)
COMPILER_ERROR = re.compile(r"^.*\.(?:c|h):\d+(?::\d+)?:\s*(?:fatal )?error:", re.M)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid repository-relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")) or not re.fullmatch(r"[A-Za-z0-9_./+-]+", value):
        raise ValueError(f"unsafe repository-relative path: {value!r}")
    return value


def validate_mapping(document, cases):
    records = document.get("cases")
    if not isinstance(records, list):
        raise ValueError("mapping must contain a cases list")
    mappings = {}
    for item in records:
        cid = item.get("case_id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", cid) or cid in mappings:
            raise ValueError("mapping has an unsafe or duplicate case_id")
        for key in ("old_commit", "new_commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", str(item.get(key, ""))):
                raise ValueError(f"{cid}: {key} must be a canonical full commit hash")
        source = safe_path(item.get("source_file"))
        target = safe_path(item.get("object_target"))
        if not source.endswith(".c") or target != source[:-2] + ".o":
            raise ValueError(f"{cid}: object_target must compile source_file directly")
        if item.get("owner_target") is not None:
            safe_path(item["owner_target"])
        for key in ("required_config", "config_enable", "config_disable"):
            values = item.get(key, [])
            if not isinstance(values, list) or len(values) != len(set(values)) or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9_]+", v) or v.startswith("CONFIG_") for v in values
            ):
                raise ValueError(f"{cid}: invalid {key}; use symbols without CONFIG_")
        if not item.get("required_config") and not item.get("skip_reason"):
            raise ValueError(f"{cid}: required_config must identify a selectable owner")
        if set(item.get("config_enable", [])) & set(item.get("config_disable", [])):
            raise ValueError(f"{cid}: contradictory configuration requests")
        mappings[cid] = item
    if set(mappings) != {case["case_id"] for case in cases}:
        raise ValueError("mapping must contain exactly the frozen manifest case set")
    for case in cases:
        item = mappings[case["case_id"]]
        if item["new_commit"] != case["meta"]["commit"]:
            raise ValueError(f"{case['case_id']}: mapping migration commit differs from pack")
        if item["source_file"] not in case["old_sources_by_path"]:
            raise ValueError(f"{case['case_id']}: mapped source is absent from pack")
    return mappings


class Commands:
    def __init__(self, root, timeout):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.records = []

    def run(self, argv, cwd, label):
        log = self.root / f"{len(self.records):03d}_{label}.log"
        start = time.monotonic()
        record = {"argv": [str(x) for x in argv], "cwd": str(cwd), "log": str(log),
                  "started_at_utc": datetime.now(timezone.utc).isoformat(),
                  "timed_out": False, "returncode": None, "error": None}
        self.records.append(record)
        try:
            with log.open("xb") as output:
                process = subprocess.Popen(record["argv"], cwd=cwd, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    record["returncode"] = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    record["timed_out"] = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    # The parent may exit while a child ignores TERM. Kill the
                    # whole original group even if process.wait already returned.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if process.poll() is None:
                        process.wait(timeout=5)
                    record["returncode"] = process.returncode
        except OSError as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["duration_seconds"] = round(time.monotonic() - start, 3)
            dump(log.with_suffix(".json"), record)
        return record

    @staticmethod
    def content(record):
        return Path(record["log"]).read_bytes()

    @classmethod
    def text(cls, record):
        return cls.content(record).decode(errors="replace")

    @staticmethod
    def ok(record):
        return record["returncode"] == 0 and not record["timed_out"] and not record["error"]

    def git(self, repo, *args):
        result = self.run(["git", "-C", repo, *args], repo, "git")
        if not self.ok(result):
            raise RuntimeError(f"Git command failed; see {result['log']}")
        return self.content(result)


def config_values(raw):
    values = {}
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key[7:]] = value
        match = re.fullmatch(r"# CONFIG_(\w+) is not set", line)
        if match:
            values[match[1]] = "n"
    return values


def configure(commands, tree, mapping, snapshot_dir, jobs):
    records = []
    records.append(commands.run(["make", "ARCH=x86_64", "allmodconfig"], tree, "allmodconfig"))
    if not commands.ok(records[-1]):
        return {"ok": False, "commands": records, "failure_stage": "allmodconfig"}
    argv = ["scripts/config"]
    for symbol in sorted(set(DISABLE_CONFIG) | set(mapping.get("config_disable", []))):
        argv += ["--disable", symbol]
    for symbol in sorted({"COMPILE_TEST", "DEBUG_INFO_NONE"} | set(mapping.get("config_enable", []))):
        argv += ["--enable", symbol]
    records.append(commands.run(argv, tree, "config_edit"))
    if commands.ok(records[-1]):
        records.append(commands.run(["make", "ARCH=x86_64", "olddefconfig"], tree, "olddefconfig"))
    if not commands.ok(records[-1]):
        return {"ok": False, "commands": records, "failure_stage": "configuration"}
    raw = (tree / ".config").read_bytes()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / ".config").write_bytes(raw)
    values = config_values(raw)
    required = {key: values.get(key, "n") for key in mapping.get("required_config", [])}
    result = {"ok": True, "commands": records, "sha256": sha(raw), "snapshot": str(snapshot_dir / ".config"),
              "required_values": required, "required_active": bool(required) and all(v in ("y", "m") for v in required.values()),
              "requested_enable_values": {k: values.get(k, "n") for k in ["COMPILE_TEST", "DEBUG_INFO_NONE", *mapping.get("config_enable", [])]},
              "requested_disable_values": {k: values.get(k, "n") for k in sorted(set(DISABLE_CONFIG) | set(mapping.get("config_disable", [])))}}
    result["recipe_satisfied"] = (values.get("COMPILE_TEST") == "y" and values.get("DEBUG_INFO_NONE") == "y"
                                  and all(v == "n" for v in result["requested_disable_values"].values()))
    if not result["required_active"] or not result["recipe_satisfied"]:
        return result
    prepared = commands.run(["make", f"-j{jobs}", "ARCH=x86_64", "modules_prepare"], tree, "modules_prepare")
    result["commands"].append(prepared)
    result["ok"] = commands.ok(prepared)
    result["config_unchanged_after_prepare"] = sha((tree / ".config").read_bytes()) == result["sha256"]
    if not result["ok"]:
        result["failure_stage"] = "modules_prepare"
    return result


def compiler_command_seen(text, source):
    source_pattern = re.compile(r"(?<![A-Za-z0-9_./-])(?:\./)?" + re.escape(source) + r"(?![A-Za-z0-9_./-])")
    # V=1 must echo an actual compilation command, not merely the outer make invocation.
    return any(re.search(r"(?:^|[\s/])(?:[\w.+-]*-)?(?:gcc|clang|cc)(?:-[\d.]+)?\s", line)
               and re.search(r"(?:^|\s)-c(?:\s|$)", line) and source_pattern.search(line)
               for line in text.splitlines())


def elf_object(raw):
    return (len(raw) >= 64 and raw[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<HH", raw, 16) == (1, 62))


def compile_phase(commands, tree, mapping, name, phase_dir, config_hash, jobs):
    phase_dir.mkdir(parents=True, exist_ok=False)
    obj = tree / mapping["object_target"]
    cmd = obj.with_name("." + obj.name + ".cmd")
    prior = {}
    for path in (obj, cmd):
        if path.is_symlink():
            raise ValueError(f"refusing symlink build artifact: {path}")
        if path.exists():
            prior[path.name] = sha(path.read_bytes())
            path.unlink()
    missing_before = not obj.exists() and not cmd.exists()
    config_before = sha((tree / ".config").read_bytes())
    result = commands.run(["make", f"-j{jobs}", "ARCH=x86_64", "V=1", mapping["object_target"]], tree, name)
    text = commands.text(result)
    config_after = sha((tree / ".config").read_bytes())
    artifact = obj.read_bytes() if obj.is_file() and not obj.is_symlink() else b""
    cmd_bytes = cmd.read_bytes() if cmd.is_file() and not cmd.is_symlink() else b""
    if artifact:
        (phase_dir / obj.name).write_bytes(artifact)
    if cmd_bytes:
        (phase_dir / cmd.name).write_bytes(cmd_bytes)
    compiler_seen = compiler_command_seen(text, mapping["source_file"])
    value = {"phase": name, "command": result, "removed_prior_artifact_sha256": prior,
             "artifact_missing_before": missing_before, "artifact_sha256": sha(artifact) if artifact else None,
             "artifact_bytes": len(artifact), "artifact_is_elf": elf_object(artifact),
             "artifact_snapshot": str(phase_dir / obj.name) if artifact else None,
             "kbuild_cmd_sha256": sha(cmd_bytes) if cmd_bytes else None,
             "kbuild_cmd_snapshot": str(phase_dir / cmd.name) if cmd_bytes else None,
             "compiler_command_seen": compiler_seen, "compiler_diagnostic": bool(COMPILER_ERROR.search(text)),
             "infrastructure_marker": bool(INFRA.search(text)),
             "config_before_sha256": config_before, "config_after_sha256": config_after,
             "config_unchanged": config_before == config_after == config_hash,
             "source_sha256": sha((tree / mapping["source_file"]).read_bytes())}
    value["passed"] = bool(commands.ok(result) and not value["infrastructure_marker"] and missing_before
                           and value["artifact_is_elf"] and compiler_seen and value["config_unchanged"])
    dump(phase_dir / "phase.json", value)
    return value


def classify(row):
    if row.get("validation_status") != "ok":
        return row.get("validation_status", "input_invalid")
    configs = row.get("configs", {})
    if any(config.get("ok") and (not config.get("required_active") or not config.get("recipe_satisfied")) for config in configs.values()):
        return "config_unavailable"
    if set(configs) != {"old", "new"} or any(not c.get("ok") or not c.get("config_unchanged_after_prepare") for c in configs.values()):
        return "infrastructure_failure"
    phases = row.get("phases", {})
    if set(phases) != set(PHASES):
        return "infrastructure_failure"
    if any(p["command"].get("timed_out") or p["command"].get("error") or p.get("infrastructure_marker") for p in phases.values()):
        return "infrastructure_failure"
    if not all(p.get("config_unchanged") for p in phases.values()):
        return "configuration_changed"
    if not all(phases[name]["passed"] for name in ("old_on_old", "new_on_new", "restored_new_on_new")):
        return "control_failure"
    p0 = phases["old_on_new"]
    if p0["passed"]:
        return "old_source_compiles"
    if p0["command"]["returncode"] != 0 and p0["compiler_diagnostic"] and p0["compiler_command_seen"]:
        return "observed_file_compile_break"
    return "infrastructure_failure"


def run_case(repo, case_dir, case, mapping, out_dir, jobs, timeout):
    cid = case["case_id"]
    case_out = out_dir / "cases" / cid
    case_out.mkdir(parents=True, exist_ok=False)
    commands = Commands(case_out / "logs", timeout)
    row = {"case_id": cid, "mapping": mapping, "validation_status": "pending", "configs": {}, "phases": {},
           "scope": "file_object_only; only packed sources transplanted; sibling sources remain at new commit"}
    try:
        new = commands.git(repo, "rev-parse", "--verify", mapping["new_commit"] + "^{commit}").decode().strip()
        old = commands.git(repo, "rev-parse", "--verify", new + "^1").decode().strip()
        if old != mapping["old_commit"]:
            raise ValueError("mapped old commit is not the migration first parent")
        packed = {}
        for path in sorted((case_dir / "old_module_sources").rglob("*")):
            if path.is_symlink():
                raise ValueError("packed source contains a symlink")
            if path.is_file():
                relative = safe_path(path.relative_to(case_dir / "old_module_sources").as_posix())
                packed[relative] = path.read_bytes()
        if not packed or mapping["source_file"] not in packed:
            raise ValueError("packed target source is missing")
        row["packed_source_sha256"] = {name: sha(raw) for name, raw in packed.items()}
        for name, raw in packed.items():
            if commands.git(repo, "show", f"{old}:{name}") != raw:
                raise ValueError(f"packed source differs from exact parent Git bytes: {name}")
        listing = commands.git(repo, "ls-tree", "-z", new, "--", mapping["source_file"])
        if not listing:
            row["validation_status"] = "target_removed"
            row["skip_evidence"] = {"new_commit": new, "source_file": mapping["source_file"], "new_git_tree_entry": None}
            return row
        pristine = {}
        for name in packed:
            entry = commands.git(repo, "ls-tree", "-z", new, "--", name)
            if not entry.startswith((b"100644 blob ", b"100755 blob ")):
                raise ValueError(f"new packed source is not an ordinary file: {name}")
            pristine[name] = commands.git(repo, "show", f"{new}:{name}")
        row["pristine_new_source_sha256"] = {name: sha(raw) for name, raw in pristine.items()}
        tree_root = out_dir / "worktrees" / cid
        tree_root.mkdir(parents=True, exist_ok=False)
        trees = {version: tree_root / version for version in ("old", "new")}
        for version, commit in (("old", old), ("new", new)):
            commands.git(repo, "worktree", "add", "--detach", str(trees[version]), commit)
        row["worktrees"] = {key: str(path) for key, path in trees.items()}
        row["validation_status"] = "ok"
        for version in ("old", "new"):
            row["configs"][version] = configure(commands, trees[version], mapping, case_out / "configs" / version, jobs)
        if not all(c.get("ok") and c.get("required_active") and c.get("recipe_satisfied") and c.get("config_unchanged_after_prepare") for c in row["configs"].values()):
            return row
        for name, version in (("old_on_old", "old"), ("new_on_new", "new")):
            row["phases"][name] = compile_phase(commands, trees[version], mapping, name, case_out / name, row["configs"][version]["sha256"], jobs)
        # All new-kernel conditions share the same configuration and sibling sources.
        try:
            for name, raw in packed.items():
                (trees["new"] / name).write_bytes(raw)
            row["phases"]["old_on_new"] = compile_phase(commands, trees["new"], mapping, "old_on_new", case_out / "old_on_new", row["configs"]["new"]["sha256"], jobs)
        finally:
            for name, raw in pristine.items():
                (trees["new"] / name).write_bytes(raw)
            row["restored_source_sha256"] = {name: sha((trees["new"] / name).read_bytes()) for name in pristine}
        row["phases"]["restored_new_on_new"] = compile_phase(commands, trees["new"], mapping, "restored_new_on_new", case_out / "restored_new_on_new", row["configs"]["new"]["sha256"], jobs)
    except Exception as exc:
        row["exception"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        row["validation_status"] = "input_invalid" if isinstance(exc, ValueError) else "infrastructure_failure"
    finally:
        row["commands"] = commands.records
        row["classification"] = classify(row)
        dump(case_out / "result.json", row)
    return row


def fresh_output(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to reuse output path: {path}")
    path.mkdir(parents=True, exist_ok=False)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout-sec", type=int, default=600)
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout_sec < 1:
        parser.error("jobs and timeout must be positive")
    args.repo, args.dataset_dir, args.manifest, args.mapping, args.out_dir = (
        path.absolute() for path in (args.repo, args.dataset_dir, args.manifest, args.mapping, args.out_dir))
    if args.out_dir.exists() or args.out_dir.is_symlink():
        parser.error("output path already exists; use a fresh run directory")
    cases = load_pressure_cases(args.manifest, args.dataset_dir, budget=4096, split="reserved")
    if any(case is None for case in cases):
        raise ValueError("frozen manifest contains an incomplete pack")
    mapping_document = json.loads(args.mapping.read_text())
    if mapping_document.get("manifest_sha256") and mapping_document["manifest_sha256"] != sha(args.manifest.read_bytes()):
        raise ValueError("mapping is bound to a different manifest digest")
    mappings = validate_mapping(mapping_document, cases)
    started = {"protocol": PROTOCOL, "evaluation_only": True, "code": code_identity(),
               "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "manifest_sha256": sha(args.manifest.read_bytes()), "mapping_sha256": sha(args.mapping.read_bytes()),
               "case_ids": [case["case_id"] for case in cases], "config": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "model_inference_calls": 0, "runtime_tests": 0, "module_builds": 0,
               "scope": "controlled x86_64 file-object compile diagnostic; worktrees retained; no patch generation"}
    fresh_output(args.out_dir)
    dump(args.out_dir / "run_metadata.started.json", started)
    versions = Commands(args.out_dir / "tool_versions", min(args.timeout_sec, 30))
    for program in ("git", "make", "gcc", "cc", "ld", "as", "python3"):
        versions.run([program, "--version"], args.repo, "version_" + program)
    rows = []
    index = case_index(args.dataset_dir)
    for number, case in enumerate(cases, 1):
        row = run_case(args.repo, index[case["case_id"]], case, mappings[case["case_id"]], args.out_dir, args.jobs, args.timeout_sec)
        rows.append(row)
        summary = {"protocol": PROTOCOL, "complete": False, "n_expected": len(cases), "n_completed": len(rows),
                   "classifications": dict(Counter(item["classification"] for item in rows)), "cases": rows}
        dump(args.out_dir / "summary.json", summary)
        print(f"[{number}/{len(cases)}] {case['case_id']}: {row['classification']}", flush=True)
    final = {**started, "finished_at_utc": datetime.now(timezone.utc).isoformat(), "code": code_identity(), "tool_versions": versions.records}
    final["integrity"] = {"code_unchanged": all(started["code"][key] == final["code"][key] for key in ("commit", "source_digest")),
                          "manifest_unchanged": started["manifest_sha256"] == sha(args.manifest.read_bytes()),
                          "mapping_unchanged": started["mapping_sha256"] == sha(args.mapping.read_bytes())}
    try:
        verify_pressure_set(args.manifest, args.dataset_dir, budget=4096, split="reserved")
        final["integrity"]["full_frozen_pool_unchanged"] = True
    except Exception as exc:
        final["integrity"]["full_frozen_pool_unchanged"] = False
        final["input_integrity_error"] = str(exc)
    final["integrity"]["verified"] = all(final["integrity"].values())
    dump(args.out_dir / "run_metadata.json", final)
    summary.update({"complete": True, "integrity_verified": final["integrity"]["verified"],
                    "interpretation": "File-object evidence only; owner/module link and runtime remain untested."})
    dump(args.out_dir / "summary.json", summary)
    return 0 if final["integrity"]["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
