#!/usr/bin/env python3
"""Exact-substring v2 checks on unrelated synthetic fixtures; no model calls."""
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
from driver_agent_pregen.exact_edits import MAX_EDITS, compile_edits, compile_substring_edits


def edit(path, old, new):
    return {"path": path, "old_text": old, "new_text": new}


def response(edits):
    return {"edits": edits, "rationale": "Unrelated synthetic substring fixture", "notes": []}


def rejects(call, reason=None):
    try:
        call()
    except ValueError as exc:
        if reason:
            assert reason in str(exc), str(exc)
        return
    raise AssertionError("expected explicit ValueError refusal")


def apply_and_verify(sources, result):
    assert result["status"] == "compiled"
    assert result["patch_sha256"] == hashlib.sha256(result["patch_diff"].encode("utf-8")).hexdigest()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory(prefix="exact-substring-apply-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        for source in sources:
            file = root / source["path"]
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(source["content"].encode("utf-8"))
        patch = result["patch_diff"].encode("utf-8")
        for flags in (["--check"], []):
            subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", *flags, "-"],
                           cwd=root, input=patch, check=True, capture_output=True, env=env)
        for source in result["patched_sources"]:
            assert (root / source["path"]).read_bytes() == source["content"].encode("utf-8")


def main():
    path = "drivers/fixture/alpha.c"
    zeta = "drivers/fixture/zeta.c"
    before = "/* λ café cafe\u0301 \u2028 */\nint fixture_ab = 7;\nconst char *label = \"untouched\";\n"
    sources = [{"path": zeta, "content": "int unrelated = 11;\n"}, {"path": path, "content": before}]
    edits = [edit(path, "ab = 7", "xy = 9"), edit(path, "fixture_", "sample_"),
             edit(path, "\u0301", "\u0300"), edit(zeta, "11", "12")]
    output = response(edits)
    frozen = copy.deepcopy([sources, output])
    result = compile_substring_edits(sources, output)
    expected = "/* λ café cafe\u0300 \u2028 */\nint sample_xy = 9;\nconst char *label = \"untouched\";\n"
    assert result["patched_sources"] == [{"path": path, "content": expected}, {"path": zeta, "content": "int unrelated = 12;\n"}]
    assert result == compile_substring_edits(list(reversed(sources)), response(list(reversed(edits))))
    assert [sources, output] == frozen
    audits = [audit for audit in result["edits_audit"] if audit["path"] == path]
    assert audits[1]["end_char"] == audits[2]["start_char"]  # Adjacent original spans.
    assert all(audit["start_byte"] > audit["start_char"] for audit in audits)
    for audit in result["edits_audit"]:
        original = next(source["content"] for source in sources if source["path"] == audit["path"])
        old_slice = original[audit["start_char"]:audit["end_char"]]
        assert original.encode("utf-8")[audit["start_byte"]:audit["end_byte"]] == old_slice.encode("utf-8")
        assert audit["old_text_sha256"] == hashlib.sha256(old_slice.encode("utf-8")).hexdigest()
    apply_and_verify(sources, result)
    result["patched_sources"][0]["content"] = "caller mutation"
    result["edits_audit"][0]["path"] = "caller mutation"
    assert [sources, output] == frozen
    print("PASS exact Unicode and combining-mark spans, adjacent same-line edits, original byte offsets, order independence and immutable inputs")

    cases = [
        ("int number = 5;\n", "int number = 5;", "int number = 6;", "int number = 6;\n"),
        ("int number = 5;", "5", "6", "int number = 6;"),
        ("int number = 5;", "5;", "6;\n", "int number = 6;\n"),
        ("int number = 5;\n", ";\n", ";", "int number = 5;"),
        ("left\nright\n", "\n", " ", None),  # Ambiguous newline must not be guessed.
        ("left\nright", "\n", " ", "left right"),
        ("left right\n", " ", "\n", "left\nright\n"),
        ("head alpha\nbeta tail\n", "alpha\nbeta", "omega\ntheta", "head omega\ntheta tail\n"),
        ("int discard = 5;\n", "discard = 5", "", "int ;\n"),
        ("int discard = 5;\n", "int discard = 5;\n", "", ""),
        ("int discard = 5;", "int discard = 5;", "", ""),
    ]
    for original, old, new, expected in cases:
        fixture = [{"path": path, "content": original}]
        output = response([edit(path, old, new)])
        if expected is None:
            rejects(lambda: compile_substring_edits(fixture, output), "ambiguous")
            continue
        compiled = compile_substring_edits(fixture, output)
        assert compiled["patched_sources"] == [{"path": path, "content": expected}]
        apply_and_verify(fixture, compiled)
        if (original and not original.endswith("\n")) or (expected and not expected.endswith("\n")):
            assert "\\ No newline at end of file\n" in compiled["patch_diff"]
    print("PASS LF omission preserves untouched LF, explicit newline insertion/deletion, multiline partial spans and empty/unterminated files via git apply")

    fixture = [{"path": path, "content": "prefix_abc_suffix\nother = 3;\n"}]
    for edits, reason in [
        ([edit(path, "", "new")], "nonempty"),
        ([edit(path, "abc", "abc")], "no-op"),
        ([edit(path, "missing", "new")], "no exact match"),
        ([edit(path, "abc", "new"), edit(path, "bc_suffix", "other")], "overlap"),
        ([edit(path, "abc", "new"), edit(path, "abc", "other")], "overlap"),
        ([edit(path, "abc", "created"), edit(path, "created", "new")], "no exact match"),
        ([edit(path, "abc", "new"), edit(path, "other", "other")], "no-op"),
    ]:
        rejects(lambda: compile_substring_edits(fixture, response(edits)), reason)
    rejects(lambda: compile_substring_edits([{"path": path, "content": "ababa\n"}],
                                           response([edit(path, "aba", "x")])), "ambiguous")
    rejects(lambda: compile_substring_edits([{"path": path, "content": "abc abc\n"}],
                                           response([edit(path, "abc", "x")])), "ambiguous")
    rejects(lambda: compile_substring_edits([{"path": path, "content": "/* cafe\u0301 */\n"}],
                                           response([edit(path, "café", "other")])), "no exact match")
    rejects(lambda: compile_substring_edits([{"path": path, "content": "int\tvalue = 2;\n"}],
                                           response([edit(path, "int value", "long value")])), "no exact match")
    print("PASS no fuzzy whitespace/Unicode repair, overlapping-aware ambiguity, disjoint original spans, sequential-match and no-op refusals")

    full_line_sources = [{"path": path, "content": "int first = 1;\nint second = 2;\n"}]
    for edits in ([], [edit(path, "int first = 1;\n", "long first = 1;\n")],
                  [edit(path, "int first = 1;\n", ""), edit(path, "int second = 2;\n", "int final = 3;")]):
        assert compile_edits(full_line_sources, response(edits)) == compile_substring_edits(full_line_sources, response(edits))
    substring = response([edit(path, "first = 1", "changed = 4")])
    rejects(lambda: compile_edits(full_line_sources, substring), "complete original lines")
    assert compile_substring_edits(full_line_sources, substring)["status"] == "compiled"
    no_lf = response([edit(path, "int first = 1;\n", "int first = 4;")])
    rejects(lambda: compile_edits(full_line_sources, no_lf), "end with LF")
    merged = compile_substring_edits(full_line_sources, no_lf)
    assert merged["patched_sources"][0]["content"] == "int first = 4;int second = 2;\n"
    apply_and_verify(full_line_sources, merged)
    valid = response([edit(path, "first", "initial")])
    for invalid in (None, [], {**valid, "extra": True}, {**valid, "notes": [1]},
                    {**valid, "edits": [valid["edits"][0]] * (MAX_EDITS + 1)},
                    {**valid, "edits": [{**valid["edits"][0], "line": 1}]}):
        rejects(lambda: compile_substring_edits(full_line_sources, invalid))
    for unsafe in ("../escape.c", "/outside.c", "a//b.c", ".git/config", "a\\b.c", "a b.c", "drivers/fixture/unknown.c"):
        rejects(lambda: compile_substring_edits(full_line_sources, response([edit(unsafe, "first", "new")])))
    for invalid in (True, None, "\x00", "\r", "\x1b", "\x7f", "\ud800"):
        rejects(lambda: compile_substring_edits(full_line_sources, response([edit(path, "first", invalid)])))
        rejects(lambda: compile_substring_edits(full_line_sources, response([edit(path, invalid, "new")])))
    empty = compile_substring_edits(full_line_sources, response([]))
    assert empty["status"] == "no_edits" and empty["patch_diff"] == "" and not empty["edits_audit"]
    assert empty["source_sha256"] == empty["patched_source_sha256"]
    print("PASS v1 full-line output parity and unchanged refusals, strict v2 schema/path/control bounds, and explicit no-edits status")
    print(json.dumps({"status": "PASS", "check_groups": 4, "real_provider_requests": 0,
                      "fixture_scope": "generic exact-substring compiler; deterministic git-apply byte roundtrips"}))


if __name__ == "__main__":
    main()
