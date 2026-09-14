#!/usr/bin/env python3
"""Offline boundary, determinism and real-extraction checks for the evidence pool."""
from __future__ import annotations

import argparse
from copy import deepcopy
import difflib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver_agent_pregen.kernel_evidence_pool import (
    DOCUMENT_NAMES, EvidenceBoundaryError, build_kernel_evidence_pool,
    git_source_reader, verify_pool_against_git,
)


def fixture():
    path = "drivers/demo/demo.c"
    old_sources = {path: "int demo(void) { return api_a() + api_b(); }\n"}
    documents = {
        "module_source.json": {
            "module_path": "drivers/demo", "old_ref": "a" * 40, "new_ref": "b" * 40,
            "source_files_old": [path], "headers_old": [], "local_include_scope_risks": [],
            "case_scope": "kbuild_target", "kbuild_target": "demo.ko",
            "kbuild_target_resolution": {"module_path": "drivers/demo", "kbuild_target": "demo.ko", "source_files": [path], "headers": [], "files": [path], "warnings": []},
        },
        "kernel_definition_locations.json": {"items": []},
        "kernel_definition_diffs.json": {"affected_func_signal_mode": "kernel_only", "collect_commit_info": False, "items": []},
    }
    for symbol, line in (("api_a", 1), ("api_b", 3)):
        use = [{"file": path, "line": 1, "enclosing_function": "demo", "code": old_sources[path].strip(), "owner_struct": None}]
        old_code, new_code = f"int {symbol}(void);", f"{'long' if symbol == 'api_a' else 'int'} {symbol}(void);"
        documents["kernel_definition_locations.json"]["items"].append({
            "symbol": symbol, "kind": "function", "used_by_driver": use,
            "old_locations": [{"path": "include/linux/demo.h", "line": line, "match": old_code, "locator": "regex"}],
            "new_locations": [{"path": "include/linux/demo.h", "line": line, "match": new_code, "locator": "regex"}],
        })
        documents["kernel_definition_diffs.json"]["items"].append({
            "symbol": symbol, "kind": "function", "used_by_driver": use,
            "old_definition": f"{line}: {old_code}", "new_definition": f"{line}: {new_code}",
            "definition_changed": old_code != new_code,
            "definition_diff": "\n".join(difflib.unified_diff([old_code], [new_code], fromfile=f"old:{symbol}", tofile=f"new:{symbol}", lineterm="")),
            "commits": [], "semantic_table_hit_rows": [], "replacement_candidates": [],
            "kernel_side_helper_diff_paths": [], "direct_include_requirements": [],
        })
    return documents, old_sources


