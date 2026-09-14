#!/usr/bin/env python3
"""Freeze a tokenizer-only, naturally over-budget context comparison census.

All source/evidence is existing evaluation case-pack content. No model weights,
inference, gate outcomes, synthetic padding, or generated distractors are used.
"""

from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.chunks import evidence_chunks, selector_case
from ctxlab.dataset import load_case
from ctxlab.metrics import PROMPT_TEMPLATE, build_prompt, render_context
from ctxlab.provenance import case_fingerprint, case_index, code_identity

PROTOCOL = "context_pressure_v1"
DEFAULT_BUDGETS = (1024, 2048, 4096)
DEFAULT_MODEL = "Qwen/Qwen2.5-Coder-0.5B"
DEFAULT_REVISION = "8123ea2e9354afb7ffcc6c8641d1b2f5ecf18301"
SPLIT_LABELS = {
    "calibration": "calibration; includes all commits represented in previously inspected manifests",
    "reserved": "reserved for future patch evaluation; previously mined; no new scoring during set construction",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class LocalTokenizer:
    """Load a pinned cached tokenizer only; never load model weights."""

    token_count_kind = "model_tokens"

    def __init__(self, model=DEFAULT_MODEL, revision=DEFAULT_REVISION):
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer

        snapshot = Path(snapshot_download(model, revision=revision, local_files_only=True))
        self.tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
        files = {}
        for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                     "vocab.json", "merges.txt", "added_tokens.json", "config.json"):
            path = snapshot / name
            if path.is_file():
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.identity = {
            "model": model, "requested_revision": revision,
            "resolved_snapshot_revision": snapshot.name,
            "class": type(self.tokenizer).__name__,
            "is_fast": getattr(self.tokenizer, "is_fast", None),
            "model_max_length": self.tokenizer.model_max_length,
            "files_sha256": files, "files_digest": digest(files),
            "packages": {name: importlib.metadata.version(name) for name in ("transformers", "tokenizers", "huggingface_hub")},
            "settings": {"add_special_tokens": False, "truncation": False,
                         "padding": False, "chat_template_applied": False, "local_files_only": True},
            "model_weights_loaded": False, "model_inference_calls": 0,
        }

    @lru_cache(maxsize=256)
    def count_tokens(self, text):
        return len(self.tokenizer(text, add_special_tokens=False, truncation=False,
                                  padding=False, verbose=False).input_ids)


def assign_split(commit, forced_commits, seed=0, calibration_basis_points=5000):
    if not commit:
        raise ValueError("a migration commit is required before assigning a split")
    value = int(hashlib.sha256(f"context-pressure-v1:{seed}:{commit}".encode()).hexdigest()[:16], 16) % 10000
    return "calibration" if commit in forced_commits or value < calibration_basis_points else "reserved"


