#!/usr/bin/env python3
"""Freeze and run production extraction stages 1–4, without inference or builds."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading

ROOT = Path(os.environ.get("DRIVER_AGENT_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from driver_agent_pregen.definition_locator import run_definition_locator
from driver_agent_pregen.kbuild_target import HEADER_SUFFIXES, relative_include_path, resolve_kbuild_target, source_includes
from driver_agent_pregen.kernel_diff_builder import run_kernel_diff_builder
from driver_agent_pregen.kernel_evidence_pool import _KERNEL_ROOTS, build_kernel_evidence_pool
from driver_agent_pregen.module_source_loader import run_module_source_loader
from driver_agent_pregen.repo_utils import RepositoryAccessor
from driver_agent_pregen.symbol_scanner import run_symbol_scanner
from ctxlab.provenance import code_identity

PROTOCOL = "docs-impl/28_explicit_excerpt_provenance.md"
CODE_FILES = ["driver_agent_pregen/" + name + ".py" for name in (
    "__init__", "definition_locator", "kbuild_target", "kernel_diff_builder", "kernel_evidence_pool",
    "module_source_loader", "repo_utils", "symbol_scanner", "utils",
)] + ["tools/extract_kernel_evidence.py", "tools/run_extraction_boundary_checks.py", PROTOCOL,
      "tools/ctxlab/__init__.py", "tools/ctxlab/provenance.py", "tools/ctxlab/dataset.py"]


class BoundaryViolation(BaseException):
    """Cannot be swallowed by the extractors' best-effort Exception handlers."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def now():
    return datetime.now(timezone.utc).isoformat()


def identity():
    code = code_identity(ROOT)
    commit = code["commit"]
    if not commit or code["tracked_worktree_dirty"]:
        raise ValueError("extraction requires a clean committed code checkout")
    files = {}
    for name in sorted(set(code["source_files"]) | {PROTOCOL}):
        path = ROOT / name
        if path.is_symlink():
            raise ValueError("symlink executable or protocol")
        raw = path.read_bytes()
        if raw != git(ROOT, "show", commit + ":" + name):
            raise ValueError("extraction executable or protocol differs from commit: " + name)
        files[name] = sha(raw)
    if Path(__file__).resolve() != ROOT / "tools/extract_kernel_evidence.py":
        raise ValueError("DRIVER_AGENT_ROOT does not identify the executing checkout")
    return {"commit": commit, "branch": code["branch"], "source_digest": code["source_digest"], "files_sha256": files}


def safe_path(path):
    if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./+@-]+", path)
            or path.startswith("/") or PurePosixPath(path).as_posix() != path
            or any(part in {".", ".."} for part in path.split("/"))):
        raise BoundaryViolation("noncanonical repository path")
    return path


def kernel_path(path):
    safe_path(path)
    return path.split("/")[0] in _KERNEL_ROOTS and "drivers" not in path.split("/")


class ProcessGuard:
    def __init__(self):
        self.process_count = 0
        self.blocked = []

    def __call__(self, event, args):
        if event.startswith("socket."):
            self.blocked.append({"event": event})
            raise BoundaryViolation("network access is forbidden during extraction")
        if event != "subprocess.Popen":
            return
        argv = args[1]
        allowed = {"rev-parse", "status", "ls-tree", "cat-file", "show", "grep", "diff"}
        if (not isinstance(argv, (list, tuple)) or len(argv) < 4 or argv[0] != "git"
                or argv[1] != "-C" or (argv[3] not in allowed and list(argv[3:]) != ["branch", "--show-current"])):
            self.blocked.append({"event": event, "program": str(args[0])})
            raise BoundaryViolation("only read-only Git subprocesses are allowed during extraction")
        self.process_count += 1


