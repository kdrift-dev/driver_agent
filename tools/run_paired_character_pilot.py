#!/usr/bin/env python3
"""Run an explicitly bounded character-budget diagnostic, never a token-budget trial.

Without --execute this freezes requests only, without reading credentials.
The historical strict token-admission flag remains false in every artifact.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver_agent_pregen.llm_client import LLMClient, LLMConfig, OpenRouterProvider, _read_kv_like_config_key
from driver_agent_pregen.paired_context import INSTRUCTIONS, MessageCharCounter, POLICIES, digest, prepare_pair
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity

PROTOCOL = "paired_character_generation_diagnostic_v1"
MODEL = "anthropic/claude-sonnet-4.6"
CANONICAL_MODEL = "anthropic/claude-4.6-sonnet-20260217"
INDEX_SHA256 = "6a83a8933bbe06ef9f2fd08462f7d3d24bcd8fdd0df6db666382d315a7adc638"
MAX_TOKENS = 4096
WIRE_OPTIONS = {
    "provider": {"only": ["anthropic"], "order": ["anthropic"], "allow_fallbacks": False, "require_parameters": True},
    "reasoning": {"enabled": False},
    "plugins": [{"id": "context-compression", "enabled": False}],
}
EXPECTED_FILES = {"summary.json", "evidence_pool.json", "old_sources.json"} | {
    f"{policy}/{name}" for policy in POLICIES
    for name in ("request_body.json", "input.json", "prompt.md", "selection_audit.json")}


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def read_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def safe_relative(name):
    return isinstance(name, str) and bool(name) and not name.startswith("/") and "\\" not in name and all(
        p not in ("", ".", "..") for p in name.split("/"))


def snapshot_prepared(root):
    """Read immutable canonical inputs once, validating every indexed byte."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("prepared directory must not be a symlink")
    root = root.resolve(strict=True)
    names = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("prepared artifacts contain a symlink")
        if path.is_file():
            names.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("nonregular prepared artifact")
    if names != EXPECTED_FILES | {"artifact_index.json"}:
        raise ValueError("unexpected or missing prepared artifact paths")
    raw = {name: (root / name).read_bytes() for name in sorted(names)}
    if sha_bytes(raw["artifact_index.json"]) != INDEX_SHA256:
        raise ValueError("canonical character preparation index anchor mismatch")
    index = read_json(raw["artifact_index.json"])
    if set(index) != {"protocol", "files", "file_count"} or index["protocol"] != "paired_context_artifacts_v1":
        raise ValueError("invalid prepared index schema")
    if set(index["files"]) != EXPECTED_FILES or index["file_count"] != 11:
        raise ValueError("invalid prepared file inventory")
    for name, expected in index["files"].items():
        if not safe_relative(name) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("unsafe index path or hash")
        if sha_bytes(raw[name]) != expected:
            raise ValueError("prepared artifact hash mismatch")
    return raw