def run_checks():
    passed = []

    def check(name, predicate):
        assert predicate, name
        passed.append(name)

    docs, sources = fixture()
    original = deepcopy(docs)
    build = lambda data: build_kernel_evidence_pool(documents=data, old_sources=sources)
    pool = build(docs)
    check("inputs unchanged", docs == original)
    check("both candidate units retained", len(pool["candidates"]) == 2)
    check("only changed symbol signal", [x["symbol"] for x in pool["obligation_signal"]] == ["api_a"])
    check("mandatory signal contains no replacement type", "long" not in json.dumps(pool["obligation_signal"]))
    check("same old/new excerpt rendered once", pool["candidates"][1]["text"].count("int api_b(void);") == 1)
    shuffled = deepcopy(docs)
    for name in DOCUMENT_NAMES[1:]:
        shuffled[name]["items"].reverse()
    check("candidate IDs and order independent of input row order", build(shuffled)["candidates"] == pool["candidates"])
    check("signal independent of input row order", build(shuffled)["obligation_signal"] == pool["obligation_signal"])
    new_ref = deepcopy(docs)
    new_ref["module_source.json"]["new_ref"] = "c" * 40
    check("candidate identities bind immutable refs", all(a["id"] != b["id"] for a, b in zip(pool["candidates"], build(new_ref)["candidates"])))
    duplicated = deepcopy(docs)
    for name in DOCUMENT_NAMES[1:]:
        duplicated[name]["items"].append(deepcopy(duplicated[name]["items"][0]))
    check("duplicate rows do not create candidates", build(duplicated)["candidates"] == pool["candidates"])
    alias = deepcopy(docs)
    for name in DOCUMENT_NAMES[1:]:
        extra = deepcopy(alias[name]["items"][1])
        extra["kind"] = "macro"
        alias[name]["items"].append(extra)
    alias_pool = build(alias)
    check("different scanner kind deduplicated", len(alias_pool["candidates"]) == 2)
    check("kind alias keeps stable ID", alias_pool["candidates"][1]["id"] == pool["candidates"][1]["id"])
    discarded = deepcopy(docs)
    discarded["module_source.json"]["note"] = "SENTINEL_CASE_HINT"
    discarded["kernel_definition_diffs.json"]["items"][0]["open_questions"] = ["SENTINEL_CASE_HINT"]
    discarded["kernel_definition_locations.json"]["items"][0]["notes"] = ["SENTINEL_CASE_HINT"]
    stripped = build(discarded)
    check("known narrative metadata never reaches candidates or signals", "SENTINEL_CASE_HINT" not in json.dumps({k: stripped[k] for k in ("candidates", "obligation_signal")}))
    check("ignored narrative cannot change selection units", stripped["candidates"] == pool["candidates"])

    # Mirror the production loader's inventory: source_files_old includes local
    # headers, while Kbuild source_files contains only translation units.
    with_header = deepcopy(docs)
    local_header = "drivers/demo/local.h"
    module = with_header["module_source.json"]
    module["source_files_old"].append(local_header)
    module["headers_old"] = [local_header]
    module["kbuild_target_resolution"]["headers"] = [local_header]
    module["kbuild_target_resolution"]["files"].append(local_header)
    header_sources = {**sources, local_header: "#define LOCAL_CONSTANT 1\n"}
    local_pool = build_kernel_evidence_pool(documents=with_header, old_sources=header_sources)
    check("production local-header inventory admitted", local_pool["candidates"] == pool["candidates"])
    for label in ("header missing from loaded files", "header also classified as source", "duplicate loaded path", "duplicate resolved header", "unresolved header closure"):
        bad = deepcopy(with_header)
        module = bad["module_source.json"]
        if label == "header missing from loaded files":
            module["source_files_old"].remove(local_header)
        elif label == "header also classified as source":
            module["kbuild_target_resolution"]["source_files"].append(local_header)
        elif label == "duplicate loaded path":
            module["source_files_old"].append(local_header)
        elif label == "duplicate resolved header":
            module["kbuild_target_resolution"]["headers"].append(local_header)
        else:
            module["kbuild_target_resolution"]["warnings"] = ["unresolved local include"]
        try:
            build_kernel_evidence_pool(documents=bad, old_sources=header_sources)
        except EvidenceBoundaryError:
            passed.append(label + " rejected")
        else:
            raise AssertionError(label)

    def reject(name, mutate):
        data = deepcopy(docs)
        mutate(data)
        try:
            build(data)
        except EvidenceBoundaryError:
            passed.append(name)
        else:
            raise AssertionError(name)

    def location_path(data, value):
        data["kernel_definition_locations.json"]["items"][0]["new_locations"][0]["path"] = value

    for path in ("drivers/demo/demo.c", "drivers/other/other.c", "include/../drivers/demo/demo.c", "/tmp/kernel.h", "include//linux/demo.h", "arch/x86/drivers/other.c"):
        reject("forbidden or noncanonical kernel path " + path, lambda data, path=path: location_path(data, path))
    reject("unsupported document", lambda data: data.update({"reference.diff": {}}))
    reject("unsupported annotation", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"reference_patch": "gold"}))
    reject("unsupported module metadata", lambda data: data["module_source.json"].update({"new_source": "gold"}))
    reject("commit evidence", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"commits": [{"subject": "gold"}]}))
    reject("semantic table", lambda data: data["kernel_definition_diffs.json"].update({"semantic_obligation_table": {"path": "gold"}}))
    reject("commit mode", lambda data: data["kernel_definition_diffs.json"].update({"collect_commit_info": True}))
    reject("driver diff injected into definition diff", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"definition_diff": "--- a/drivers/demo/demo.c\n+++ b/drivers/demo/demo.c"}))
    reject("truncated source excerpt", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"old_definition": "1: int api_a(void);\n... truncated"}))
    reject("sibling driver usage", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"used_by_driver": [{"file": "drivers/other/other.c", "line": 1}]}))
    reject("new source mislabeled old usage", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"used_by_driver": [{"file": "drivers/demo/demo.c", "line": 1, "code": "replacement()"}]}))
    reject("changed flag without source difference", lambda data: data["kernel_definition_diffs.json"]["items"][1].update({"definition_changed": True}))
    reject("mutable source ref", lambda data: data["module_source.json"].update({"new_ref": "master"}))
    reject("forbidden discarded helper path", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"kernel_side_helper_diff_paths": ["drivers/demo/demo.c"]}))
    reject("foreign replacement annotation", lambda data: data["kernel_definition_diffs.json"]["items"][0].update({"replacement_candidates": [{"reference_diff": "gold"}]}))
    reject("unresolved local headers", lambda data: data["module_source.json"].update({"local_include_scope_risks": ["missing header"]}))
    reject("unknown function signal", lambda data: data["kernel_definition_diffs.json"]["items"][0]["used_by_driver"][0].update({"enclosing_function": "handcrafted_hint"}))
    reject("unsupported nested Kbuild metadata", lambda data: data["module_source.json"]["kbuild_target_resolution"].update({"reference_diff": "gold"}))
    reject("Kbuild sibling source", lambda data: data["module_source.json"].update({"kbuild_target_resolution": {"module_path": "drivers/demo", "source_files": ["drivers/other/other.c"]}}))
    conflict = deepcopy(docs)
    conflicting = deepcopy(conflict["kernel_definition_diffs.json"]["items"][0])
    conflicting["used_by_driver"] = []
    conflict["kernel_definition_diffs.json"]["items"].append(conflicting)
    try:
        build(conflict)
    except EvidenceBoundaryError:
        passed.append("conflicting duplicate row rejected")
    else:
        raise AssertionError("conflicting duplicate row")

    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git = lambda *args: subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git("init", "-q")
        git("config", "user.name", "Evidence test")
        git("config", "user.email", "evidence@example.invalid")
        (repo / "drivers/demo").mkdir(parents=True)
        (repo / "include/linux").mkdir(parents=True)
        (repo / "drivers/demo/demo.c").write_text(sources["drivers/demo/demo.c"])
        header = repo / "include/linux/demo.h"
        header.write_text("int api_a(void);\n\nint api_b(void);\n")
        git("add", ".")
        git("commit", "-qm", "old")
        actual = deepcopy(docs)
        actual["module_source.json"]["old_ref"] = git("rev-parse", "HEAD")
        header.write_text("long api_a(void);\n\nint api_b(void);\n")
        git("add", ".")
        git("commit", "-qm", "new")
        actual["module_source.json"]["new_ref"] = git("rev-parse", "HEAD")
        verified = build_kernel_evidence_pool(documents=actual, old_sources=sources, source_reader=git_source_reader(repo))
        check("live Git provenance verification", verified["audit"]["source_verification"] == "git_blob_lines_verified")
        check("independent Git verification", verify_pool_against_git(verified, repo)["status"] == "PASS")
        tampered = deepcopy(verified)
        tampered["candidates"][0]["text"] += "\nreplacement hint"
        try:
            verify_pool_against_git(tampered, repo)
        except EvidenceBoundaryError:
            passed.append("post-build candidate tampering rejected")
        else:
            raise AssertionError("post-build tampering")
        bad = deepcopy(actual)
        bad["kernel_definition_diffs.json"]["items"][0]["old_definition"] = "1: int api_a(int fake);"
        try:
            build_kernel_evidence_pool(documents=bad, old_sources=sources, source_reader=git_source_reader(repo))
        except EvidenceBoundaryError:
            passed.append("source mismatch under permitted path rejected")
        else:
            raise AssertionError("source mismatch")
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    checks = run_checks()
    result = {"status": "PASS", "check_count": len(checks), "checks": checks}
    if args.extracted:
        pool = build_kernel_evidence_pool(args.extracted, source_reader=git_source_reader(args.repo) if args.repo else None)
        rows = pool["candidates"]
        overlap = set(pool["audit"]["obligation_symbols"])
        overlapping = [row for row in rows if set(row["symbols"]) & overlap]
        result["real_extraction"] = {
            "candidate_count": len(rows), "candidate_chars_min": min(row["chars"] for row in rows),
            "candidate_chars_max": max(row["chars"] for row in rows),
            "candidate_chars_total": sum(row["chars"] for row in rows),
            "obligation_symbols": sorted(overlap),
            "overlapping_candidates": [{"id": row["id"], "symbols": row["symbols"], "chars": row["chars"]} for row in overlapping],
            "other_candidate_count": len(rows) - len(overlapping),
            "pool_sha256": pool["audit"]["pool_sha256"], "source_verification": pool["audit"]["source_verification"],
            "eligibility": "multiple optional candidates exist; actual shared request budget must establish competition separately",
        }
        if args.repo:
            result["git_verification"] = verify_pool_against_git(pool, args.repo)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
