#!/usr/bin/env python3
"""Production v3 identity-edit contract checks on unrelated synthetic sources."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver_agent_pregen.exact_edits import (
    MAX_EDITS, compile_edits, compile_identity_edits, compile_substring_edits,
)


def edit(path, old, new):
    return {"path": path, "old_text": old, "new_text": new}


def output(edits):
    return {"edits": edits, "rationale": "Generic synthetic identity contract fixture", "notes": []}


def rejects(call, reason=None):
    try:
        call()
    except ValueError as exc:
        if reason:
            assert reason in str(exc), str(exc)
        return
    raise AssertionError("expected explicit ValueError refusal")


def apply_and_verify(sources, compiled):
    assert compiled["status"] == "compiled"
    assert compiled["patch_sha256"] == hashlib.sha256(compiled["patch_diff"].encode("utf-8")).hexdigest()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory(prefix="identity-contract-apply-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        for source in sources:
            path = root / source["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source["content"].encode("utf-8"))
        for flags in (["--check"], []):
            subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", *flags, "-"], cwd=root,
                           input=compiled["patch_diff"].encode("utf-8"), env=env, check=True, capture_output=True)
        for source in compiled["patched_sources"]:
            assert (root / source["path"]).read_bytes() == source["content"].encode("utf-8")


def legacy_fields(compiled):
    result = copy.deepcopy(compiled)
    for key in ("submitted_edits", "submitted_count", "changed_count", "identity_count"):
        result.pop(key)
    for audit in result["edits_audit"]:
        audit.pop("submitted_index")
        audit.pop("effect")
    return result


def main():
    path = "drivers/fixture/alpha.c"
    zeta = "drivers/fixture/zeta.c"
    sources = [{"path": zeta, "content": "int retained = 8;\n"},
               {"path": path, "content": "/* λ cafe\u0301 */\nint fixture_ab = 7;\n"}]
    submitted = output([edit(zeta, "retained", "retained"), edit(path, "ab = 7", "xy = 9"),
                        edit(path, "fixture_", "fixture_"), edit(path, "\u0301", "\u0300")])
    frozen = copy.deepcopy([sources, submitted])
    compiled = compile_identity_edits(sources, submitted)
    assert compiled["status"] == "compiled"
    assert (compiled["submitted_count"], compiled["changed_count"], compiled["identity_count"]) == (4, 2, 2)
    assert compiled["submitted_edits"] == submitted["edits"]
    assert [a["submitted_index"] for a in compiled["edits_audit"]] == [3, 2, 1, 0]
    assert [a["effect"] for a in compiled["edits_audit"]] == ["changed", "identity", "changed", "identity"]
    assert compiled["edits_audit"][1]["end_char"] == compiled["edits_audit"][2]["start_char"]
    assert compiled["patched_sources"] == [
        {"path": path, "content": "/* λ cafe\u0300 */\nint fixture_xy = 9;\n"},
        {"path": zeta, "content": "int retained = 8;\n"}]
    for audit in compiled["edits_audit"]:
        item = submitted["edits"][audit["submitted_index"]]
        source = next(s["content"] for s in sources if s["path"] == item["path"])
        assert source[audit["start_char"]:audit["end_char"]] == item["old_text"]
        assert source.encode("utf-8")[audit["start_byte"]:audit["end_byte"]] == item["old_text"].encode("utf-8")
        assert audit["old_text_sha256"] == hashlib.sha256(item["old_text"].encode("utf-8")).hexdigest()
        assert audit["new_text_sha256"] == hashlib.sha256(item["new_text"].encode("utf-8")).hexdigest()
    reverse = compile_identity_edits(list(reversed(sources)), output(list(reversed(submitted["edits"]))))
    assert reverse["patch_diff"] == compiled["patch_diff"] and reverse["patched_sources"] == compiled["patched_sources"]
    apply_and_verify(sources, compiled)
    compiled["submitted_edits"][0]["new_text"] = "caller mutation"
    compiled["patched_sources"][0]["content"] = "caller mutation"
    compiled["edits_audit"][0]["path"] = "caller mutation"
    assert [sources, submitted] == frozen
    print("PASS complete list and every identity retained; original index/effect/byte audits, adjacent spans, order independence and input immutability")

    for before, old, new, expected in (
        ("int keep = 1;\n", "keep = 1", "keep = 2", "int keep = 2;\n"),
        ("int keep = 1;", "1", "2", "int keep = 2;"),
        ("int keep = 1;\n", ";\n", ";", "int keep = 1;"),
        ("int keep = 1;", "1;", "2;\n", "int keep = 2;\n"),
    ):
        fixture = [{"path": path, "content": before}]
        result = compile_identity_edits(fixture, output([edit(path, old, new), edit(path, "int", "int")]))
        assert result["patched_sources"] == [{"path": path, "content": expected}]
        apply_and_verify(fixture, result)
    deletion_sources = [{"path": path, "content": "erase\n"}, {"path": zeta, "content": "retain\n"}]
    deletion = compile_identity_edits(deletion_sources, output([edit(path, "erase\n", ""), edit(zeta, "retain", "retain")]))
    assert deletion["patched_sources"] == [{"path": path, "content": ""}, {"path": zeta, "content": "retain\n"}]
    apply_and_verify(deletion_sources, deletion)
    identities = compile_identity_edits(sources, output([edit(path, "fixture_", "fixture_"), edit(zeta, "retained", "retained")]))
    assert identities["status"] == "identity_only" and identities["changed_count"] == 0
    assert identities["identity_count"] == identities["submitted_count"] == len(identities["edits_audit"]) == 2
    assert identities["patch_diff"] == "" and identities["source_sha256"] == identities["patched_source_sha256"]
    empty = compile_identity_edits(sources, output([]))
    assert empty["status"] == "no_edits" and empty["patch_diff"] == "" and not empty["edits_audit"]
    assert empty["submitted_count"] == empty["changed_count"] == empty["identity_count"] == 0
    print("PASS real git-apply bytes for preserved/changed terminal LF and deletion; identity-only and no-edits produce no patch")

    fixture = [{"path": path, "content": "abcdef\n"}]
    for edits, reason in (
        ([edit(path, "abc", "new"), edit(path, "bcde", "bcde")], "overlap"),
        ([edit(path, "abc", "abc"), edit(path, "abc", "abc")], "overlap"),
        ([edit(path, "abc", "abc"), edit(path, "abc", "new")], "overlap"),
        ([edit(path, "abc", "created"), edit(path, "created", "created")], "no exact match"),
        ([edit(path, "", "")], "nonempty"),
    ):
        rejects(lambda: compile_identity_edits(fixture, output(edits)), reason)
    for original, old in (("ababa\n", "aba"), ("same same\n", "same")):
        rejects(lambda: compile_identity_edits([{"path": path, "content": original}], output([edit(path, old, old)])), "ambiguous")
    rejects(lambda: compile_identity_edits([{"path": path, "content": "abZ"}], output([
        edit(path, "a", "ab"), edit(path, "b", ""), edit(path, "Z", "Z")])), "no source change")
    rejects(lambda: compile_identity_edits([{"path": path, "content": "cafe\u0301"}], output([edit(path, "café", "café")])), "no exact match")
    for invalid in (None, [], {**submitted, "extra": True}, {**submitted, "notes": [1]},
                    {**submitted, "edits": [submitted["edits"][0]] * (MAX_EDITS + 1)},
                    {**submitted, "edits": [{**submitted["edits"][0], "line": 1}]}):
        rejects(lambda: compile_identity_edits(sources, invalid))
    for unsafe in ("../escape.c", "/tmp/escape.c", ".git/config", "a//b.c", "a\\b.c", "a b.c", "other.c"):
        rejects(lambda: compile_identity_edits(sources, output([edit(unsafe, "retained", "retained")])))
    for invalid in (True, None, "\x00", "\r", "\x1b", "\x7f", "\ud800"):
        rejects(lambda: compile_identity_edits(sources, output([edit(path, invalid, invalid)])))
    print("PASS identity-inclusive ambiguity/overlap and all source/schema/path/text bounds; no normalized or sequential matches and net-zero changes reject")

    full_lines = [{"path": path, "content": "int first = 1;\nint second = 2;\n"}]
    for edits in ([], [edit(path, "int first = 1;\n", "long first = 1;\n")],
                  [edit(path, "int first = 1;\n", ""), edit(path, "int second = 2;\n", "int final = 3;")]):
        value = output(edits)
        first, second = compile_edits(full_lines, value), compile_substring_edits(full_lines, value)
        assert first == second == legacy_fields(compile_identity_edits(full_lines, value))
    partial = output([edit(path, "first", "initial")])
    assert compile_substring_edits(full_lines, partial) == legacy_fields(compile_identity_edits(full_lines, partial))
    rejects(lambda: compile_edits(full_lines, partial), "complete original lines")
    identity_line = output([edit(path, "int first = 1;\n", "int first = 1;\n")])
    for compiler in (compile_edits, compile_substring_edits):
        rejects(lambda: compiler(full_lines, identity_line), "no-op")
        assert not any(key in compiler(full_lines, output([])) for key in ("submitted_edits", "submitted_count", "changed_count", "identity_count"))
    assert compile_identity_edits(full_lines, identity_line)["status"] == "identity_only"
    print("PASS unchanged v1/v2 legacy result keys, full-line/subsequence output parity and old no-op refusals")
    print(json.dumps({"status": "PASS", "check_groups": 4, "real_provider_requests": 0,
                      "fixture_scope": "generic production identity contract, no prior driver responses"}))


if __name__ == "__main__":
    main()
