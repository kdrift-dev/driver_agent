#!/usr/bin/env python3
"""Offline exact-edit contract checks on unrelated synthetic source fixtures."""
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
from driver_agent_pregen.exact_edits import MAX_EDITS, RESPONSE_SCHEMA, compile_edits


def response(edits):
    return {"edits": edits, "rationale": "Fixture migration", "notes": []}


def edit(path, old, new):
    return {"path": path, "old_text": old, "new_text": new}


def rejects(call, message=None):
    try:
        call()
    except ValueError as exc:
        if message:
            assert message in str(exc), str(exc)
        return
    raise AssertionError("expected explicit ValueError refusal")


def apply_and_verify(sources, result):
    assert result["status"] == "compiled"
    assert result["patch_sha256"] == hashlib.sha256(result["patch_diff"].encode("utf-8")).hexdigest()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory(prefix="exact-edit-check-") as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        for source in sources:
            path = root / source["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source["content"].encode("utf-8"))
        patch = result["patch_diff"].encode("utf-8")
        for args in (["--check"], []):
            subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", *args, "-"],
                           input=patch, cwd=root, check=True, capture_output=True, env=env)
        for source in result["patched_sources"]:
            assert (root / source["path"]).read_bytes() == source["content"].encode("utf-8")


def main():
    path = "drivers/fixture/alpha.c"
    sources = [
        {"path": "drivers/fixture/zeta.c", "content": "int zeta(void) { return 5; }\n"},
        {"path": path, "content": "/* café λ\u2028: LF only */\nint width = 2;\nint mode = 3;\nint stable = 7;\n"},
    ]
    edits = [edit(path, "int mode = 3;\n", "int mode = 4;\n"),
             edit("drivers/fixture/zeta.c", sources[0]["content"], "int zeta(void) { return 6; }\n"),
             edit(path, "int width = 2;\n", "long width = 2;\n")]
    output = response(edits)
    original = copy.deepcopy([sources, output])
    result = compile_edits(sources, output)
    assert [sources, output] == original
    assert result == compile_edits(list(reversed(sources)), response(list(reversed(edits))))
    assert result["patched_sources"][0]["path"] == path
    assert result["edits_audit"][0]["start_line"] == result["edits_audit"][0]["end_line"] == 2
    assert result["edits_audit"][0]["start_byte"] > result["edits_audit"][0]["start_char"]
    assert result["patched_sources"][0]["content"] == "/* café λ\u2028: LF only */\nlong width = 2;\nint mode = 4;\nint stable = 7;\n"
    apply_and_verify(sources, result)
    result["patched_sources"][0]["content"] = "caller mutation"
    result["edits_audit"][0]["path"] = "caller mutation"
    assert [sources, output] == original
    empty = compile_edits(sources, response([]))
    assert empty["status"] == "no_edits" and empty["patch_diff"] == "" and empty["edits_audit"] == []
    assert empty["source_sha256"] == empty["patched_source_sha256"]
    encoded = json.dumps(sorted(sources, key=lambda s: s["path"]), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert empty["source_sha256"] == hashlib.sha256(encoded).hexdigest()
    print("PASS simultaneous original-source edits, canonical ordering, UTF-8 byte preservation, immutable inputs and source/diff hashes")

    for before, old, new, after in [
        ("int a = 1;\nint b = 2;", "int b = 2;", "int b = 3;", "int a = 1;\nint b = 3;"),
        ("int a = 1;\nint b = 2;", "int b = 2;", "int b = 3;\n", "int a = 1;\nint b = 3;\n"),
        ("int a = 1;\n", "int a = 1;\n", "int a = 2;", "int a = 2;"),
        ("int a = 1;\nint b = 2;\n", "int a = 1;\n", "", "int b = 2;\n"),
        ("int a = 1;\nint b = 2;", "int b = 2;", "", "int a = 1;\n"),
        ("int a = 1;\n", "int a = 1;\n", "", ""),
        ("int a = 1;", "int a = 1;", "", ""),
        ("int a = 1;\nint b = 2;", "int a = 1;\n", "int a = 4;\n", "int a = 4;\nint b = 2;"),
    ]:
        fixture = [{"path": path, "content": before}]
        compiled = compile_edits(fixture, response([edit(path, old, new)]))
        assert compiled["patched_sources"] == [{"path": path, "content": after}]
        apply_and_verify(fixture, compiled)
        if (before and not before.endswith("\n")) or (after and not after.endswith("\n")):
            assert "\\ No newline at end of file\n" in compiled["patch_diff"]
    print("PASS generated diffs apply byte-exactly with deletion, empty files, terminal LF changes and unterminated context lines")

    fixture = [{"path": path, "content": "one\ntwo\nthree\n"}]
    for invalid_edits, reason in [
        ([edit(path, "absent\n", "new\n")], "no exact match"),
        ([edit(path, "one", "new")], "complete original lines"),
        ([edit(path, "ne\n", "new\n")], "complete original lines"),
        ([edit(path, "one\n", "new")], "end with LF"),
        ([edit(path, "", "new\n")], "nonempty"),
        ([edit(path, "one\n", "one\n")], "no-op"),
        ([edit(path, "one\ntwo\n", "new\n"), edit(path, "two\n", "other\n")], "overlap"),
        ([edit(path, "one\n", "new\n"), edit(path, "one\n", "other\n")], "overlap"),
        ([edit(path, "one\n", "created\n"), edit(path, "created\n", "new\n")], "no exact match"),
    ]:
        rejects(lambda: compile_edits(fixture, response(invalid_edits)), reason)
    rejects(lambda: compile_edits([{"path": path, "content": "same\nsame\n"}],
                                 response([edit(path, "same\n", "new\n")])), "ambiguous")
    # Two occurrences overlap: str.count would incorrectly report only one.
    rejects(lambda: compile_edits([{"path": path, "content": "a\na\na\n"}],
                                 response([edit(path, "a\na\n", "new\n")])), "ambiguous")
    print("PASS ambiguous and overlapping matches, partial lines, overlapping spans, sequential-match attempts and no-op refusals")

    valid = response([edit(path, "one\n", "new\n")])
    malformed = [None, [], True, {**valid, "patch_diff": "forbidden"},
                 {"edits": valid["edits"], "rationale": "missing notes"},
                 {**valid, "edits": {}}, {**valid, "edits": [None]},
                 {**valid, "edits": [{**valid["edits"][0], "line": 1}]},
                 {**valid, "edits": [valid["edits"][0]] * (MAX_EDITS + 1)},
                 {**valid, "rationale": False}, {**valid, "notes": "text"},
                 {**valid, "notes": [1]}]
    for invalid in malformed:
        rejects(lambda: compile_edits(fixture, invalid))
    for invalid_path in ("../outside.c", "/tmp/outside.c", "drivers//a.c", "./a.c", "a/../b.c",
                         "a/./b.c", "a\\b.c", "a b.c", "a\nb.c", "a\tb.c", ".git/config", "a/.git/x", "C:/x.c", "x;cmd.c", ""):
        rejects(lambda: compile_edits(fixture, response([edit(invalid_path, "one\n", "new\n")])))
        rejects(lambda: compile_edits([{"path": invalid_path, "content": "one\n"}], response([])))
    rejects(lambda: compile_edits(fixture, response([edit("drivers/fixture/outside.c", "one\n", "new\n")])), "outside")
    for value in (None, True, 1, [], "bad\x00text", "bad\r\ntext", "bad\x1btext", "bad\x7ftext", "bad\ud800text"):
        for field in ("old_text", "new_text"):
            invalid = copy.deepcopy(valid)
            invalid["edits"][0][field] = value
            rejects(lambda: compile_edits(fixture, invalid))
        rejects(lambda: compile_edits([{"path": path, "content": value}], response([])))
    for invalid_sources in (None, [], fixture + fixture, [{"path": path}], [{**fixture[0], "reference": "forbidden"}], [None]):
        rejects(lambda: compile_edits(invalid_sources, response([])))
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    assert RESPONSE_SCHEMA["properties"]["edits"]["items"]["additionalProperties"] is False
    assert set(RESPONSE_SCHEMA["required"]) == {"edits", "rationale", "notes"}
    assert "minLength" not in json.dumps(RESPONSE_SCHEMA) and "maxItems" not in json.dumps(RESPONSE_SCHEMA)
    print("PASS strict response/source schema, edit bound, path containment, controls and malformed UTF-8")


if __name__ == "__main__":
    main()
