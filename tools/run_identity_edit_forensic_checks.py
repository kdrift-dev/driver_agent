#!/usr/bin/env python3
"""Generic offline identity-operation checks; no historical-response fixtures."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import prepare_identity_edit_forensics as forensic


def response(edits):
    return {"edits": edits, "rationale": "Generic synthetic forensic fixture", "notes": []}


def edit(path, old, new):
    return {"path": path, "old_text": old, "new_text": new}


def rejects(call, reason=None):
    try:
        call()
    except ValueError as exc:
        if reason:
            assert reason in str(exc), str(exc)
        return
    raise AssertionError("expected explicit refusal")


def apply_and_verify(sources, result):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory(prefix="identity-forensic-apply-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        for source in sources:
            file = root / source["path"]
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(source["content"].encode("utf-8"))
        for flags in (["--check"], []):
            subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", *flags, "-"],
                           input=result["patch_diff"].encode("utf-8"), cwd=root, env=env,
                           check=True, capture_output=True)
        for source in result["patched_sources"]:
            assert (root / source["path"]).read_bytes() == source["content"].encode("utf-8")


def synthetic_generation(root, sources, output):
    """Create a completely synthetic 31-file generation fixture, then pin it."""
    write = forensic.write_json
    protocol = forensic.pilot.SUBSTRING_PROTOCOL
    frozen_hashes, conditions = {}, {}
    for policy in forensic.POLICIES:
        payload, prompt, wire = forensic.pilot.render(sources, [], [], model=forensic.pilot.MODEL,
                                                    max_tokens=forensic.pilot.MAX_TOKENS,
                                                    edit_contract=forensic.CONTRACT)
        frozen = root / "freeze" / policy
        write(frozen / "input.json", payload)
        write(frozen / "request_body.json", wire)
        write(frozen / "selection_audit.json", {"fixture": True})
        (frozen / "prompt.md").write_text(prompt, encoding="utf-8")
        raw_output = json.dumps(output, ensure_ascii=False)
        provider_response = {"model": forensic.pilot.MODEL, "provider": "Anthropic",
                             "choices": [{"finish_reason": "stop", "native_finish_reason": "end_turn",
                                          "message": {"role": "assistant", "content": raw_output}}]}
        attempt = {"returned_model": forensic.pilot.MODEL, "returned_provider": "Anthropic",
                   "finish_reason": "stop", "http_status": 200,
                   "response_raw": json.dumps(provider_response, ensure_ascii=False), "response": provider_response}
        audit = {"request_body": wire, "request_status": "succeeded", "parse_status": "ok",
                 "response_truncated": False, "request_attempt_count": 1,
                 "request_attempt_count_exact": True, "request_attempt_count_bounds": {"lower": 1, "upper": 1},
                 "configured_max_retries": 0, "attempts": [attempt]}
        strict, compiled = forensic.pilot.inspect(audit, raw_output, sources, wire, forensic.CONTRACT)
        assert compiled is None and strict["rejection_reasons"] == ["invalid_structured_edits: no-op edit is forbidden"]
        stage = root / "generation" / policy
        write(stage / "diagnostic_result.json", strict)
        write(stage / "dispatch_intent.json", {"fixture": True})
        write(stage / "input.json", payload)
        write(stage / "parsed_output.json", {"fixture": "ignored by materializer"})
        write(stage / "provider_audit.json", audit)
        write(stage / "request_body.json", wire)
        (stage / "prompt.md").write_text(prompt, encoding="utf-8")
        (stage / "raw_output.txt").write_text(raw_output, encoding="utf-8")
        conditions[policy] = strict
    write(root / "freeze/response_schema.json", forensic.pilot.RESPONSE_SCHEMA)
    write(root / "freeze/selection_summary.json", {"fixture": True})
    frozen_hashes = {name: forensic.sha(raw) for name, raw in forensic.file_snapshot(root / "freeze").items()}
    manifest = {"protocol": protocol, "edit_contract": forensic.CONTRACT,
                "code": {"commit": forensic.GENERATION_COMMIT, "tracked_worktree_dirty": False},
                "condition_order": list(forensic.POLICIES), "generation_ready": False,
                "source_sha256": forensic.digest(sources), "requirements_sha256": forensic.digest([]),
                "files_sha256": frozen_hashes}
    write(root / "freeze/manifest.json", manifest)
    freeze_hash = forensic.sha((root / "freeze/manifest.json").read_bytes())
    summary = {"protocol": protocol, "edit_contract": forensic.CONTRACT, "freeze_sha256": freeze_hash,
               "status": "executed", "llm_calls": 2, "explicit_client_attempts": 2,
               "explicit_client_attempts_exact": True, "conditions": conditions}
    for name in ("summary.json", "progress_after_1.json", "progress_after_2.json"):
        write(root / name, summary)
    raw = forensic.file_snapshot(root)
    assert len(raw) == 30
    write(root / "artifact_index.json", {"protocol": protocol, "file_count": len(raw),
                                       "files": {name: forensic.sha(data) for name, data in raw.items()}})
    return forensic.sha((root / "artifact_index.json").read_bytes()), freeze_hash


def main():
    path = "drivers/fixture/test.c"
    sources = [{"path": path, "content": "/* λ cafe\u0301 */\nint marker = 1;\nint stable = 7;\n"}]
    output = response([edit(path, "marker = 1", "marker = 2"), edit(path, "stable = 7", "stable = 7")])
    frozen = copy.deepcopy([sources, output])
    result = forensic.interpret(sources, output)
    assert (result["submitted_count"], result["changed_count"], result["identity_count"]) == (2, 1, 1)
    assert result["status"] == "materialized" and result["evaluation_only"] is True
    assert result["production_admission"] == "not_performed"
    assert result["submitted_output"] == output and result["submitted_edits"] == output["edits"]
    assert [audit["effect"] for audit in result["edits_audit"]] == ["changed", "identity"]
    assert [audit["submitted_index"] for audit in result["edits_audit"]] == [0, 1]
    assert result["patched_sources"][0]["content"] == "/* λ cafe\u0301 */\nint marker = 2;\nint stable = 7;\n"
    assert all(a["start_byte"] > a["start_char"] for a in result["edits_audit"])
    reverse = forensic.interpret(sources, response(list(reversed(output["edits"]))))
    assert reverse["patch_diff"] == result["patch_diff"] and reverse["patched_sources"] == result["patched_sources"]
    assert [audit["submitted_index"] for audit in reverse["edits_audit"]] == [1, 0]
    apply_and_verify(sources, result)
    result["submitted_edits"][0]["new_text"] = "caller mutation"
    result["patched_sources"][0]["content"] = "caller mutation"
    assert [sources, output] == frozen
    rejects(lambda: forensic.pilot.compile_substring_edits(sources, output), "no-op")
    print("PASS entire submitted list and identity audit retained; exact changed source, original offsets, immutable input and unchanged production refusal")

    identity = forensic.interpret(sources, response([edit(path, "stable = 7", "stable = 7")]))
    assert identity["status"] == "identity_only" and identity["changed_count"] == 0 and identity["identity_count"] == 1
    assert identity["patch_diff"] == "" and identity["source_sha256"] == identity["patched_source_sha256"]
    empty = forensic.interpret(sources, response([]))
    assert empty["status"] == "no_edits" and empty["submitted_count"] == 0
    for before, old, new, expected in (
        ("/* e\u0301 */\nint keep = 1;\n", "\u0301", "\u0300", "/* e\u0300 */\nint keep = 1;\n"),
        ("int keep = 1;\nint end = 2;", "end = 2", "end = 3", "int keep = 1;\nint end = 3;"),
        ("int keep = 1;\n", ";\n", ";", "int keep = 1;"),
        ("int keep = 1;", "1;", "2;\n", "int keep = 2;\n"),
    ):
        fixture = [{"path": path, "content": before}]
        interpreted = forensic.interpret(fixture, response([edit(path, old, new), edit(path, "keep", "keep")]))
        assert interpreted["patched_sources"][0]["content"] == expected
        apply_and_verify(fixture, interpreted)
    other = "drivers/fixture/other.c"
    fixture = [{"path": path, "content": "erase\n"}, {"path": other, "content": "retain\n"}]
    deletion = forensic.interpret(fixture, response([edit(path, "erase\n", ""), edit(other, "retain", "retain")]))
    assert {s["path"]: s["content"] for s in deletion["patched_sources"]} == {path: "", other: "retain\n"}
    apply_and_verify(fixture, deletion)
    print("PASS identity-only/no-edits distinct states, combining text and final-LF preservation/change, plus full-file deletion")

    fixture = [{"path": path, "content": "abcdef\n"}]
    for edits, reason in (
        ([edit(path, "abc", "new"), edit(path, "bcde", "bcde")], "overlapping"),
        ([edit(path, "abc", "abc"), edit(path, "abc", "abc")], "overlapping"),
        ([edit(path, "abc", "abc"), edit(path, "abc", "new")], "overlapping"),
        ([edit(path, "abc", "created"), edit(path, "created", "created")], "does not match"),
        ([edit(path, "", "")], "nonempty"),
    ):
        rejects(lambda: forensic.interpret(fixture, response(edits)), reason)
    for source, old in (("ababa\n", "aba"), ("same same\n", "same")):
        rejects(lambda: forensic.interpret([{"path": path, "content": source}], response([edit(path, old, old)])), "ambiguous")
    rejects(lambda: forensic.interpret([{"path": path, "content": "ab"}], response([edit(path, "a", "ab"), edit(path, "b", "")])), "no source change")
    rejects(lambda: forensic.interpret([{"path": path, "content": "cafe\u0301"}], response([edit(path, "café", "café")])), "does not match")
    for invalid in (None, [], {**output, "extra": True}, {**output, "notes": [1]},
                    {**output, "edits": [output["edits"][0]] * 17},
                    {**output, "edits": [{**output["edits"][0], "line": 1}]}):
        rejects(lambda: forensic.interpret(sources, invalid))
    for unsafe in ("../outside.c", "/tmp/outside.c", ".git/config", "a//b.c", "a\\b.c", "a b.c", "other.c"):
        rejects(lambda: forensic.interpret(sources, response([edit(unsafe, "int", "int")])))
    for invalid in (None, True, "\x00", "\r", "\x7f", "\ud800"):
        rejects(lambda: forensic.interpret(sources, response([edit(path, invalid, invalid)])))
    rejects(lambda: forensic.read_json('{"edits":[],"edits":[]}'), "duplicate")
    print("PASS identity-inclusive overlap/uniqueness and all strict schema/path/UTF-8 controls; no normalization or silent filtering")

    with tempfile.TemporaryDirectory(prefix="identity-forensic-cli-") as directory:
        root = Path(directory)
        generation = root / "synthetic-generation"
        index_sha, freeze_sha = synthetic_generation(generation, sources, output)
        code = {"commit": "synthetic-forensic-code", "tracked_worktree_dirty": False,
                "source_digest": "synthetic-forensic-digest", "source_files": {},
                "protocol_document": {"path": forensic.PROTOCOL_DOCUMENT,
                                      "sha256": forensic.sha((Path(__file__).resolve().parents[1] / forensic.PROTOCOL_DOCUMENT).read_bytes())}}
        with patch.object(forensic, "GENERATION_INDEX_SHA256", index_sha), patch.object(forensic, "GENERATION_FREEZE_SHA256", freeze_sha), patch.object(forensic, "committed_code", return_value=code), patch.object(forensic.pilot, "build_client", side_effect=AssertionError("forensics must never call model")):
            original = forensic.snapshot_generation(generation)
            args = SimpleNamespace(generation_dir=generation, out_dir=root / "forensics")
            with redirect_stdout(io.StringIO()):
                report = forensic.run(args)
            assert report["new_model_calls"] == report["new_build_calls"] == 0
            assert report["production_admission_unchanged"] is True
            assert forensic.snapshot_generation(generation) == original
            manifest = forensic.read_json((args.out_dir / "freeze/manifest.json").read_bytes())
            assert manifest["policy_selected_after_observing_outputs"] is True
            assert manifest["protocol_document"] == code["protocol_document"]
            assert forensic.sha((args.out_dir / "freeze/protocol.md").read_bytes()) == code["protocol_document"]["sha256"]
            assert manifest["original_generation_index_sha256"] == index_sha
            for name, raw in original.items():
                assert (args.out_dir / "freeze/generation_input" / name).read_bytes() == raw
            for policy in forensic.POLICIES:
                stage = args.out_dir / "conditions" / policy
                interpreted = forensic.read_json((stage / "interpretation.json").read_bytes())
                assert interpreted["canonical_strict_admission"] == "rejected"
                assert interpreted["submitted_output"] == output
                assert (stage / "raw_output.txt").read_bytes() == original[f"generation/{policy}/raw_output.txt"]
                assert interpreted["raw_output_sha256"] == forensic.sha((stage / "raw_output.txt").read_bytes())
                assert interpreted["patch_sha256"] == forensic.sha((stage / "generated.diff").read_bytes())
            index = forensic.read_json((args.out_dir / "artifact_index.json").read_bytes())
            assert index["file_count"] == len(index["files"])
            assert all(forensic.sha((args.out_dir / name).read_bytes()) == value for name, value in index["files"].items())
            rejects(lambda: forensic.run(args), "existing")
            (generation / "extra.txt").write_text("unexpected")
            rejects(lambda: forensic.snapshot_generation(generation), "inventory")
            (generation / "extra.txt").unlink()
            changed = generation / "generation/first_fit/raw_output.txt"
            saved = changed.read_bytes()
            changed.write_bytes(saved + b" ")
            rejects(lambda: forensic.snapshot_generation(generation), "hash")
            changed.write_bytes(saved)
            changed.unlink()
            changed.symlink_to(root / "missing")
            rejects(lambda: forensic.snapshot_generation(generation), "symlink")
            changed.unlink()
            changed.write_bytes(saved)
            # Intact source index is insufficient if provider content is not
            # bound: test the semantic guard directly on copied synthetic bytes.
            altered = copy.deepcopy(original)
            audit = forensic.read_json(altered["generation/first_fit/provider_audit.json"])
            audit["attempts"][0]["response"]["choices"][0]["message"]["content"] += " "
            altered["generation/first_fit/provider_audit.json"] = json.dumps(audit).encode()
            rejects(lambda: forensic.validate_generation(altered), "binding")
            original_interpret = forensic.interpret
            def mutate_frozen_input(*values):
                result = original_interpret(*values)
                frozen_source = root / "mutated/freeze/old_sources.json"
                frozen_source.write_bytes(frozen_source.read_bytes() + b" ")
                return result
            with patch.object(forensic, "interpret", side_effect=mutate_frozen_input):
                rejects(lambda: forensic.run(SimpleNamespace(generation_dir=generation, out_dir=root / "mutated")), "changed")
            aborted = forensic.read_json((root / "mutated/summary.json").read_bytes())
            assert aborted["status"] == "aborted_without_retry"
            assert not (root / "mutated/conditions/obligation_ranked").exists()
    print("PASS synthetic pinned CLI freeze, exact raw/source copies, original rejection retention, indexed outputs, tamper detection and zero model/build calls")
    print(json.dumps({"status": "PASS", "check_groups": 4, "real_provider_requests": 0,
                      "real_generation_materializations": 0, "fixture_scope": "unrelated synthetic exact edit lists and synthetic pinned generation"}))


if __name__ == "__main__":
    main()