def resolve_forced_commits(index, manifest_paths):
    metas = {cid: json.loads((path / "case.json").read_text()) for cid, path in index.items()}
    known_commits = {str(meta.get("commit") or "") for meta in metas.values()}
    if any(not re.fullmatch(r"[0-9a-f]{40}", commit) for commit in known_commits):
        raise ValueError("census migration commits must be canonical lowercase 40-character SHA-1 values")

    def resolve_commit(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{1,40}", value):
            raise ValueError(f"invalid calibration commit: {value!r}")
        matches = sorted(commit for commit in known_commits if commit.startswith(value.lower()))
        if not matches:
            raise ValueError(f"unknown calibration commit: {value}")
        if len(matches) != 1:
            raise ValueError(f"ambiguous calibration commit prefix: {value}")
        return matches[0]

    forced = set()
    origins = []
    for path in manifest_paths:
        path = Path(path)
        raw = path.read_bytes()
        manifest = json.loads(raw)
        case_ids = manifest.get("case_ids") or []
        declared_commits = manifest.get("commits") or []
        if not isinstance(declared_commits, list):
            raise ValueError(f"calibration commits must be a list: {path}")
        commits = {resolve_commit(value) for value in declared_commits}
        if not case_ids and not commits:
            raise ValueError(f"calibration manifest contains no case_ids or commits: {path}")
        missing = set(case_ids) - set(index)
        if missing:
            raise ValueError(f"calibration cases absent from census: {sorted(missing)}")
        commits.update(str(metas[cid].get("commit") or "") for cid in case_ids)
        if "" in commits:
            raise ValueError("calibration case has no migration commit")
        forced.update(commits)
        origins.append({"name": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                        "case_ids": sorted(case_ids), "commits": sorted(commits)})
    return forced, sorted(origins, key=lambda item: (item["name"], item["sha256"]))


def case_pressure(case, tokenizer, budgets=DEFAULT_BUDGETS, window=8192, require_competing=True):
    """Measure exactly the full comparison prompt; labels are not consulted."""
    # Chunk-local token counts are not used by the census; only rendered prompt
    # costs matter. Avoid wasting model tokenization on already-ineligible cases.
    chunks = evidence_chunks(selector_case(case))
    source = case["old_source"]
    mandatory = tokenizer.count_tokens(build_prompt("", source))
    target = tokenizer.count_tokens(case["reference_diff"])
    if mandatory > max(budgets):
        return {
            "mandatory_prompt_tokens": mandatory, "full_evidence_prompt_tokens": None,
            "reference_target_tokens": target, "candidate_count": len(chunks),
            "old_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "full_prompt_sha256": hashlib.sha256(build_prompt(render_context(chunks), source).encode()).hexdigest(),
            "candidate_costs": [{"id": chunk.id, "single_block_prompt_tokens": None} for chunk in chunks],
            "feasibility_status": "not_evaluated_mandatory_overflow",
            "budgets": [{"budget": budget, "eligible": False,
                         "exclusion_reasons": ["mandatory_source_prompt_exceeds_budget"]
                            + (["budget_plus_reference_target_exceeds_window"] if budget + target > window else [])
                            + (["fewer_than_two_candidate_blocks"] if len(chunks) < 2 else []),
                         "individually_feasible_blocks": None, "oversized_block_count": None,
                         "feasible_union_prompt_tokens": None, "competing_feasible_choices": None}
                        for budget in budgets],
        }
    full = tokenizer.count_tokens(build_prompt(render_context(chunks), source))
    costs = [{"id": chunk.id, "single_block_prompt_tokens": tokenizer.count_tokens(
        build_prompt(render_context([chunk]), source))} for chunk in chunks]
    rows = []
    for budget in budgets:
        feasible = [chunk for chunk, cost in zip(chunks, costs) if cost["single_block_prompt_tokens"] <= budget]
        feasible_union = tokenizer.count_tokens(build_prompt(render_context(feasible), source))
        reasons = []
        if mandatory > budget:
            reasons.append("mandatory_source_prompt_exceeds_budget")
        if full <= budget:
            reasons.append("full_evidence_prompt_fits_budget")
        if budget + target > window:
            reasons.append("budget_plus_reference_target_exceeds_window")
        if len(chunks) < 2:
            reasons.append("fewer_than_two_candidate_blocks")
        if len(feasible) < 2:
            reasons.append("fewer_than_two_individually_feasible_blocks")
        if require_competing and feasible_union <= budget:
            reasons.append("overflow_only_from_infeasible_blocks" if full > budget
                           else "no_competing_feasible_evidence")
        rows.append({"budget": budget, "eligible": not reasons, "exclusion_reasons": reasons,
                     "individually_feasible_blocks": len(feasible),
                     "oversized_block_count": len(chunks) - len(feasible),
                     "feasible_union_prompt_tokens": feasible_union,
                     "competing_feasible_choices": len(feasible) >= 2 and feasible_union > budget})
    return {
        "mandatory_prompt_tokens": mandatory, "full_evidence_prompt_tokens": full,
        "reference_target_tokens": target, "candidate_count": len(chunks),
        "feasibility_status": "measured",
        "old_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "full_prompt_sha256": hashlib.sha256(build_prompt(render_context(chunks), source).encode()).hexdigest(),
        "candidate_costs": costs, "budgets": rows,
    }


def census(index, tokenizer, forced_commits, *, budgets=DEFAULT_BUDGETS, window=8192,
           seed=0, calibration_basis_points=5000, require_competing=True, progress=False):
    # Assign every commit before loading/tokenizing any case or inspecting eligibility.
    assignments = {}
    for cid, path in sorted(index.items()):
        meta = json.loads((path / "case.json").read_text())
        commit = str(meta.get("commit") or "")
        if not commit:
            raise ValueError(f"case missing migration commit: {cid}")
        assignments[cid] = {"commit": commit,
                            "split": assign_split(commit, forced_commits, seed, calibration_basis_points),
                            "forced_calibration": commit in forced_commits}
    inventory = []
    for number, (cid, path) in enumerate(sorted(index.items()), 1):
        fingerprint = case_fingerprint(path)
        case = load_case(path)
        record = {"case_id": cid, **assignments[cid], "case_content": fingerprint}
        if case is None:
            record.update({"load_status": "incomplete_case_pack", "budgets": [
                {"budget": budget, "eligible": False, "exclusion_reasons": ["incomplete_case_pack"]} for budget in budgets]})
        else:
            if str(case["meta"].get("commit") or "") != assignments[cid]["commit"]:
                raise ValueError(f"migration commit changed after split assignment: {cid}")
            record.update({"load_status": "ok", **case_pressure(case, tokenizer, budgets, window, require_competing)})
            # Evaluation diagnostics only, computed AFTER eligibility. Never used to select or split.
            record["gold_labels_diagnostic"] = {"gold_item_count": len(case.get("gold_items") or []),
                                                 "zero_gold": not bool(case.get("gold_items"))}
        inventory.append(record)
        if progress and (number % 100 == 0 or number == len(index)):
            print(f"[pressure] tokenized {number}/{len(index)} cases", flush=True)
    return assignments, inventory


def summarize(inventory, budgets):
    summary = {}
    for budget in budgets:
        counts, reasons = Counter(), Counter()
        for record in inventory:
            counts["total_cases"] += 1
            counts["total_" + record["split"]] += 1
            row = next(row for row in record["budgets"] if row["budget"] == budget)
            reasons.update(row["exclusion_reasons"])
            if row["eligible"]:
                counts["eligible_cases"] += 1
                counts["eligible_" + record["split"]] += 1
                if record.get("gold_labels_diagnostic", {}).get("zero_gold"):
                    counts["eligible_zero_gold_diagnostic_only"] += 1
        summary[str(budget)] = {**dict(sorted(counts.items())), "exclusion_counts_nonexclusive": dict(sorted(reasons.items()))}
    return summary


def build_artifacts(index, tokenizer, forced_commits, origins, *, budgets=DEFAULT_BUDGETS,
                    window=8192, seed=0, calibration_basis_points=5000, require_competing=True,
                    progress=False):
    assignments, inventory = census(index, tokenizer, forced_commits, budgets=budgets, window=window,
                                     seed=seed, calibration_basis_points=calibration_basis_points,
                                     require_competing=require_competing, progress=progress)
    # Case packs may live outside git. Refuse to freeze a census if any bytes
    # changed while tokenization was running.
    for record in inventory:
        if case_fingerprint(index[record["case_id"]]) != record["case_content"]:
            raise ValueError(f"case-pack content changed during census: {record['case_id']}")
    pool_hashes = {record["case_id"]: record["case_content"]["sha256"] for record in inventory}
    config = {"budgets": list(budgets), "window_tokens": window, "seed": seed,
              "calibration_basis_points": calibration_basis_points, "require_competing_feasible_union": require_competing,
              "source": "full_old_source", "arm": "full", "token_count_kind": tokenizer.token_count_kind,
              "prompt_template_sha256": hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest()}
    split = {"protocol": PROTOCOL, "recipe": "sha256(context-pressure-v1:seed:commit) first64bits mod10000 < calibration_basis_points; forced commits override",
             "config": config, "labels": SPLIT_LABELS, "forced_calibration_commits": sorted(forced_commits),
             "forced_origins": origins, "case_assignments": assignments}
    inventory_file = {"protocol": PROTOCOL, "pool_content_digest": digest(pool_hashes), "cases": inventory}
    artifacts = {"inventory.json": inventory_file, "split.json": split,
                 "census.json": {"protocol": PROTOCOL, "summary_by_budget": summarize(inventory, budgets),
                                 "pool_cases": len(inventory), "pool_commits": len({x["commit"] for x in assignments.values()})}}
    for budget in budgets:
        for label in SPLIT_LABELS:
            eligible = [record for record in inventory if record["split"] == label and
                        next(row for row in record["budgets"] if row["budget"] == budget)["eligible"]]
            artifacts[f"{label}_{budget}.json"] = {
                "protocol": PROTOCOL, "schema_version": 1, "budget": budget, "split": label,
                "split_description": SPLIT_LABELS[label], "config": config,
                "candidate_pool": "evaluation_mined_casepacks_not_production_retrieval",
                "task_scope": "unverified_mined_migration",
                "required_before_patch_quality_evaluation": [
                    "map case sources to a concrete Kbuild module target and its dependencies",
                    "verify required repair using an old-driver-on-new-kernel baseline build",
                ],
                "eligibility_uses": "existing source/evidence token counts and reference target length only; no outcomes or gold-presence filtering",
                "pool_content_digest": digest(pool_hashes), "inventory_digest": digest(inventory_file),
                "split_digest": digest(split), "tokenizer": tokenizer.identity,
                "n_selected": len(eligible), "case_ids": [record["case_id"] for record in eligible],
                "case_content_sha256": {record["case_id"]: record["case_content"]["sha256"] for record in eligible},
                "case_commits": {record["case_id"]: record["commit"] for record in eligible},
            }
    return artifacts


def write_artifacts(out_dir, artifacts, code=None):
    if code and not code.get("unchanged", False):
        raise ValueError("refusing to freeze a census while code identity changed")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    for name, value in sorted(artifacts.items()):
        with (out_dir / name).open("x") as fh:
            fh.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    integrity = {"protocol": PROTOCOL, "files_sha256": {
        name: hashlib.sha256((out_dir / name).read_bytes()).hexdigest() for name in sorted(artifacts)},
        "code": code or {}, "model_inference_calls": 0,
        "code_integrity": "verified_initial_and_final" if code else "fixture_without_code_snapshot"}
    with (out_dir / "integrity.json").open("x") as fh:
        fh.write(json.dumps(integrity, indent=2, sort_keys=True) + "\n")


def verify_pressure_set(manifest_path, dataset_dir, *, budget=None, split=None):
    manifest_path = Path(manifest_path)
    root = manifest_path.parent
    integrity = json.loads((root / "integrity.json").read_text())
    if manifest_path.name not in integrity["files_sha256"]:
        raise ValueError("manifest is not part of the frozen artifact inventory")
    for name, expected in integrity["files_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"pressure artifact content changed: {name}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("protocol") != PROTOCOL:
        raise ValueError("not a context-pressure manifest")
    if manifest["config"]["token_count_kind"] == "model_tokens":
        code = integrity.get("code", {})
        if integrity.get("code_integrity") != "verified_initial_and_final" or not code.get("unchanged"):
            raise ValueError("census has no verified initial/final code identity; diagnostic artifact only")
        initial, final = code.get("initial", {}), code.get("final", {})
        if initial.get("commit") != final.get("commit") or initial.get("source_digest") != final.get("source_digest"):
            raise ValueError("census code changed while tokenizing")
    if budget is not None and manifest["budget"] != budget:
        raise ValueError("manifest budget does not match requested budget")
    if split is not None and manifest["split"] != split:
        raise ValueError("manifest split does not match requested split")
    inventory = json.loads((root / "inventory.json").read_text())
    assignments = json.loads((root / "split.json").read_text())
    if digest(inventory) != manifest["inventory_digest"] or digest(assignments) != manifest["split_digest"]:
        raise ValueError("inventory/split digest mismatch")
    index = case_index(Path(dataset_dir))
    actual_hashes = {cid: case_fingerprint(path)["sha256"] for cid, path in sorted(index.items())}
    if digest(actual_hashes) != manifest["pool_content_digest"]:
        raise ValueError("case-pack content or census pool changed")
    actual_commits = {cid: str(json.loads((path / "case.json").read_text()).get("commit") or "")
                      for cid, path in index.items()}
    recipe = assignments["config"]
    if manifest["config"] != recipe or set(assignments["case_assignments"]) != set(index):
        raise ValueError("manifest configuration or split population mismatch")
    forced = set(assignments["forced_calibration_commits"])
    if forced != {commit for origin in assignments["forced_origins"] for commit in origin["commits"]}:
        raise ValueError("forced calibration commits disagree with historical origins")
    inventory_by_id = {record["case_id"]: record for record in inventory["cases"]}
    if len(inventory_by_id) != len(inventory["cases"]) or set(inventory_by_id) != set(index):
        raise ValueError("inventory case population mismatch")
    for cid, commit in actual_commits.items():
        row = assignments["case_assignments"][cid]
        expected_split = assign_split(commit, forced, recipe["seed"], recipe["calibration_basis_points"])
        if row != {"commit": commit, "split": expected_split, "forced_calibration": commit in forced}:
            raise ValueError(f"split recipe or actual migration commit mismatch: {cid}")
        if any(inventory_by_id[cid][key] != row[key] for key in row):
            raise ValueError(f"inventory and split assignment disagree: {cid}")
    expected_ids = [record["case_id"] for record in inventory["cases"] if record["split"] == manifest["split"] and
                    next(row for row in record["budgets"] if row["budget"] == manifest["budget"])["eligible"]]
    if expected_ids != manifest["case_ids"] or len(expected_ids) != manifest["n_selected"]:
        raise ValueError("manifest selected cases disagree with inventory")
    if set(manifest["case_content_sha256"]) != set(manifest["case_ids"]):
        raise ValueError("manifest selected-case hash inventory mismatch")
    for cid in manifest["case_ids"]:
        if actual_hashes.get(cid) != manifest["case_content_sha256"].get(cid):
            raise ValueError(f"selected case content mismatch: {cid}")
    if manifest["case_commits"] != {cid: actual_commits[cid] for cid in manifest["case_ids"]}:
        raise ValueError("manifest migration commit map mismatch")
    return manifest


def load_pressure_cases(manifest_path, dataset_dir, *, budget, split):
    manifest = verify_pressure_set(manifest_path, dataset_dir, budget=budget, split=split)
    index = case_index(Path(dataset_dir))
    return [load_case(index[cid]) for cid in manifest["case_ids"]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--calibration-manifest", action="append", required=True,
                        help="previously inspected case_ids/commits; all their migration commits become calibration")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--budgets", default=",".join(map(str, DEFAULT_BUDGETS)))
    parser.add_argument("--window", type=int, default=8192)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--calibration-basis-points", type=int, default=5000)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    args = parser.parse_args(argv)
    budgets = tuple(int(part) for part in args.budgets.split(","))
    if not budgets or len(set(budgets)) != len(budgets) or any(budget <= 0 for budget in budgets):
        parser.error("budgets must be unique positive integers")
    if args.window <= 0 or not 0 <= args.calibration_basis_points <= 10000:
        parser.error("window must be positive; calibration-basis-points must be between 0 and 10000")
    if Path(args.out_dir).exists():
        raise FileExistsError(f"refusing to reuse output directory: {args.out_dir}")
    index = case_index(Path(args.dataset_dir))
    if not index:
        raise ValueError("no case packs in dataset")
    forced, origins = resolve_forced_commits(index, args.calibration_manifest)
    tokenizer = LocalTokenizer(args.model, args.revision)
    initial_code = code_identity()
    artifacts = build_artifacts(index, tokenizer, forced, origins, budgets=budgets, window=args.window,
                               seed=args.seed, calibration_basis_points=args.calibration_basis_points, progress=True)
    final_code = code_identity()
    code = {"initial": initial_code, "final": final_code,
            "unchanged": initial_code["commit"] == final_code["commit"]
                and initial_code["source_digest"] == final_code["source_digest"]}
    write_artifacts(args.out_dir, artifacts, code)
    print(json.dumps(artifacts["census.json"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
