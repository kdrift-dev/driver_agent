#!/usr/bin/env python3
"""Evaluate a sparse, reviewed calibration set with gate v2 (no model calls).

This is diagnostic evaluation. It neither relabels the frozen experiment nor
changes production prompts. The 8000-character condition isolates the legacy
gold cutoff; it is not a proposed model-token budget for a new PPL protocol.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from ctxlab.dataset import load_cases
from ctxlab.gate_v2 import evaluate_gate
from ctxlab.metrics import expressibility_check
from ctxlab.provenance import write_run_metadata

CONDITIONS = ("gold_full", "gold_prefix_8000", "none")


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def summary(rows):
    aggregates = []
    for condition in CONDITIONS:
        selected = [row for row in rows if row["condition"] == condition]
        counts = Counter(row["gate_v2"]["status"] for row in selected)
        aggregates.append({"condition": condition, "cases": len(selected),
                           "v2_statuses": dict(sorted(counts.items())),
                           "legacy_pass": sum(row["legacy_gate"]["gate_pass"] for row in selected)})
    return aggregates


def report_markdown(aggregates):
    lines = ["# Gate v2 calibration diagnostics", "",
             "The reviewed cases were selected from known historical failures. "
             "This is a calibration review, not held-out evaluation or a new PPL experiment.", "",
             "| Context | Cases | pass | fail | unknown | not_applicable | Legacy mention pass |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for record in aggregates:
        counts = record["v2_statuses"]
        lines.append(f"| {record['condition']} | {record['cases']} | "
                     + " | ".join(str(counts.get(status, 0)) for status in
                                  ("pass", "fail", "unknown", "not_applicable"))
                     + f" | {record['legacy_pass']} |")
    lines.extend(["", "No adjusted historical pass rate is computed. Unknown and not_applicable "
                  "are not successes; the two gate definitions have different targets.", "",
                  "Every condition uses the complete packed old source for both gates. "
                  "gold_prefix_8000 keeps 8000 context characters solely to diagnose the old "
                  "cutoff. No model or selector is invoked. Exact input texts, sparse review "
                  "scope and per-occurrence decisions are retained in gate_review_rows.jsonl.", ""])
    return "\n".join(lines)


def run(args):
    output = Path(args.out_root)
    if any((output / name).exists() for name in
           ("run_metadata.json", "run_metadata.started.json", "gate_review_rows.jsonl")):
        raise ValueError(f"existing gate run at {output}; choose a new --out-root")
    annotations_path = Path(args.annotations)
    annotations_text = annotations_path.read_text()
    annotations = json.loads(annotations_text)
    if annotations.get("evaluation_only") is not True or annotations.get("schema_version") != 1:
        raise ValueError("unsupported or non-evaluation annotation document")
    reviewed = annotations.get("cases")
    if not isinstance(reviewed, list) or not reviewed:
        raise ValueError("annotation document must contain reviewed cases")
    ids = [record["case_id"] for record in reviewed]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate reviewed case IDs")
    loaded = load_cases(Path(args.dataset_dir), verbose=False)
    by_id = {case["case_id"]: case for case in loaded}
    if len(loaded) != len(by_id):
        raise ValueError("duplicate dataset case IDs")
    if any(cid not in by_id for cid in ids):
        raise ValueError("reviewed cases are absent from dataset")
    cases = [by_id[cid] for cid in ids]
    extra = {"protocol": "gate_v2_review_diagnostic", "phase": "started",
             "annotations_sha256": sha256(annotations_text),
             "sample_role": "historically_exposed_calibration", "model_calls": 0}
    write_run_metadata(output, args, cases, extra=extra)
    rows = []
    for case, annotation in zip(cases, reviewed):
        contexts = {"gold_full": case["gold_text"],
                    "gold_prefix_8000": case["gold_text"][:8000], "none": ""}
        for condition in CONDITIONS:
            context, source = contexts[condition], case["old_source"]
            gate = evaluate_gate(context, source, case["reference_diff"], annotation=annotation,
                                 source_files=case.get("old_sources_by_path"))
            annotation_errors = [issue for issue in gate.get("input_issues", [])
                                 if issue.startswith("annotation_") or issue.startswith("malformed_annotation")]
            if annotation_errors:
                raise ValueError(f"invalid review binding for {case['case_id']}: {annotation_errors}")
            if gate["status"] not in {"pass", "fail", "unknown", "not_applicable"}:
                raise ValueError(f"unexpected gate status: {gate['status']}")
            rows.append({"case_id": case["case_id"], "condition": condition,
                         "evaluation_only": True, "sample_role": "calibration",
                         "context_text": context, "old_source_text": source,
                         "reference_sha256": sha256(case["reference_diff"]),
                         "annotation_sha256": sha256(json.dumps(annotation, sort_keys=True)),
                         "review_scope": annotation.get("review"), "gate_v2": gate,
                         "legacy_gate": expressibility_check(context, source, case["reference_diff"])})
    with (output / "gate_review_rows.jsonl").open("x") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    aggregates = summary(rows)
    (output / "gate_review_summary.json").write_text(json.dumps(aggregates, indent=2) + "\n")
    (output / "gate_review_summary.md").write_text(report_markdown(aggregates))
    extra.update(phase="complete", rows=len(rows),
                 annotations_unchanged=sha256(annotations_path.read_text()) == extra["annotations_sha256"])
    metadata = write_run_metadata(output, args, cases, extra=extra)
    if not extra["annotations_unchanged"] or not all(metadata["integrity"][key] for key in
            ("selected_content_unchanged", "code_unchanged", "manifest_unchanged")):
        raise RuntimeError("inputs or code changed during evaluation; output is diagnostic only, not reproducible")
    print(json.dumps({"cases": len(cases), "rows": len(rows), "model_calls": 0,
                      "aggregates": aggregates}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, default=Path("datasets/gate_review_v2.json"))
    parser.add_argument("--out-root", type=Path, required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
