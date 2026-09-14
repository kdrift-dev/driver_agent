#!/usr/bin/env python3
"""Independent frozen-code parity and forensic-evidence integrity checks."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import audit_ppl_gate as audit


def frozen_oracle():
    namespace = {"Any": Any}
    root = Path(__file__).resolve().parents[1]
    for path, blob in audit.LEGACY_BLOBS.items():
        content = subprocess.check_output(["git", "show", f"{audit.LEGACY_COMMIT}:{path}"], cwd=root)
        actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        assert actual == blob, f"frozen blob differs: {path}"
        if path.endswith("textutil.py"):
            exec(compile(content, f"{audit.LEGACY_COMMIT}:{path}", "exec"), namespace)
    for path, names in (("tools/ctxlab/metrics.py", {"added_lines", "expressibility_check"}),
                        ("tools/ctxlab/dataset.py", {"evidence_text"})):
        content = subprocess.check_output(["git", "show", f"{audit.LEGACY_COMMIT}:{path}"], cwd=root)
        tree = ast.parse(content)
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(selected) == len(names)
        exec(compile(ast.Module(body=selected, type_ignores=[]), f"{audit.LEGACY_COMMIT}:{path}", "exec"), namespace)
    return namespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    args = parser.parse_args()
    oracle = frozen_oracle()
    checks = 1
    fixtures = [
        ("symbol: kernel_api", "old_name", "+int fresh = kernel_api(old_name);"),
        ("", "", "+#include <linux/dma-buf.h>\n+int x = 0x5c;\n+char c = 'z';"),
        ("", "", '+/* text */ int a; // tail\n+* continuation\n+char *s = "literal";'),
        ("api", "old", "+int old = api();\n+++ b/metadata"),
    ]
    for context, source, diff in fixtures:
        assert audit.legacy_gate(context, source, diff) == oracle["expressibility_check"](context, source, diff)
        checks += 1
    manifest = json.loads(args.manifest.read_text())
    root = args.dataset_dir / "cases" if (args.dataset_dir / "cases").exists() else args.dataset_dir
    cases = [audit.read_case(root / cid) for cid in manifest["case_ids"]]
    for index, case in enumerate(cases):
        ev = json.loads((case["directory"] / "kernel_evidence.json").read_text())
        for item in ev["gold"] + ev["distractors"]:
            assert audit.evidence_text(item) == oracle["evidence_text"](item)
        for condition in audit.CONDITIONS:
            context, _ = audit.context_for(cases, index, condition, 8000)
            assert audit.legacy_gate(context, case["old_source"], case["reference_diff"]) == oracle["expressibility_check"](context, case["old_source"], case["reference_diff"])
            checks += 1
    report = json.loads(args.audit_report.read_text())
    assert report["historical_reconciliation"]["matches"]
    assert report["summary"]["cases"] == len(cases)
    assert [r["case_id"] for r in report["cases"]] == manifest["case_ids"]
    assert report["summary"] == audit.aggregate(report["cases"], report["scorer_old_source_budget"])
    for case, record in zip(cases, report["cases"]):
        assert [m["identifier"] for m in record["missing"]] == record["legacy_gate"]["missing_identifiers"]
        for miss in record["missing"]:
            assert miss["evidence"]["reference_added"], (case["case_id"], miss["identifier"], "no source evidence")
            for location in miss["evidence"]["reference_added"]:
                raw = case["reference_diff"].splitlines()[location["artifact_line"] - 1]
                assert raw.startswith("+") and raw[1:] == location["text"]
                assert miss["identifier"] in audit.identifiers(raw)
            assert not miss["semantic_ownership_proven"]
            checks += 1
    # The legacy gate deliberately sees the full source; do not silently turn
    # the scorer's source prefix into the historical definition.
    source = " " * 12000 + "outside_prefix"
    assert audit.legacy_gate("", source, "+outside_prefix();")["gate_pass"]
    assert not audit.legacy_gate("", source[:12000], "+outside_prefix();")["gate_pass"]
    checks += 1
    # A corrupt historical row must invalidate conclusions, including a stale
    # aggregate from an earlier successful run using the same output directory.
    with tempfile.TemporaryDirectory(prefix="gate_audit_checks_") as tmp:
        work = Path(tmp)
        directory = work / "input" / "only"
        directory.mkdir(parents=True)
        (directory / "old_module_sources").mkdir()
        (directory / "old_module_sources" / "driver.c").write_text("old_name;")
        (directory / "case.json").write_text(json.dumps({"case_id": "only", "evaluation_only": True}))
        (directory / "reference.diff").write_text("+new_name(old_name);\n")
        (directory / "kernel_side.diff").write_text("+new_name;\n")
        (directory / "kernel_evidence.json").write_text(json.dumps({"gold": [{"symbol": "new_name"}], "distractors": []}))
        small = [audit.read_case(directory)]
        csv_path = work / "bad.csv"
        fields = ["case_id", "condition", "gate_coverage", "gate_pass", "gate_missing_count", "context_chars", "context_truncated"]
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for condition in audit.CONDITIONS:
                context, truncated = audit.context_for(small, 0, condition, 8000)
                gate = oracle["expressibility_check"](context, small[0]["old_source"], small[0]["reference_diff"])
                writer.writerow({"case_id": "only", "condition": condition,
                                 "gate_coverage": gate["coverage"], "gate_pass": gate["gate_pass"],
                                 "gate_missing_count": len(gate["missing_identifiers"]),
                                 "context_chars": len(context) + (condition == "gold"),
                                 "context_truncated": truncated})
        manifest_path = work / "manifest.json"
        manifest_path.write_text(json.dumps({"case_ids": ["only"], "n_selected": 1}))
        out = work / "out"
        out.mkdir()
        (out / "gate_audit.json").write_text('{"summary": "stale success"}')
        code = audit.main(["--dataset-dir", str(work / "input"), "--manifest", str(manifest_path),
                           "--historical-csv", str(csv_path), "--out-dir", str(out)])
        assert code == 2
        invalid = json.loads((out / "gate_audit.json").read_text())
        assert invalid["status"] == "historical_reconciliation_failed" and invalid["summary"] is None
        assert not (out / "cases").exists()
        checks += 1
    print(f"PASS: {checks} assertions/groups; frozen-code parity on {len(cases) * 5} conditions, every missing-token evidence location, fail-closed historical mismatch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