class GuardedAccessor(RepositoryAccessor):
    def __init__(self, *, module_path, **kwargs):
        super().__init__(**kwargs)
        self.module_path = safe_path(module_path)
        self.prefix = self.module_path + "/"
        self.old_sources = set()
        self.old_headers = set()
        self.old_makefiles = {self.prefix + "Makefile", self.prefix + "Kbuild"}
        self.accesses = []
        self.lock = threading.Lock()

    def record(self, **row):
        with self.lock:
            self.accesses.append(row)

    def authorize_sources(self, paths):
        for path in paths:
            safe_path(path)
            if not path.startswith(self.prefix):
                raise BoundaryViolation("Kbuild source leaves module scope")
        self.old_sources = set(paths)

    def read_file(self, side, relative_path):
        safe_path(relative_path)
        if side not in {"old", "new"}:
            raise BoundaryViolation("invalid source side")
        is_old_module = side == "old" and relative_path in (self.old_makefiles | self.old_sources | self.old_headers)
        if not is_old_module and (relative_path.startswith(self.prefix) or not kernel_path(relative_path)):
            self.record(operation="blocked_read", side=side, path=relative_path)
            raise BoundaryViolation("forbidden source content: " + side + ":" + relative_path)
        ref = self.old_ref if side == "old" else self.new_ref
        raw = git(self.repo, "show", ref + ":" + relative_path)
        text = raw.decode("utf-8")
        self.record(operation="read", side=side, path=relative_path, bytes=len(raw), sha256=sha(raw))
        if side == "old" and relative_path in (self.old_sources | self.old_headers):
            for _, kind, include in source_includes(text):
                candidate = relative_include_path(relative_path, include) if kind == "quoted" else None
                if candidate and candidate.startswith(self.prefix) and Path(candidate).suffix.lower() in HEADER_SUFFIXES:
                    safe_path(candidate)
                    self.old_headers.add(candidate)
        return text

    def list_module_files(self, *args, **kwargs):
        raise BoundaryViolation("directory-wide source enumeration is forbidden")

    def search(self, side, pattern, *, pathspecs=None, **kwargs):
        # Apply the permitted-root boundary BEFORE grep, not after reading hits.
        requested = list(pathspecs or [])
        effective = []
        for spec in requested:
            if spec.startswith((":(exclude)", ":(exclude,glob)")):
                effective.append(spec)
            elif spec == ".":
                effective.extend(root + "/" for root in sorted(_KERNEL_ROOTS))
            elif spec.split("/")[0] in _KERNEL_ROOTS and "drivers" not in spec.split("/"):
                effective.append(spec)
            else:
                raise BoundaryViolation("forbidden kernel search pathspec: " + spec)
        if not requested or all(spec.startswith(":") for spec in requested):
            effective.extend(root + "/" for root in sorted(_KERNEL_ROOTS))
        effective.extend([":(exclude)" + self.module_path, ":(exclude)drivers", ":(exclude,glob)**/drivers/**"])
        self.record(operation="search", side=side, requested_pathspecs=requested,
                    effective_pathspecs=effective, pattern_sha256=sha(pattern.encode()))
        hits = super().search(side, pattern, pathspecs=effective, **kwargs)
        if any(not kernel_path(hit.path) for hit in hits):
            raise BoundaryViolation("forbidden kernel search result")
        return hits

    def refs_diff(self, pathspecs=None, max_chars=200000):
        paths = list(pathspecs or [])
        if not paths or any(path.startswith(self.prefix) or not kernel_path(path) for path in paths):
            raise BoundaryViolation("diff requires explicit permitted kernel paths")
        self.record(operation="kernel_diff", paths=paths)
        return super().refs_diff(paths, max_chars=max_chars)

    def log_s(self, *args, **kwargs):
        raise BoundaryViolation("commit-message evidence is forbidden")

    def commit_details(self, *args, **kwargs):
        raise BoundaryViolation("commit evidence is forbidden")

    def source_reader(self, ref, path):
        if ref not in {self.old_ref, self.new_ref}:
            raise BoundaryViolation("unfrozen source ref")
        return self.read_file("old" if ref == self.old_ref else "new", path)