def reproduce(raw):
    summary = read_json(raw["summary.json"])
    pool = read_json(raw["evidence_pool.json"])
    sources = read_json(raw["old_sources.json"])
    if summary["model"] != MODEL or summary["max_output_tokens"] != MAX_TOKENS:
        raise ValueError("unexpected frozen model/output cap")
    if summary["source_mode"] != "full_module" or summary["source_verification"] != "git_blob_lines_verified":
        raise ValueError("complete verified old source is required")
    if summary["counter"] != MessageCharCounter.identity or summary["generation_calls"] != 0:
        raise ValueError("only the no-inference character preparation is admitted")
    if summary["budget_rule"] != "mandatory + ceil(max(0, full_pool - mandatory) / 4)":
        raise ValueError("unexpected budget rule")
    # JSON artifact writers sort keys; prompt bytes preserve original insertion
    # order. Recover only that order from the hash-anchored original prompt.
    prompt = raw["first_fit/prompt.md"].decode("utf-8")
    if not prompt.startswith(INSTRUCTIONS):
        raise ValueError("unexpected frozen instruction prefix")
    ordered = read_json(prompt[len(INSTRUCTIONS):])
    if ordered["old_module_sources"] != sources or ordered["migration_requirements"] != pool["obligation_signal"]:
        raise ValueError("frozen prompt source/requirement values differ")
    sources = ordered["old_module_sources"]
    requirements = ordered["migration_requirements"]
    pair = prepare_pair(sources, requirements, pool["candidates"],
                        counter=MessageCharCounter(), model=MODEL, max_tokens=MAX_TOKENS)
    for key, value in pair.items():
        if key != "conditions" and summary.get(key) != value:
            raise ValueError(f"reproduced preparation summary differs: {key}")
    if not pair["selection_pressure_eligible"]:
        raise ValueError("selection pressure is required")
    for policy in POLICIES:
        condition = pair["conditions"][policy]
        audit = {k: v for k, v in condition.items() if k not in {"request_body", "input_payload", "prompt"}}
        if condition["generation_ready"] is not False or condition["dispatch_refusal"] != "counter_provider_binding_unverified":
            raise ValueError("strict generation refusal must be preserved")
        if condition["status"] != "within_budget":
            raise ValueError("character budget exceeded")
        expected = {"request_body.json": condition["request_body"], "input.json": condition["input_payload"],
                    "selection_audit.json": audit}
        for name, value in expected.items():
            if read_json(raw[f"{policy}/{name}"]) != value:
                raise ValueError(f"reproduced condition artifact differs: {policy}/{name}")
        if raw[f"{policy}/prompt.md"].decode("utf-8") != condition["prompt"] or summary["conditions"][policy] != audit:
            raise ValueError("prompt or summary selection changed")
    return pair, summary, sources


def wire_body(body):
    result = copy.deepcopy(body)
    if set(result) != {"model", "messages", "temperature", "max_tokens"}:
        raise ValueError("unexpected base request fields")
    if result["model"] != MODEL or result["max_tokens"] != MAX_TOKENS or result["temperature"] != 0:
        raise ValueError("unexpected base request settings")
    result.update(copy.deepcopy(WIRE_OPTIONS))
    return result


class DiagnosticProvider(OpenRouterProvider):
    def request_body(self, prompt, max_tokens=None):
        return wire_body(super().request_body(prompt, max_tokens))


def build_client(config_ini):
    # This function is invoked only after a complete freeze and --execute.
    key = _read_kv_like_config_key(Path(config_ini), "OPEN_ROUTER_KEY")
    if not key:
        raise ValueError("explicit config does not contain OPEN_ROUTER_KEY")
    config = LLMConfig(provider="openrouter", model=MODEL, enabled=False,
                       timeout_sec=300, max_retries=0, max_tokens=MAX_TOKENS)
    client = LLMClient(config)
    client._provider = DiagnosticProvider(model=MODEL, api_key=key, timeout_sec=300,
                                          max_retries=0, max_tokens=MAX_TOKENS)
    return client


def validate_patch(parsed, sources):
    if not isinstance(parsed, dict) or set(parsed) != {"patch_candidates", "module_level_notes", "parse_status"}:
        raise ValueError("response must contain only the requested schema")
    if parsed["parse_status"] != "ok":
        raise ValueError("response was not parsed successfully")
    notes = parsed["module_level_notes"]
    candidates = parsed["patch_candidates"]
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        raise ValueError("module notes must be strings")
    if not isinstance(candidates, list) or len(candidates) > 1:
        raise ValueError("at most one patch candidate is admitted")
    if not candidates:
        return None
    candidate = candidates[0]
    if not isinstance(candidate, dict) or set(candidate) != {"candidate_id", "rationale", "patch_diff"} or any(
            not isinstance(candidate[k], str) for k in candidate):
        raise ValueError("invalid patch candidate schema")
    if not candidate["candidate_id"].strip():
        raise ValueError("empty candidate id")
    patch = candidate["patch_diff"]
    if not patch.strip():
        return None
    source_map = {item["path"]: item["content"] for item in sources}
    old_paths, new_paths = [], []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/") or parts[2][2:] != parts[3][2:] or parts[2][2:] not in source_map:
                raise ValueError("git diff header is outside the supplied old source")
        elif line.startswith("--- "):
            path = line[4:]
            if not path.startswith("a/") or path[2:] not in source_map:
                raise ValueError("patch old path is outside the supplied old source")
            old_paths.append(path[2:])
        elif line.startswith("+++ "):
            path = line[4:]
            if not path.startswith("b/") or path[2:] not in source_map:
                raise ValueError("patch new path is outside the supplied old source")
            new_paths.append(path[2:])
        elif line.startswith(("GIT binary patch", "Binary files ", "rename ", "copy ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")):
            raise ValueError("only text edits to existing source files are admitted")
    if not old_paths or old_paths != new_paths or len(set(old_paths)) != len(old_paths):
        raise ValueError("patch path headers must match uniquely")
    # Application and build validity belong to the independent validator.
    # Preserve the model's diff bytes even when its hunk counts are invalid.
    if not any(line.startswith("@@ ") for line in patch.splitlines()):
        raise ValueError("patch has no unified diff hunk")
    return patch


