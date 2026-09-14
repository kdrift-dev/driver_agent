#!/usr/bin/env python3
"""Freeze paired context requests from production extraction, without inference."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver_agent_pregen.kernel_evidence_pool import DOCUMENT_NAMES, build_kernel_evidence_pool, git_source_reader
from driver_agent_pregen.paired_context import HFChatCounter, MessageCharCounter, digest, prepare_pair
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.provenance import code_identity


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(args):
    extracted = args.extracted_dir.resolve(strict=True)
    out = args.out_dir.absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("refusing to reuse an output directory")
    code_before = code_identity()
    input_bytes = {}
    for name in DOCUMENT_NAMES:
        path = extracted / name
        if path.is_symlink():
            raise ValueError("symlink extraction document")
        input_bytes[name] = path.read_bytes()
    documents = {name: json.loads(raw) for name, raw in input_bytes.items()}
    module = documents["module_source.json"]
    reader = git_source_reader(args.repo)
    source_paths = sorted(set(module["source_files_old"] + module.get("headers_old", [])))
    sources = []
    for name in source_paths:
        if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
            raise ValueError("invalid extracted old-source path")
        path = extracted / "old_module_sources" / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(extracted / "old_module_sources"):
            raise ValueError("old source escaped extraction directory")
        raw = path.read_bytes()
        input_bytes["old_module_sources/" + name] = raw
        content = raw.decode("utf-8")
        if reader(module["old_ref"], name) != content:
            raise ValueError("old source does not equal its exact Git blob")
        sources.append({"path": name, "content": content})
    pool = build_kernel_evidence_pool(documents=documents, old_sources={s["path"]: s["content"] for s in sources}, source_reader=reader)
    inputs = {name: hashlib.sha256(raw).hexdigest() for name, raw in input_bytes.items()}
    if args.counter == "hf_chat_template":
        if not args.tokenizer_snapshot or not args.tokenizer_model or not args.tokenizer_revision:
            raise ValueError("HF counter requires snapshot, model id and exact revision")
        counter = HFChatCounter(args.tokenizer_snapshot, model_id=args.tokenizer_model, revision=args.tokenizer_revision)
    else:
        counter = MessageCharCounter()
    result = prepare_pair(sources, pool["obligation_signal"], pool["candidates"],
                          counter=counter, model=args.generation_model,
                          max_tokens=args.max_output_tokens, budget=args.input_budget)
    if code_identity() != code_before or inputs != {name: sha_file(extracted / name) for name in input_bytes}:
        raise ValueError("code or production extraction changed while preparing requests")
    out.mkdir(parents=True, exist_ok=False)
    write(out / "evidence_pool.json", pool)
    write(out / "old_sources.json", sources)
    summary = {k: v for k, v in result.items() if k != "conditions"}
    summary.update({"scope": "explicit full-module paired context preparation; native pruner and production defaults unchanged",
        "generation_calls": 0, "runtime_tests": 0, "code": code_before,
        "extraction_directory": str(extracted), "extraction_sha256": inputs,
        "old_ref": module["old_ref"], "new_ref": module["new_ref"],
        "kbuild_target": module.get("kbuild_target"), "module_path": module["module_path"],
        "source_verification": pool["audit"]["source_verification"], "conditions": {}})
    for name, condition in result["conditions"].items():
        folder = out / name
        write(folder / "request_body.json", condition["request_body"])
        write(folder / "input.json", condition["input_payload"])
        (folder / "prompt.md").write_text(condition["prompt"])
        audit = {k: v for k, v in condition.items() if k not in ("request_body", "input_payload", "prompt")}
        write(folder / "selection_audit.json", audit)
        summary["conditions"][name] = audit
    write(out / "summary.json", summary)
    index = {str(p.relative_to(out)): sha_file(p) for p in sorted(out.rglob("*")) if p.is_file()}
    write(out / "artifact_index.json", {"protocol": "paired_context_artifacts_v1", "files": index, "file_count": len(index)})
    print(json.dumps({"output": str(out), "counter": counter.identity["kind"],
        "budget": summary["common_input_budget"], "mandatory": summary["mandatory_count"],
        "full": summary["full_pool_count"], "candidates": len(pool["candidates"]),
        "selection_pressure_eligible": summary["selection_pressure_eligible"],
        "conditions": {k: {"input_count": v["input_count"], "selected_blocks": len(v["selected_ids"]),
            "status": v["status"], "generation_ready": v["generation_ready"]} for k, v in summary["conditions"].items()}}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--counter", choices=("message_chars", "hf_chat_template"), default="message_chars")
    parser.add_argument("--tokenizer-snapshot", type=Path)
    parser.add_argument("--tokenizer-model")
    parser.add_argument("--tokenizer-revision")
    parser.add_argument("--generation-model", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--input-budget", type=int, help="common counter-unit cap; default frozen quarter-pool rule")
    args = parser.parse_args()
    prepare(args)


if __name__ == "__main__":
    main()
