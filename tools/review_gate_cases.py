#!/usr/bin/env python3
"""Select and verify a sparse, manual, evaluation-only gate calibration review.

Selection is outcome-informed, not held out. This tool never infers semantic
judgments from the old regex categories. Git source caches stay under runs/;
the reviewed annotation contains provenance, locations and short evidence only.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re

from audit_ppl_gate import IDENTIFIER_RE, read_case, sha256

SEED = "gate-review-v2:0"
COUNT = 24


def select_cases(report):
    failed = [case for case in report["cases"] if case["missing"]]
    ranked = sorted(failed, key=lambda case: hashlib.sha256(
        (SEED + "\0" + case["case_id"]).encode()).hexdigest())
    categories = sorted(report["summary"]["identifier_categories"], key=lambda category: (
        report["summary"]["cases_with_category_nonexclusive"][category], category))
    selected, commits = [], set()
    for category in categories:
        case = next(case for case in ranked if case["commit"] not in commits
                    and any(m["category"] == category for m in case["missing"]))
        selected.append(case)
        commits.add(case["commit"])
    for case in ranked:
        if len(selected) == COUNT:
            break
        if case["commit"] not in commits:
            selected.append(case)
            commits.add(case["commit"])
    if len(selected) != COUNT:
        raise ValueError("insufficient distinct commits for the fixed 24-case review")
    return selected


def mask_comments_and_literals(text):
    # Preserve offsets/newlines. Full Git target source supplies comment state,
    # unlike concatenating only added lines. Preprocessor names remain visible.
    pattern = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', re.S)
    return pattern.sub(lambda match: "".join("\n" if c == "\n" else " " for c in match[0]), text)


def reviewed_occurrences(case, git_index):
    key = case["commit"] + ":" + case["module_file"]
    obj = git_index[key]
    source = Path(obj["cache_path"]).read_text()
    # Git hunk line numbers count LF only; splitlines() also splits formfeeds
    # (present in st.c), silently shifting otherwise correct source locations.
    masked_lines = mask_comments_and_literals(source).split("\n")
    locations = []
    for missing in case["missing"]:
        symbol = missing["identifier"]
        for hit in missing["evidence"]["reference_added"]:
            line = masked_lines[hit["source_line"] - 1]
            # Keep include-path artifacts so their explicit non_requirement
            # judgment can explain why the v2 C lexer removes them.
            matches = [m for m in IDENTIFIER_RE.finditer(line) if m[0] == symbol]
            for occurrence, match in enumerate(matches):
                locations.append({"symbol": symbol, "file": hit["source_path"],
                                  "new_line": hit["source_line"], "diff_line": hit["artifact_line"],
                                  "occurrence": occurrence, "column": match.start() + 1})
    return sorted(locations, key=lambda x: (x["new_line"], x["column"], x["symbol"]))


def verify_annotations(annotation, selected, dataset_root, git_index):
    if annotation.get("schema_version") != 1 or annotation.get("evaluation_only") is not True:
        raise ValueError("review must use schema_version=1 and evaluation_only=true")
    if annotation.get("selection", {}).get("held_out") is not False:
        raise ValueError("review must explicitly disclose that it is not held out")
    records = annotation["cases"]
    if [c["case_id"] for c in records] != [c["case_id"] for c in selected]:
        raise ValueError("review IDs/order differ from deterministic selection")
    classes, symbols, subtypes = Counter(), Counter(), Counter()
    for record, audited in zip(records, selected):
        case = read_case(dataset_root / record["case_id"])
        provenance = record["input_provenance"]
        if provenance["commit"] != case["meta"]["commit"] or provenance["parent"] != case["meta"]["parent"]:
            raise ValueError("review Git refs differ from the frozen case")
        new_obj = git_index[provenance["commit"] + ":" + case["meta"]["module_file"]]
        old_path = case["meta"].get("old_driver_path", case["meta"]["module_file"])
        old_obj = git_index[provenance["parent"] + ":" + old_path]
        for obj in (new_obj, old_obj):
            if sha256(Path(obj["cache_path"])) != obj["sha256"]:
                raise ValueError("target Git cache differs from recorded content hash")
        if Path(old_obj["cache_path"]).read_text() != case["old_source"]:
            raise ValueError("packed old source does not equal the exact parent Git object")
        if provenance["old_git_blob"] != old_obj["git_blob"] or provenance["new_git_blob"] != new_obj["git_blob"]:
            raise ValueError("target Git object IDs differ from the reviewed provenance")
        if provenance["kernel_evidence_sha256"] != sha256(dataset_root / record["case_id"] / "kernel_evidence.json"):
            raise ValueError("packed kernel evidence changed after review")
        for key in ("old_source", "reference_diff"):
            digest = hashlib.sha256(case[key].encode()).hexdigest()
            if record["input_sha256"][key] != digest:
                raise ValueError(f"input binding differs: {record['case_id']} {key}")
        if record["review"].get("scope") != "legacy_missing_occurrences_only":
            raise ValueError("sparse review coverage must be explicit")
        expected = reviewed_occurrences(audited, git_index)
        expected_locations = {(h["symbol"], h["file"], h["new_line"], h["occurrence"]): h
                              for h in expected}
        expected_keys = {(h["symbol"], h["file"], h["new_line"], h["occurrence"]) for h in expected}
        actual_keys = [(j["symbol"], j["file"], j["new_line"], j["occurrence"]) for j in record["judgments"]]
        if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
            raise ValueError(f"missing/duplicate/extraneous occurrence judgment: {record['case_id']}")
        if set(j["symbol"] for j in record["judgments"]) != set(audited["legacy_gate"]["missing_identifiers"]):
            raise ValueError(f"legacy missing-name coverage incomplete: {record['case_id']}")
        for judgment in record["judgments"]:
            key = (judgment["symbol"], judgment["file"], judgment["new_line"], judgment["occurrence"])
            location = expected_locations[key]
            if any(judgment[field] != location[field] for field in ("column", "diff_line")):
                raise ValueError("reviewed occurrence column/diff location differs")
            classification = judgment["classification"]
            if classification not in {"external", "local", "non_requirement", "unknown"}:
                raise ValueError("unknown judgment classification")
            target_line = Path(new_obj["cache_path"]).read_text().split("\n")[judgment["new_line"] - 1]
            include = re.match(r"\s*#\s*include\s*<([^>]+)>", target_line)
            if (include and include.start(1) <= judgment["column"] - 1 < include.end(1)
                    and (classification != "non_requirement" or judgment["subtype"] != "include_path_fragment")):
                raise ValueError("include-path occurrence is not a C identifier requirement")
            if not judgment["rationale"] or not judgment["evidence"]:
                raise ValueError("judgment lacks review trace")
            classes[classification] += 1
            subtypes[judgment["subtype"]] += 1
            for evidence in judgment["evidence"]:
                if evidence["source"] == "reference_diff":
                    text = case["reference_diff"]
                elif evidence["source"] == "old_source":
                    text = case["old_source"]
                elif evidence["source"] == "kernel_git":
                    obj = git_index[evidence["ref"] + ":" + evidence["path"]]
                    if evidence["blob_sha256"] != obj["sha256"] or sha256(Path(obj["cache_path"])) != obj["sha256"]:
                        raise ValueError("Git evidence cache/hash differs")
                    text = Path(obj["cache_path"]).read_text()
                else:
                    raise ValueError(f"unsupported evidence source {evidence['source']}")
                if evidence.get("quote") and evidence["quote"] not in text:
                    raise ValueError(f"evidence quote absent: {record['case_id']} {judgment['symbol']}")
                if evidence.get("line"):
                    line = text.split("\n")[evidence["line"] - 1]
                    if evidence.get("quote") and evidence["quote"] not in line:
                        raise ValueError("evidence line/quote differ")
                scope = evidence.get("scope")
                if scope and not (1 <= scope["start_line"] <= scope["end_line"] <= len(text.split("\n"))):
                    raise ValueError("invalid reviewed scope bounds")
                if scope and not (scope["start_line"] <= evidence["line"] <= scope["end_line"]):
                    raise ValueError("declaration evidence is outside its reviewed scope")
                if scope and judgment["subtype"] in {"block_variable", "function_parameter", "target_macro_formal"}:
                    if not scope["start_line"] <= judgment["new_line"] <= scope["end_line"]:
                        raise ValueError("local occurrence is outside its reviewed scope")
            if classification == "local" and not any(
                e["source"] == "kernel_git" and e.get("scope") for e in judgment["evidence"]
            ):
                raise ValueError("local judgment lacks full-source scope trace")
        for symbol in sorted({j["symbol"] for j in record["judgments"]}):
            values = {j["classification"] for j in record["judgments"] if j["symbol"] == symbol}
            symbols[next(iter(values)) if len(values) == 1 else "mixed"] += 1
    return {"cases": len(records), "distinct_commits": len({c["commit"] for c in selected}),
            "legacy_missing_names": sum(len(c["missing"]) for c in selected),
            "reviewed_occurrences": sum(classes.values()), "classification_occurrences": dict(classes),
            "classification_case_symbol_pairs": dict(symbols), "subtype_occurrences": dict(subtypes),
            "held_out": False, "scope": "legacy_missing_occurrences_only"}


def negative_controls(annotation, selected, root, git_index):
    """Integrity failures must not quietly produce a reviewed-case result."""
    controls = []
    changed = copy.deepcopy(annotation)
    changed["cases"][0]["input_sha256"]["old_source"] = "0" * 64
    controls.append(("changed source fingerprint", changed))
    changed = copy.deepcopy(annotation)
    changed["cases"][0]["judgments"].pop()
    controls.append(("missing occurrence judgment", changed))
    changed = copy.deepcopy(annotation)
    changed["cases"][0]["judgments"][0]["occurrence"] = 999
    controls.append(("wrong same-line occurrence", changed))
    changed = copy.deepcopy(annotation)
    evidence = next(e for c in changed["cases"] for j in c["judgments"]
                    if j["classification"] == "local" for e in j["evidence"] if e.get("scope"))
    evidence["scope"]["end_line"] = evidence["scope"]["start_line"] - 1
    controls.append(("invalid local scope", changed))
    changed = copy.deepcopy(annotation)
    record = next(c for c in changed["cases"] if c["case_id"].startswith("0ac6f4056c4a__"))
    header = next(j for j in record["judgments"]
                  if j["symbol"] == "resv" and j["subtype"] == "include_path_fragment")
    member = next(j for j in record["judgments"]
                  if j["symbol"] == "resv" and j["classification"] == "external")
    header["classification"] = member["classification"]
    header["subtype"] = member["subtype"]
    controls.append(("header fragment inherits same-spelled member classification", changed))
    for name, changed in controls:
        try:
            verify_annotations(changed, selected, root, git_index)
        except ValueError:
            continue
        raise AssertionError(f"negative control was accepted: {name}")
    return [name for name, _ in controls]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=Path("runs/gate_audit_20260914/gate_audit.json"))
    parser.add_argument("--dataset-dir", type=Path, default=Path("runs/gate_audit_input_20260914"))
    parser.add_argument("--git-index", type=Path, default=Path("runs/gate_review_20260914/git_objects/index.json"))
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--negative-controls", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = json.loads(args.audit.read_text())
    selected = select_cases(report)
    result = {"schema_version": 1, "evaluation_only": True,
              "selection": {"seed": SEED, "count": COUNT, "held_out": False,
                            "recipe": "SHA256(seed + NUL + case_id) rank; one unseen commit per category, rarest category first; fill unseen commits in rank order.",
                            "source_audit_sha256": sha256(args.audit)},
              "case_ids": [case["case_id"] for case in selected]}
    if args.annotations:
        annotations = json.loads(args.annotations.read_text())
        if annotations["selection"]["source_audit_sha256"] != sha256(args.audit):
            raise ValueError("annotation audit provenance differs")
        root = args.dataset_dir / "cases" if (args.dataset_dir / "cases").is_dir() else args.dataset_dir
        git_index = json.loads(args.git_index.read_text())
        result["verification"] = verify_annotations(annotations, selected, root, git_index)
        if args.negative_controls:
            result["negative_controls_rejected"] = negative_controls(annotations, selected, root, git_index)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
