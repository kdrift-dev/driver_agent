#!/usr/bin/env python3
"""Evaluation-only materialization of a pinned rejected v2 edit list.

No model calls, builds or changes to production admission. Identity operations
are validated and applied alongside every other submitted operation.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver_agent_pregen.exact_edits import _diff
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity
import run_structured_edit_pilot as pilot

PROTOCOL = "identity_edit_forensics_v1"
PROTOCOL_DOCUMENT = "docs-impl/23_identity_edit_forensic_protocol.md"
GENERATION_INDEX_SHA256 = "32bb2f2ab1495d45b05e81ea1bb4f409a22afefb60135135ce7a0fd926f92cb7"
GENERATION_FREEZE_SHA256 = "f857319b43d210ce3c39f29b02f2a434c59cfb4c8bdb4fd1d8d2264d4decfb42"
GENERATION_COMMIT = "0e5ed41062c4973a5f0106a6c0d0dd667b5dde29"
CONTRACT = "exact_substrings_v2"
POLICIES = ("first_fit", "obligation_ranked")
MAX_EDITS = 16


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha(canonical(value).encode("utf-8"))


def read_json(raw):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def fields(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError(f"invalid {label} fields")


def text(value):
    if type(value) is not str:
        raise ValueError("text must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("text is not valid UTF-8") from exc
    if any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in value):
        raise ValueError("forbidden control character")
    return value


def relative(value):
    text(value)
    if (not re.fullmatch(r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*", value)
            or any(p in (".", "..", ".git") for p in value.split("/"))):
        raise ValueError("unsafe relative path")
    return value


def interpret(sources, output):
    """Pure forensic interpretation; never production candidate admission.

    All submitted spans, including identity operations, must uniquely match the
    immutable original and be disjoint. No operation is filtered or repaired.
    """
    if type(sources) is not list or not sources:
        raise ValueError("nonempty source list required")
    originals = {}
    for source in sources:
        fields(source, ("path", "content"), "source")
        path = relative(source["path"])
        if path in originals:
            raise ValueError("duplicate source path")
        originals[path] = text(source["content"])
    fields(output, ("edits", "rationale", "notes"), "response")
    text(output["rationale"])
    if type(output["notes"]) is not list:
        raise ValueError("notes must be a list")
    for note in output["notes"]:
        text(note)
    edits = output["edits"]
    if type(edits) is not list or len(edits) > MAX_EDITS:
        raise ValueError("at most 16 edits required")
    spans = {path: [] for path in originals}
    for submitted_index, edit in enumerate(edits):
        fields(edit, ("path", "old_text", "new_text"), "edit")
        path = relative(edit["path"])
        if path not in originals:
            raise ValueError("edit outside supplied sources")
        old, new = text(edit["old_text"]), text(edit["new_text"])
        if not old:
            raise ValueError("old_text must be nonempty")
        original = originals[path]
        start = original.find(old)
        if start < 0:
            raise ValueError("old_text does not match original")
        if original.find(old, start + 1) >= 0:
            raise ValueError("ambiguous original match, including overlapping occurrences")
        spans[path].append((start, start + len(old), submitted_index, old, new))
    old_sources = [{"path": path, "content": originals[path]} for path in sorted(originals)]
    patched_sources, audits, diffs = [], [], []
    for source in old_sources:
        path, original = source["path"], source["content"]
        cursor, pieces = 0, []
        for start, end, submitted_index, old, new in sorted(spans[path]):
            if start < cursor:
                raise ValueError("overlapping original spans, including identity operations")
            pieces.extend((original[cursor:start], new))
            audits.append({"submitted_index": submitted_index, "path": path,
                           "effect": "identity" if old == new else "changed",
                           "start_char": start, "end_char": end,
                           "start_byte": len(original[:start].encode("utf-8")),
                           "end_byte": len(original[:end].encode("utf-8")),
                           "start_line": original.count("\n", 0, start) + 1,
                           "end_line": original.count("\n", 0, end) + (0 if old.endswith("\n") else 1),
                           "old_text": old, "new_text": new,
                           "old_text_sha256": sha(old.encode("utf-8")),
                           "new_text_sha256": sha(new.encode("utf-8"))})
            cursor = end
        pieces.append(original[cursor:])
        patched = "".join(pieces)
        patched_sources.append({"path": path, "content": patched})
        if original != patched:
            diffs.append(_diff(path, original, patched))
    patch = "".join(diffs)
    changed = sum(a["effect"] == "changed" for a in audits)
    if changed and not patch:
        raise ValueError("combined changed edits produce no source change")
    return {"protocol": PROTOCOL, "evaluation_only": True,
            "production_admission": "not_performed",
            "status": "materialized" if changed else ("identity_only" if edits else "no_edits"),
            "submitted_count": len(edits), "changed_count": changed,
            "identity_count": len(edits) - changed,
            "submitted_edits": copy.deepcopy(edits), "submitted_output": copy.deepcopy(output),
            "submitted_output_sha256": digest(output), "edits_audit": audits,
            "patched_sources": patched_sources, "patch_diff": patch,
            "source_sha256": digest(old_sources), "patched_source_sha256": digest(patched_sources),
            "patch_sha256": sha(patch.encode("utf-8"))}


def file_snapshot(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("source directory is a symlink")
    root = root.resolve(strict=True)
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("source inventory contains a symlink")
        if path.is_file():
            name = relative(path.relative_to(root).as_posix())
            files[name] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("source inventory contains a nonregular entry")
    return files


def snapshot_generation(root):
    raw = file_snapshot(root)
    if "artifact_index.json" not in raw or sha(raw["artifact_index.json"]) != GENERATION_INDEX_SHA256:
        raise ValueError("canonical generation index anchor mismatch")
    index = read_json(raw["artifact_index.json"])
    fields(index, ("protocol", "files", "file_count"), "generation index")
    if index["protocol"] != pilot.SUBSTRING_PROTOCOL or type(index["files"]) is not dict:
        raise ValueError("unexpected generation index protocol")
    if type(index["file_count"]) is not int or index["file_count"] != 30 or len(index["files"]) != 30:
        raise ValueError("unexpected canonical generation file count")
    if set(raw) != set(index["files"]) | {"artifact_index.json"}:
        raise ValueError("canonical generation inventory changed")
    for name, expected in index["files"].items():
        relative(name)
        if type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha(raw[name]) != expected:
            raise ValueError("canonical generation file hash mismatch")
    if sha(raw["freeze/manifest.json"]) != GENERATION_FREEZE_SHA256:
        raise ValueError("canonical generation freeze anchor mismatch")
    return raw


def validate_generation(raw):
    """Bind raw provider responses and intact old input; do not materialize."""
    manifest = read_json(raw["freeze/manifest.json"])
    summary = read_json(raw["summary.json"])
    if (manifest["protocol"] != pilot.SUBSTRING_PROTOCOL or manifest["edit_contract"] != CONTRACT
            or manifest["code"]["commit"] != GENERATION_COMMIT or manifest["code"]["tracked_worktree_dirty"] is not False
            or manifest["condition_order"] != list(POLICIES) or manifest["generation_ready"] is not False):
        raise ValueError("unexpected canonical generation identity")
    if (summary["protocol"] != manifest["protocol"] or summary["edit_contract"] != CONTRACT
            or summary["freeze_sha256"] != GENERATION_FREEZE_SHA256 or summary["status"] != "executed"
            or summary["llm_calls"] != 2 or summary["explicit_client_attempts"] != 2
            or summary["explicit_client_attempts_exact"] is not True):
        raise ValueError("unexpected canonical generation summary")
    expected_freeze = {name[len("freeze/"):] for name in raw if name.startswith("freeze/")} - {"manifest.json"}
    if set(manifest["files_sha256"]) != expected_freeze or any(sha(raw["freeze/" + name]) != value for name, value in manifest["files_sha256"].items()):
        raise ValueError("original freeze manifest inventory or hash mismatch")
    sources, arms = None, {}
    for policy in POLICIES:
        prefix = "generation/" + policy + "/"
        frozen = "freeze/" + policy + "/"
        payload = read_json(raw[frozen + "input.json"])
        fields(payload, ("old_module_sources", "migration_requirements", "kernel_evidence"), "frozen payload")
        candidate_sources = payload["old_module_sources"]
        if type(candidate_sources) is not list or len(candidate_sources) != 1 or digest(candidate_sources) != manifest["source_sha256"]:
            raise ValueError("sole original source binding mismatch")
        if sources is None:
            sources = candidate_sources
        elif sources != candidate_sources:
            raise ValueError("condition sources differ")
        if digest(payload["migration_requirements"]) != manifest["requirements_sha256"]:
            raise ValueError("requirements binding mismatch")
        wire = read_json(raw[frozen + "request_body.json"])
        prompt = raw[frozen + "prompt.md"].decode("utf-8")
        expected_wire = pilot.structured_wire(pilot.build_chat_request(pilot.MODEL, prompt, pilot.MAX_TOKENS), CONTRACT)
        if (not prompt.startswith(pilot.SUBSTRING_INSTRUCTIONS)
                or read_json(prompt[len(pilot.SUBSTRING_INSTRUCTIONS):]) != payload or wire != expected_wire
                or read_json(raw[prefix + "request_body.json"]) != wire
                or read_json(raw[prefix + "input.json"]) != payload or raw[prefix + "prompt.md"] != raw[frozen + "prompt.md"]):
            raise ValueError("frozen input/request binding mismatch")
        audit = read_json(raw[prefix + "provider_audit.json"])
        raw_output = raw[prefix + "raw_output.txt"]
        strict_result, compiled = pilot.inspect(audit, raw_output.decode("utf-8"), sources, wire, CONTRACT)
        if (compiled is not None or strict_result["candidate_admission"] != "rejected"
                or strict_result["rejection_reasons"] != ["invalid_structured_edits: no-op edit is forbidden"]
                or strict_result != read_json(raw[prefix + "diagnostic_result.json"])
                or strict_result != summary["conditions"][policy]):
            raise ValueError("canonical strict rejection or provider binding differs")
        attempt = audit["attempts"][0]
        if (attempt["http_status"] != 200 or audit["configured_max_retries"] != 0
                or audit["request_attempt_count_bounds"] != {"lower": 1, "upper": 1}
                or read_json(attempt["response_raw"]) != attempt["response"]):
            raise ValueError("raw HTTP provider response or attempt binding differs")
        arms[policy] = {"raw_output": raw_output, "output": read_json(raw_output),
                        "strict_result": strict_result, "request_sha256": digest(wire)}
    return sources, arms, manifest


def committed_code():
    code = code_identity()
    if code["tracked_worktree_dirty"] or not code["commit"]:
        raise ValueError("commit materializer changes before forensic materialization")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    for name in ("tools/prepare_identity_edit_forensics.py", "tools/run_identity_edit_forensic_checks.py", PROTOCOL_DOCUMENT):
        committed = subprocess.run(["git", "show", "HEAD:" + name], cwd=root, env=env,
                                   capture_output=True, check=False)
        current_hash = sha((root / name).read_bytes())
        if committed.returncode or sha(committed.stdout) != current_hash:
            raise ValueError("materializer, checks and forensic protocol must be committed")
        if name != PROTOCOL_DOCUMENT and current_hash != code["source_files"].get(name):
            raise ValueError("materializer source identity differs")
        if name == PROTOCOL_DOCUMENT:
            code["protocol_document"] = {"path": name, "sha256": current_hash}
    return code


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def run(args):
    out = Path(args.out_dir).absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("refusing existing forensic output, including partial runs")
    source_root = Path(args.generation_dir).resolve(strict=True)
    if source_root in out.resolve().parents:
        raise ValueError("forensic output must be outside canonical generation")
    code = committed_code()
    raw = snapshot_generation(args.generation_dir)
    sources, arms, original_manifest = validate_generation(raw)
    protocol_raw = (Path(__file__).resolve().parents[1] / PROTOCOL_DOCUMENT).read_bytes()
    if sha(protocol_raw) != code["protocol_document"]["sha256"]:
        raise ValueError("forensic protocol changed before freeze")
    if committed_code() != code or snapshot_generation(args.generation_dir) != raw:
        raise ValueError("code or generation input changed before freeze")
    out.mkdir(parents=True, exist_ok=False)
    frozen = out / "freeze"
    for name, value in raw.items():
        file = frozen / "generation_input" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value)
    write_json(frozen / "old_sources.json", sources)
    (frozen / "protocol.md").write_bytes(protocol_raw)
    freeze_files = file_snapshot(frozen)
    manifest = {"protocol": PROTOCOL, "evaluation_only": True,
                "policy_selected_after_observing_outputs": True,
                "production_admission_unchanged": True, "new_model_calls": 0, "new_build_calls": 0,
                "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "code": code,
                "protocol_document": code["protocol_document"],
                "generation_directory": str(source_root), "original_generation_code": original_manifest["code"],
                "original_generation_index_sha256": GENERATION_INDEX_SHA256,
                "original_generation_freeze_sha256": GENERATION_FREEZE_SHA256,
                "original_source_sha256": digest(sources), "conditions": list(POLICIES),
                "files_sha256": {name: sha(value) for name, value in freeze_files.items()},
                "interpretation": "entire original raw list; exact unique disjoint original spans including identity; no normalization or filtering"}
    write_json(frozen / "manifest.json", manifest)
    frozen_raw = file_snapshot(frozen)
    freeze_hash = sha(frozen_raw["manifest.json"])

    def verify():
        if committed_code() != code or snapshot_generation(args.generation_dir) != raw or file_snapshot(frozen) != frozen_raw:
            raise ValueError("code, original input or forensic freeze changed")

    report = {"protocol": PROTOCOL, "evaluation_only": True, "status": "frozen",
              "freeze_sha256": freeze_hash, "new_model_calls": 0, "new_build_calls": 0,
              "production_admission_unchanged": True, "conditions": {}}
    try:
        verify()
        for policy in POLICIES:
            arm = arms[policy]
            interpreted = interpret(sources, arm["output"])
            interpreted.update({"raw_output_sha256": sha(arm["raw_output"]),
                                "canonical_strict_admission": "rejected",
                                "canonical_strict_rejection_reasons": arm["strict_result"]["rejection_reasons"],
                                "original_request_sha256": arm["request_sha256"], "forensic_freeze_sha256": freeze_hash})
            if (interpreted["submitted_count"], interpreted["changed_count"], interpreted["identity_count"]) != (2, 1, 1):
                raise ValueError("unexpected counts in pinned canonical response")
            stage = out / "conditions" / policy
            stage.mkdir(parents=True)
            (stage / "raw_output.txt").write_bytes(arm["raw_output"])
            write_json(stage / "submitted_output.json", arm["output"])
            write_json(stage / "interpretation.json", interpreted)
            (stage / "generated.diff").write_bytes(interpreted["patch_diff"].encode("utf-8"))
            report["conditions"][policy] = {key: interpreted[key] for key in (
                "status", "submitted_count", "changed_count", "identity_count", "raw_output_sha256",
                "source_sha256", "patched_source_sha256", "patch_sha256", "canonical_strict_admission",
                "canonical_strict_rejection_reasons")}
            verify()
        report["status"] = "materialized_for_evaluation_only"
    except BaseException as exc:
        report["status"] = "aborted_without_retry"
        report["failure_type"] = type(exc).__name__
        write_json(out / "summary.json", report)
        raise
    write_json(out / "summary.json", report)
    files = file_snapshot(out)
    write_json(out / "artifact_index.json", {"protocol": PROTOCOL, "file_count": len(files),
                                           "files": {name: sha(value) for name, value in files.items()}})
    verify()
    print(json.dumps({"status": report["status"], "new_model_calls": 0, "new_build_calls": 0,
                      "counts": {policy: [arm["submitted_count"], arm["changed_count"], arm["identity_count"]] for policy, arm in report["conditions"].items()}}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