def inspect_response(audit, parsed, sources, expected_wire, raw_output):
    attempts = audit.get("attempts", [])
    final = attempts[-1] if attempts else {}
    result = {"request_status": audit.get("request_status"), "parse_status": parsed.get("parse_status"),
              "response_truncated": audit.get("response_truncated"), "attempts": attempts,
              "request_attempt_count": audit.get("request_attempt_count"),
              "request_attempt_count_exact": audit.get("request_attempt_count_exact"),
              "request_attempt_count_bounds": audit.get("request_attempt_count_bounds"),
              "attempt_count_unit": "explicit_http_or_sdk_invocation; provider internal retries unknown",
              "returned_model": final.get("returned_model"), "returned_provider": final.get("returned_provider"),
              "finish_reason": final.get("finish_reason"), "usage": final.get("usage"),
              "candidate_admission": "rejected", "generation_ready": False,
              "actual_token_budget_comparison": False}
    response = final.get("response")
    choices = response.get("choices") if isinstance(response, dict) else None
    result["native_finish_reason"] = choices[0].get("native_finish_reason") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    auth_quota = any(a.get("http_status") in {401, 402, 403, 429} or
        (isinstance(a.get("response"), dict) and isinstance(a["response"].get("error"), dict) and
         a["response"]["error"].get("code") in {401, 402, 403, 429}) for a in attempts)
    result["stop_pair_for_auth_or_quota"] = auth_quota
    reasons = []
    if audit.get("request_body") != expected_wire:
        reasons.append("actual_request_body_mismatch")
    if audit.get("request_status") != "succeeded" or audit.get("response_truncated") is not False:
        reasons.append("request_not_successful_or_truncated")
    if audit.get("request_attempt_count_exact") is not True or audit.get("request_attempt_count") != 1 or len(attempts) != 1:
        reasons.append("request_attempt_count_not_exactly_one")
    if final.get("finish_reason") != "stop":
        reasons.append("finish_reason_not_stop")
    if final.get("returned_model") not in {MODEL, CANONICAL_MODEL}:
        reasons.append("returned_model_mismatch")
    if final.get("returned_provider") not in {"Anthropic", "anthropic"}:
        reasons.append("returned_provider_mismatch")
    patch = None
    if not reasons:
        try:
            strict_parsed = read_json(raw_output)
            if not isinstance(strict_parsed, dict) or set(strict_parsed) != {"patch_candidates", "module_level_notes"}:
                raise ValueError("raw response must be exactly one requested JSON object")
            strict_parsed["parse_status"] = "ok"
            if parsed != strict_parsed:
                raise ValueError("parsed response differs from strict raw JSON")
            patch = validate_patch(strict_parsed, sources)
            result["candidate_admission"] = "admitted" if patch is not None else (
                "no_candidate" if not strict_parsed["patch_candidates"] else "empty_patch")
        except (ValueError, OSError) as exc:
            reasons.append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    result["rejection_reasons"] = reasons
    return result, patch