def run(args):
    out = args.out_dir.absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("refusing to reuse an output directory")
    if any(not re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (args.old_ref, args.new_ref)):
        raise ValueError("full immutable old/new commit IDs are required")
    if args.old_ref == args.new_ref or not 1 <= args.jobs <= 32:
        raise ValueError("distinct refs and 1–32 workers are required")
    safe_path(args.module_path)
    if "/" in args.kbuild_target or not re.fullmatch(r"[A-Za-z0-9_+.-]+", args.kbuild_target):
        raise ValueError("Kbuild target must be a basename")
    code = identity()
    repo = args.repo.resolve(strict=True)
    kernel_head = git(repo, "rev-parse", "HEAD").decode().strip()
    for ref in (args.old_ref, args.new_ref):
        if git(repo, "rev-parse", ref + "^{commit}").decode().strip() != ref:
            raise ValueError("declared ref is not an exact commit")
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"protocol": "kernel_evidence_extraction_v2", "started_at_utc": now(),
                "code": code, "kernel_head": kernel_head,
                "arguments": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
                "settings": {"source_mode": "full_module", "affected_func_signal_mode": "kernel_only",
                             "collect_commits": False, "semantic_obligation_table": None,
                             "allowed_stages": ["module_source_loader", "symbol_scanner", "definition_locator", "kernel_diff_builder"],
                             "kernel_search_boundary": "allowlisted roots before grep; nested drivers excluded"},
                "model_calls_authorized": 0, "build_calls_authorized": 0}
    write(out / "freeze/manifest.json", manifest)
    with (out / "freeze/protocol.md").open("xb") as stream:
        stream.write((ROOT / PROTOCOL).read_bytes())
    freeze_sha256 = sha((out / "freeze/manifest.json").read_bytes())
    guard = ProcessGuard()
    sys.addaudithook(guard)
    os.environ["DRIVER_AGENT_GIT_GREP_THREADS"] = "1"
    accessor = GuardedAccessor(repo=repo, old_ref=args.old_ref, new_ref=args.new_ref, module_path=args.module_path)
    report = {"status": "failed", "generation_ready": False, "actual_token_budget_comparison": False,
              "freeze_sha256": freeze_sha256, "stages": []}
    failure = None
    try:
        extracted = out / "extracted"
        resolution = resolve_kbuild_target(accessor, side="old", module_path=args.module_path,
                                          kbuild_target=args.kbuild_target, include_all_headers=False)
        accessor.authorize_sources(resolution.source_files)
        module = run_module_source_loader(accessor, module_path=args.module_path, extracted_dir=extracted,
                                          kbuild_target=args.kbuild_target)
        if module.kbuild_target_resolution["warnings"]:
            raise ValueError("unresolved Kbuild/include scope: " + repr(module.kbuild_target_resolution["warnings"]))
        if set(module.kbuild_target_resolution["source_files"]) != accessor.old_sources:
            raise BoundaryViolation("owned translation unit inventory changed")
        report["stages"].append("module_source_loader")
        scan = run_symbol_scanner(module, extracted)
        report["stages"].append("symbol_scanner")
        locations = run_definition_locator(accessor, extracted, scan["driver_used_symbols"],
                                           module_path=args.module_path, exclude_driver_paths=True, jobs=args.jobs)
        report["stages"].append("definition_locator")
        run_kernel_diff_builder(accessor, extracted, locations, args.module_path, collect_commits=False,
                                affected_func_signal_mode="kernel_only", old_ref=args.old_ref, new_ref=args.new_ref,
                                semantic_obligation_table=None)
        report["stages"].append("kernel_diff_builder")
        sources = {}
        for path in module.source_files_old:
            blob = (module.copied_root / path).read_bytes()
            if blob.decode("utf-8") != accessor.source_reader(args.old_ref, path):
                raise BoundaryViolation("old source bytes changed during extraction")
            sources[path] = {"sha256": sha(blob), "bytes": len(blob), "lines": len(blob.splitlines())}
        report["source_inventory"] = sources
        report["headers"] = module.headers_old
        try:
            pool = build_kernel_evidence_pool(extracted, source_reader=accessor.source_reader)
        except ValueError as exc:
            report.update(status="pool_rejected", pool_boundary_refusal=str(exc))
        else:
            write(out / "kernel_evidence_pool.json", pool)
            report.update(status="prepared", pool_sha256=pool["audit"]["pool_sha256"],
                          candidate_count=len(pool["candidates"]), obligation_signal_count=len(pool["obligation_signal"]),
                          pool_source_verification=pool["audit"]["source_verification"])
    except BaseException as exc:
        failure = exc
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        try:
            report["code_unchanged"] = identity() == code
            report["kernel_head_unchanged"] = git(repo, "rev-parse", "HEAD").decode().strip() == kernel_head
            report["freeze_unchanged"] = sha((out / "freeze/manifest.json").read_bytes()) == freeze_sha256
            report["protocol_unchanged"] = sha((out / "freeze/protocol.md").read_bytes()) == code["files_sha256"][PROTOCOL]
            if "source_inventory" in report:
                copied = out / "extracted/old_module_sources"
                final_paths = {path.relative_to(copied).as_posix() for path in copied.rglob("*") if path.is_file()}
                report["old_source_inventory_unchanged"] = final_paths == set(report["source_inventory"])
                for name, info in report["source_inventory"].items():
                    raw = (copied / name).read_bytes()
                    report["old_source_inventory_unchanged"] &= (
                        sha(raw) == info["sha256"] and raw.decode("utf-8") == accessor.source_reader(args.old_ref, name))
                if not report["old_source_inventory_unchanged"]:
                    raise BoundaryViolation("old source inventory or bytes changed")
            report["forbidden_modules_loaded"] = sorted(name for name in sys.modules if name in {
                "driver_agent_pregen.llm_client", "driver_agent_pregen.validation", "driver_agent_pregen.patch_generation"})
            if (not all(report[key] for key in ("code_unchanged", "kernel_head_unchanged", "freeze_unchanged", "protocol_unchanged"))
                    or guard.blocked or report["forbidden_modules_loaded"]):
                raise BoundaryViolation("extraction integrity/operation boundary failed")
        except BaseException as exc:
            failure = failure or exc
            report.update(status="failed", final_integrity_error=str(exc))
        report.update(finished_at_utc=now(), model_call_count=0, build_call_count=0,
                      read_only_git_process_count=guard.process_count, blocked_operations=guard.blocked)
        write(out / "access_audit.json", accessor.accesses)
        write(out / "summary.json", report)
        files = {path.relative_to(out).as_posix(): sha(path.read_bytes()) for path in sorted(out.rglob("*")) if path.is_file()}
        write(out / "artifact_index.json", {"files_sha256": files})
    if failure:
        raise failure
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--old-ref", required=True)
    parser.add_argument("--new-ref", required=True)
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--kbuild-target", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    print(json.dumps(run(parser.parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