def run(args, *, client_factory=None):
    if args.execute and not args.config_ini:
        raise ValueError("--execute requires an explicit --config-ini")
    out = Path(args.out_dir).absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("refusing to reuse an output directory, including partial runs")
    code = code_identity()
    if args.execute and code["tracked_worktree_dirty"]:
        raise ValueError("commit executable changes before an executed diagnostic")
    raw = snapshot_prepared(args.prepared_dir)
    pair, prepared_summary, sources = reproduce(raw)
    wires = {p: wire_body(pair["conditions"][p]["request_body"]) for p in POLICIES}
    input_hashes = {p: sha_bytes(b) for p, b in raw.items()}
    if code_identity() != code or snapshot_prepared(args.prepared_dir) != raw:
        raise ValueError("code or prepared inputs changed during admission")
    out.mkdir(parents=True, exist_ok=False)
    freeze = out / "freeze"
    freeze_files = {}
    for policy in POLICIES:
        condition = pair["conditions"][policy]
        for name, value in (("request_body.json", wires[policy]), ("input.json", condition["input_payload"])):
            file = freeze / policy / name
            write_json(file, value)
            freeze_files[file.relative_to(freeze).as_posix()] = sha_bytes(file.read_bytes())
        file = freeze / policy / "prompt.md"
        file.write_text(condition["prompt"], encoding="utf-8")
        freeze_files[file.relative_to(freeze).as_posix()] = sha_bytes(file.read_bytes())
    manifest = {"protocol": PROTOCOL, "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
                "code": code, "prepared_directory": str(Path(args.prepared_dir).resolve()),
                "prepared_files_sha256": input_hashes, "freeze_files_sha256": freeze_files,
                "strict_preparation_protocol": pair["protocol"], "generation_ready": False,
                "strict_dispatch_refusal": "counter_provider_binding_unverified",
                "diagnostic_admission": "character_budget_only", "actual_token_budget_comparison": False,
                "model": MODEL, "canonical_model": CANONICAL_MODEL, "source_mode": "full_module",
                "old_ref": prepared_summary["old_ref"], "new_ref": prepared_summary["new_ref"],
                "module_path": prepared_summary["module_path"], "kbuild_target": prepared_summary["kbuild_target"],
                "source_sha256": pair["source_sha256"], "pool_sha256": pair["pool_sha256"],
                "requirements_sha256": pair["requirements_sha256"], "counter": pair["counter"],
                "common_character_budget": pair["common_input_budget"], "wire_options": WIRE_OPTIONS,
                "max_output_tokens": MAX_TOKENS, "max_retries": 0, "call_limit": 2, "condition_order": list(POLICIES),
                "claim_scope": "exploratory one-case diagnostic; equal character cap, not equal actual token budget",
                "provider_internal_retries": "unknown", "wire_option_documentation": [
                    "https://openrouter.ai/docs/guides/routing/provider-selection",
                    "https://openrouter.ai/docs/guides/best-practices/reasoning-tokens",
                    "https://openrouter.ai/docs/guides/features/message-transforms"],
                "conditions": {p: {"wire_request_sha256": digest(wires[p]),
                    "base_request_sha256": pair["conditions"][p]["request_sha256"],
                    "messages_sha256": digest(wires[p]["messages"]),
                    "input_characters": MessageCharCounter().count(wires[p]),
                    "selected_ids": pair["conditions"][p]["selected_ids"],
                    "generation_ready": False} for p in POLICIES}}
    write_json(freeze / "manifest.json", manifest)
    manifest_raw = (freeze / "manifest.json").read_bytes()
    frozen_digest = sha_bytes(manifest_raw)
    print(json.dumps({"event": "frozen_before_provider_construction", "freeze_sha256": frozen_digest,
                      "output": str(out), "execute": args.execute}), flush=True)

    def verify_freeze():
        if code_identity() != code or snapshot_prepared(args.prepared_dir) != raw:
            raise ValueError("code or canonical inputs changed after freeze")
        if (freeze / "manifest.json").is_symlink() or (freeze / "manifest.json").read_bytes() != manifest_raw:
            raise ValueError("freeze manifest changed")
        inventory = set()
        for path in freeze.rglob("*"):
            if path.is_symlink():
                raise ValueError("freeze contains a symlink")
            if path.is_file():
                inventory.add(path.relative_to(freeze).as_posix())
        if inventory != set(freeze_files) | {"manifest.json"}:
            raise ValueError("freeze file inventory changed")
        for name, expected in freeze_files.items():
            if sha_bytes((freeze / name).read_bytes()) != expected:
                raise ValueError("frozen wire request changed")
        for policy in POLICIES:
            if digest(wires[policy]) != manifest["conditions"][policy]["wire_request_sha256"]:
                raise ValueError("in-memory wire request changed")
        print(json.dumps({"event": "freeze_verified", "freeze_sha256": frozen_digest}), flush=True)

    report = {"protocol": PROTOCOL, "freeze_sha256": frozen_digest, "generation_ready": False,
              "actual_token_budget_comparison": False, "status": "prepared_only", "llm_calls": 0,
              "runtime_tests": 0, "conditions": {}, "explicit_http_attempts": 0,
              "explicit_http_attempts_exact": True, "provider_internal_retries": "unknown"}
    try:
        verify_freeze()
        if args.execute:
            client = (client_factory or build_client)(args.config_ini)
            if not isinstance(client._provider, DiagnosticProvider) or client.config.max_retries != 0 or client._provider.max_retries != 0:
                raise ValueError("diagnostic requires the pinned provider and zero explicit retries")
            report["status"] = "executed"
            for policy in POLICIES:
                verify_freeze()
                if report["llm_calls"] >= 2:
                    raise ValueError("two-call diagnostic limit reached")
                condition = pair["conditions"][policy]
                if client._provider.request_body(condition["prompt"], MAX_TOKENS) != wires[policy]:
                    raise ValueError("provider body differs from frozen wire request")
                print(json.dumps({"event": "dispatch", "condition": policy, "ordinal": report["llm_calls"] + 1,
                                  "freeze_sha256": frozen_digest, "wire_request_sha256": digest(wires[policy])}), flush=True)
                stage = out / "generation" / policy
                report["llm_calls"] += 1
                write_json(stage / "dispatch_intent.json", {
                    "ordinal": report["llm_calls"], "condition": policy, "freeze_sha256": frozen_digest,
                    "wire_request_sha256": digest(wires[policy]), "http_attempt_count": None,
                    "status": "call_about_to_start; no automatic resume or retry"})
                parsed = client.call_json(stage_dir=stage, prompt_text=condition["prompt"],
                                          input_payload=condition["input_payload"], dry_run=False,
                                          default_output={"patch_candidates": [], "module_level_notes": []}, max_tokens=MAX_TOKENS)
                audit = read_json((stage / "provider_audit.json").read_bytes())
                if read_json((stage / "request_body.json").read_bytes()) != wires[policy]:
                    raise ValueError("saved actual wire request changed")
                result, patch = inspect_response(audit, parsed, sources, wires[policy], (stage / "raw_output.txt").read_text(encoding="utf-8"))
                if patch is not None:
                    (stage / "generated.diff").write_text(patch, encoding="utf-8")
                    (stage / "parsed_diff.patch").write_text(patch, encoding="utf-8")
                    result["parsed_diff_sha256"] = sha_bytes(patch.encode("utf-8"))
                write_json(stage / "diagnostic_result.json", result)
                report["conditions"][policy] = result
                exact = result["request_attempt_count_exact"] is True
                report["explicit_http_attempts_exact"] &= exact
                if exact and report["explicit_http_attempts"] is not None:
                    report["explicit_http_attempts"] += result["request_attempt_count"]
                else:
                    report["explicit_http_attempts"] = None
                write_json(out / f"progress_after_{report['llm_calls']}.json", report)
                verify_freeze()
                if result["stop_pair_for_auth_or_quota"]:
                    report["status"] = "stopped_auth_or_quota"
                    break
        verify_freeze()
    except BaseException as exc:
        report["status"] = "aborted_without_retry"
        report["failure_type"] = type(exc).__name__
        if report["llm_calls"] > len(report["conditions"]):
            report["explicit_http_attempts"] = None
            report["explicit_http_attempts_exact"] = False
        write_json(out / "summary.json", report)
        raise
    write_json(out / "summary.json", report)
    files = {p.relative_to(out).as_posix(): sha_bytes(p.read_bytes()) for p in sorted(out.rglob("*")) if p.is_file()}
    write_json(out / "artifact_index.json", {"protocol": PROTOCOL, "files": files, "file_count": len(files)})
    print(json.dumps({"event": "completed", "status": report["status"], "llm_calls": report["llm_calls"],
                      "freeze_sha256": frozen_digest, "conditions": {p: r["candidate_admission"] for p, r in report["conditions"].items()}}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--config-ini", type=Path, help="read only with --execute; no environment fallback")
    parser.add_argument("--execute", action="store_true", help="send at most two frozen character-budget diagnostic requests")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
