#!/usr/bin/env python3
"""Evaluation-only forensic audit of the frozen PPL expressibility gate.

The legacy gate/loader below are intentionally independent of ctxlab, whose
semantics may change. Never pass this tool's reference-derived observations to
patch generation, relabel the frozen packs, or treat the categories as an oracle.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

LEGACY_COMMIT = "924a8b5b3b0caa5bf4e1ae23a523bc37751a2af7"
LEGACY_BLOBS = {
    "tools/ctxlab/metrics.py": "55e0ac731eed4d5bdf626122e7e08255abcf0262",
    "tools/ctxlab/textutil.py": "f2e6e9334e0d44bad3500ad5f69dd0d2addc5465",
    "tools/ctxlab/dataset.py": "b55268517db8b13d205ba5f64f25cb9915d2d23e",
    "tools/run_ppl_signal_experiment.py": "91259a9f9d2027367fa0c55e645f8958033d0b05",
}
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
C_KEYWORDS = set("""auto break case char const continue default do double else
enum extern float for goto if inline int long register restrict return short
signed sizeof static struct switch typedef union unsigned void volatile while
bool true false NULL u8 u16 u32 u64 s8 s16 s32 s64 size_t ssize_t int8_t int16_t
int32_t int64_t uint8_t uint16_t uint32_t uint64_t __init __exit __user __iomem
__must_check __maybe_unused __packed likely unlikely""".split())
CONDITIONS = ("gold", "gold_distractors", "distractors_only", "random", "none")


def identifiers(text):
    return set(IDENTIFIER_RE.findall(text)) - C_KEYWORDS


def strip_comments_and_strings(code):
    # Preserve even the frozen heuristic's defects (e.g. character literals).
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    out = []
    for line in code.splitlines():
        if line.lstrip().startswith(("*", "/*")):
            continue
        line = re.sub(r"//.*$", " ", line)
        if "/*" in line:
            line = line.split("/*", 1)[0]
        if "*/" in line:
            line = line.split("*/", 1)[1]
        out.append(re.sub(r'"(?:[^"\\]|\\.)*"', " ", line))
    return "\n".join(out)


def added_lines(diff):
    return [line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def legacy_gate(context, old_source, reference):
    needed = identifiers(strip_comments_and_strings("\n".join(added_lines(reference))))
    missing = sorted(needed - identifiers(old_source) - identifiers(context))
    return {"needed_identifiers": len(needed), "missing_identifiers": missing,
            "coverage": round((len(needed) - len(missing)) / len(needed), 4) if needed else 1.0,
            "gate_pass": not missing}


def evidence_text(item):
    parts = [f"symbol: {item.get('symbol', '?')}"]
    for key in ("definition_diff", "old_definition", "new_definition",
                "old_definition_excerpt", "new_definition_excerpt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"--- {key} ---\n{value.strip()}")
    for side in ("old_locations", "new_locations"):
        lines = [f"{loc.get('path')}:{loc.get('line')}: {loc.get('text', '')}"
                 for loc in (item.get(side) or []) if isinstance(loc, dict)]
        if lines:
            parts.append(f"--- {side} ---\n" + "\n".join(lines))
    return "\n".join(parts)


def read_case(directory):
    meta = json.loads((directory / "case.json").read_text())
    ev = json.loads((directory / "kernel_evidence.json").read_text())
    if "gold" not in ev or meta.get("evaluation_only") is not True:
        raise ValueError(f"not an evaluation-only mined pack: {directory}")
    source_files = sorted(p for p in (directory / "old_module_sources").rglob("*")
                          if p.is_file() and p.suffix in (".c", ".h"))
    if not source_files:
        raise ValueError(f"missing old source: {directory}")
    return {"directory": directory, "meta": meta, "case_id": meta["case_id"],
            "reference_diff": (directory / "reference.diff").read_text(errors="replace"),
            "kernel_diff": (directory / "kernel_side.diff").read_text(errors="replace"),
            "gold_text": "\n\n".join(evidence_text(it) for it in (ev.get("gold") or [])),
            "distractor_text": "\n\n".join(evidence_text(it) for it in (ev.get("distractors") or [])),
            "old_source": "\n\n".join(p.read_text(errors="replace") for p in source_files)}


def context_for(cases, index, condition, budget):
    case = cases[index]
    texts = {"gold": case["gold_text"],
             "gold_distractors": case["gold_text"] + "\n\n" + case["distractor_text"],
             "distractors_only": case["distractor_text"],
             "random": cases[(index + 1) % len(cases)]["gold_text"] if len(cases) > 1 else "",
             "none": ""}
    text = texts[condition]
    return text[:budget], len(text) > budget


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_inventory(directory):
    files = {str(p.relative_to(directory)): sha256(p)
             for p in sorted(directory.rglob("*")) if p.is_file()}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return files, digest


def bool_value(value):
    if value not in ("True", "False", "true", "false", "1", "0"):
        raise ValueError(f"invalid historical boolean {value!r}")
    return value in ("True", "true", "1")


def reconcile(cases, historical_csv, context_budget):
    with historical_csv.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    historical = {}
    for row in rows:
        key = (row["case_id"], row["condition"])
        if key in historical:
            raise ValueError(f"duplicate historical row: {key}")
        historical[key] = row
    expected = {(c["case_id"], cond) for c in cases for cond in CONDITIONS}
    if set(historical) != expected:
        raise ValueError("historical CSV must contain exactly the frozen cases × five conditions")
    comparisons, mismatches = [], []
    for index, case in enumerate(cases):
        for condition in CONDITIONS:
            context, truncated = context_for(cases, index, condition, context_budget)
            gate = legacy_gate(context, case["old_source"], case["reference_diff"])
            actual = {"gate_coverage": gate["coverage"], "gate_pass": gate["gate_pass"],
                      "gate_missing_count": len(gate["missing_identifiers"]),
                      "context_chars": len(context), "context_truncated": truncated}
            row = historical[(case["case_id"], condition)]
            old = {"gate_coverage": float(row["gate_coverage"]),
                   "gate_pass": bool_value(row["gate_pass"]),
                   "gate_missing_count": int(row["gate_missing_count"]),
                   "context_chars": int(row["context_chars"]),
                   "context_truncated": bool_value(row["context_truncated"])}
            comparison = {"case_id": case["case_id"], "condition": condition,
                          "matches": actual == old, "historical": old, "rederived": actual}
            comparisons.append(comparison)
            if actual != old:
                mismatches.append(comparison)
    return {"rows": len(comparisons), "gold_cases": len(cases),
            "matches": not mismatches, "mismatches": mismatches,
            "comparisons": comparisons}


def diff_locations(diff, symbol, artifact, added_only=False):
    path, old_line, new_line = None, None, None
    hits = []
    for line_number, line in enumerate(diff.splitlines(), 1):
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            old_line, new_line = int(match[1]), int(match[2])
            continue
        if line.startswith(("diff ", "--- ", "+++ ", "index ")):
            continue
        if new_line is None:
            continue
        side = "new" if line.startswith("+") else "old" if line.startswith("-") else "context"
        if symbol in identifiers(line) and (not added_only or side == "new"):
            hits.append({"artifact": artifact, "artifact_line": line_number,
                         "source_path": path, "source_line": old_line if side == "old" else new_line,
                         "side": side, "text": line[1:]})
        if not line.startswith(("-", "\\")):
            new_line += 1
        if not line.startswith(("+", "\\")):
            old_line += 1
    return hits


def text_locations(text, symbol, artifact):
    return [{"artifact": artifact, "artifact_line": number, "text": line}
            for number, line in enumerate(text.splitlines(), 1) if symbol in identifiers(line)]


def new_side_hunks(reference):
    """Evaluation-only signature context; hunk boundaries prevent joining C statements."""
    out = []
    in_hunk = False
    for line in reference.splitlines():
        if line.startswith("@@ "):
            out.append("; /* hunk boundary */")
            in_hunk = True
        elif line.startswith("diff "):
            in_hunk = False
        elif in_hunk and line.startswith(("+", " ")):
            out.append(line[1:])
    return "\n".join(out)


def classify_identifier(case, symbol, budget):
    """Conservative triage, not C name resolution or proof of label validity."""
    locations = diff_locations(case["reference_diff"], symbol, "reference.diff", True)
    kernel_hits = diff_locations(case["kernel_diff"], symbol, "kernel_side.diff")
    gold_hits = text_locations(case["gold_text"], symbol, "audit_rendered_gold.txt")
    distractor_hits = text_locations(case["distractor_text"], symbol, "audit_rendered_distractors.txt")
    code = "\n".join(added_lines(case["reference_diff"]))
    clean = strip_comments_and_strings(code)
    new_side = strip_comments_and_strings(new_side_hunks(case["reference_diff"]))
    escaped = re.escape(symbol)
    observations = []
    # Pattern hits are evidence candidates; no case-specific whitelists.
    declaration_patterns = {
        "macro_defined_in_driver_patch": rf"(?m)^\s*#\s*define\s+{escaped}\b",
        "label_defined_in_driver_patch": rf"(?m)^\s*{escaped}\s*:(?!:)",
        "variable_or_parameter_declaration":
            rf"\b(?:void|int|char|long|short|bool|u8|u16|u32|u64|s8|s16|s32|s64|size_t|ssize_t|"
            rf"struct\s+\w+|enum\s+\w+|union\s+\w+|\w+_t)\s+\**\s*{escaped}\s*(?:__\w+\s*)*(?=[=;,)\[])",
        "comma_variable_declaration_candidate":
            rf"(?m)^\s*(?:static\s+)?(?:const\s+)?(?:int|char|long|short|bool|"
            rf"struct\s+\w+|\w+_t)\s+[^;()\n=]+,\s*\**\s*{escaped}\s*(?=[=;,])",
        "function_pointer_declaration_candidate": rf"\(\s*\*\s*{escaped}\s*\)\s*\(",
        "function_declaration_candidate":
            rf"(?m)^\s*(?:(?:static|inline|const|unsigned|__init|__exit)\s+)*"
            rf"(?:void|int|char|long|short|bool|u8|u16|u32|u64|struct\s+\w+|\w+_t)"
            rf"\s+\**\s*{escaped}\s*\(",
        "static_function_declaration_candidate": rf"(?m)^\s*static\s+[^;={{}}\n]+\b{escaped}\s*\(",
        "tag_defined_in_driver_patch": rf"\b(?:struct|enum|union)\s+{escaped}\s*\{{",
        "macro_parameter_in_driver_patch": rf"(?m)^\s*#\s*define\s+\w+\([^\n)]*\b{escaped}\b[^\n)]*\)",
    }
    for observation, pattern in declaration_patterns.items():
        match = re.search(pattern, new_side)
        if match:
            observations.append({"kind": observation, "matched_text": match[0]})
    field = bool(re.search(rf"(?:->|\.)\s*{escaped}\b", clean))
    type_tag = bool(re.search(rf"\b(?:struct|union|enum)\s+{escaped}\b", clean))
    call = bool(re.search(rf"\b{escaped}\s*\(", clean))
    macro = symbol.isupper()
    if field:
        observations.append({"kind": "member_access_or_designated_initializer"})
    if type_tag or symbol.endswith("_t"):
        observations.append({"kind": "type_tag_or_typedef_spelling"})
    if call or macro:
        observations.append({"kind": "call_or_macro_spelling"})
    macro_argument = bool(re.search(rf"\b[A-Z_][A-Z_0-9]*\([^;\n]*\b{escaped}\b", clean))
    token_paste = bool(re.search(rf"(?:##\s*{escaped}\b|\b{escaped}\s*##)", clean))
    extern = bool(re.search(rf"\bextern\b[^;\n]*\b{escaped}\b", new_side))
    if macro_argument or token_paste:
        observations.append({"kind": "macro_argument_or_token_pasting"})
    if extern:
        observations.append({"kind": "explicit_extern_declaration"})

    # Legacy regex erroneously extracts xABCD / U suffixes from numeric tokens,
    # header path components and character contents. Only call this an artifact
    # if all occurrences disappear when these lexical forms are removed.
    non_literal = re.sub(r"(?m)^\s*#\s*include\s*<[^>]+>.*$", " ", clean)
    non_literal = re.sub(r"'(?:[^'\\]|\\.)*'", " ", non_literal)
    non_literal = re.sub(r"\b(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d*)?)(?:[uUlLfF]+)?\b", " ", non_literal)
    lexical_only = symbol not in identifiers(non_literal)
    if lexical_only:
        category, confidence = "legacy_lexical_artifact", "high"
        reason = "All needed-token occurrences are in numeric/character literals or angle-bracket include paths."
    elif symbol in identifiers(case["gold_text"]) and symbol not in identifiers(case["gold_text"][:budget]):
        category, confidence = "context_budget_truncation", "high"
        reason = "Identifier occurs in full rendered gold but is absent after the historical context prefix cutoff."
    elif observations and any(o["kind"] in declaration_patterns for o in observations) and not kernel_hits and not extern:
        category, confidence = "new_driver_declaration_candidate", "medium"
        reason = "Added driver lines contain a declaration/label/definition pattern; ownership and scope are heuristic."
    elif kernel_hits:
        category, confidence = "kernel_diff_not_in_gold_context", "high"
        reason = "Exact identifier occurs in the packed kernel-side diff but not in scored gold. This proves a mention omission, not a required dependency."
    elif distractor_hits:
        category, confidence = "distractor_evidence_not_in_gold", "high"
        reason = "Exact identifier occurs in packed distractor evidence but not gold; semantic relevance is not established."
    elif field or type_tag or symbol.endswith("_t") or call or macro or macro_argument or token_paste or extern:
        category, confidence = "indirection_or_external_dependency_candidate", "low"
        reason = "Member/type/call/macro syntax suggests a dependency requiring name resolution; neither kernel ownership nor missing definition is proven."
    else:
        category, confidence = "unresolved", "low"
        reason = "No sufficient declaration or packed kernel evidence establishes ownership or cause."
    return {"identifier": symbol, "category": category, "confidence": confidence,
            "reason": reason, "observations": observations,
            "evidence": {"reference_added": locations,
                         "reference_all_hunk_mentions": diff_locations(case["reference_diff"], symbol, "reference.diff"),
                         "kernel_diff": kernel_hits,
                         "full_gold": gold_hits, "distractors": distractor_hits},
            "semantic_ownership_proven": False}


def audit_cases(cases, context_budget, old_source_budget, out_dir):
    results = []
    for case in cases:
        context = case["gold_text"][:context_budget]
        gate = legacy_gate(context, case["old_source"], case["reference_diff"])
        prefix = legacy_gate(context, case["old_source"][:old_source_budget], case["reference_diff"])
        missing = [classify_identifier(case, symbol, context_budget) for symbol in gate["missing_identifiers"]]
        case_dir = out_dir / "cases" / case["case_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        # Audit-only reconstruction, useful for exact evidence line citations.
        (case_dir / "audit_rendered_gold.txt").write_text(case["gold_text"])
        (case_dir / "audit_rendered_distractors.txt").write_text(case["distractor_text"])
        record = {"case_id": case["case_id"], "input_directory": str(case["directory"]),
                  "evaluation_only": True, "commit": case["meta"].get("commit"),
                  "module_file": case["meta"].get("module_file"),
                  "treewide_scale": case["meta"].get("treewide_scale"),
                  "legacy_gate": gate, "scorer_source_prefix_gate_diagnostic": prefix,
                  "additional_missing_after_source_prefix": sorted(set(prefix["missing_identifiers"]) - set(gate["missing_identifiers"])),
                  "old_source_chars": len(case["old_source"]),
                  "gold_chars": len(case["gold_text"]), "missing": missing}
        (case_dir / "audit.json").write_text(json.dumps(record, indent=2) + "\n")
        results.append(record)
    return results


def aggregate(records, old_source_budget=12000):
    category_identifiers, category_cases = Counter(), Counter()
    confidence, syntax = Counter(), Counter()
    for record in records:
        category_cases.update({m["category"] for m in record["missing"]})
        category_identifiers.update(m["category"] for m in record["missing"])
        confidence.update(m["confidence"] for m in record["missing"])
        syntax.update(observation["kind"] for m in record["missing"] for observation in m["observations"])
    failed = [r for r in records if not r["legacy_gate"]["gate_pass"]]
    return {"cases": len(records), "gold_pass": sum(r["legacy_gate"]["gate_pass"] for r in records),
            "gold_fail": sum(not r["legacy_gate"]["gate_pass"] for r in records),
            "missing_identifier_occurrences": sum(len(r["missing"]) for r in records),
            "mean_legacy_coverage": sum(r["legacy_gate"]["coverage"] for r in records) / len(records),
            "identifier_categories": dict(sorted(category_identifiers.items())),
            "cases_with_category_nonexclusive": dict(sorted(category_cases.items())),
            "confidence_counts": dict(confidence),
            "syntax_observations_nonexclusive": dict(sorted(syntax.items())),
            "failure_treewide_scale_histogram": dict(sorted(Counter(str(r["treewide_scale"]) for r in failed).items(), key=lambda pair: int(pair[0]))),
            "below_0_75_treewide_scale_histogram": dict(sorted(Counter(str(r["treewide_scale"]) for r in failed if r["legacy_gate"]["coverage"] < .75).items(), key=lambda pair: int(pair[0]))),
            "failing_unique_commits": len({r["commit"] for r in failed}),
            "old_source_over_prefix_cases": sum(r["old_source_chars"] > old_source_budget for r in records),
            "cases_with_additional_missing_after_source_prefix": sum(bool(r["additional_missing_after_source_prefix"]) for r in records),
            "additional_missing_after_source_prefix_occurrences": sum(len(r["additional_missing_after_source_prefix"]) for r in records),
            "source_prefix_gate_pass_diagnostic": sum(r["scorer_source_prefix_gate_diagnostic"]["gate_pass"] for r in records),
            "legacy_pass_but_source_prefix_fail": sum(r["legacy_gate"]["gate_pass"] and not r["scorer_source_prefix_gate_diagnostic"]["gate_pass"] for r in records),
            "all_categories_are_audit_only": True,
            "no_adjusted_gate_or_relabelled_success_rate": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--historical-csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--context-budget", type=int, default=8000)
    parser.add_argument("--old-source-budget", type=int, default=12000)
    args = parser.parse_args(argv)
    if args.context_budget < 0 or args.old_source_budget < 0:
        parser.error("budgets must be nonnegative")
    if args.out_dir.resolve() == args.dataset_dir.resolve() or args.dataset_dir.resolve() in args.out_dir.resolve().parents:
        parser.error("audit output must be outside the frozen dataset")
    manifest = json.loads(args.manifest.read_text())
    case_ids = manifest["case_ids"]
    if len(case_ids) != len(set(case_ids)) or len(case_ids) != manifest["n_selected"]:
        raise ValueError("manifest has duplicate IDs or wrong n_selected")
    root = args.dataset_dir / "cases" if (args.dataset_dir / "cases").is_dir() else args.dataset_dir
    cases = [read_case(root / case_id) for case_id in case_ids]
    if [case["case_id"] for case in cases] != case_ids:
        raise ValueError("pack case IDs disagree with the frozen manifest")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    reconciliation = reconcile(cases, args.historical_csv, args.context_budget)
    (args.out_dir / "historical_reconciliation.json").write_text(json.dumps(reconciliation, indent=2) + "\n")
    if not reconciliation["matches"]:
        # Invalidate any previous aggregate in a reused audit directory.
        (args.out_dir / "gate_audit.json").write_text(json.dumps({
            "tool": "audit_ppl_gate_v1", "evaluation_only": True,
            "status": "historical_reconciliation_failed", "summary": None,
            "historical_reconciliation": {k: v for k, v in reconciliation.items() if k != "comparisons"},
        }, indent=2) + "\n")
        print(f"Historical mismatch in {len(reconciliation['mismatches'])} rows; no classification/conclusions emitted.", file=sys.stderr)
        return 2
    inventory, digest = tree_inventory(args.dataset_dir)
    records = audit_cases(cases, args.context_budget, args.old_source_budget, args.out_dir)
    report = {"tool": "audit_ppl_gate_v1", "evaluation_only": True,
              "legacy_commit": LEGACY_COMMIT, "legacy_source_blobs": LEGACY_BLOBS,
              "manifest_sha256": sha256(args.manifest), "historical_csv_sha256": sha256(args.historical_csv),
              "audit_tool_sha256": sha256(Path(__file__)),
              "frozen_full_eligible_pool_id_digest": manifest.get("dataset_digest"),
              "full_eligible_pool_digest_revalidated": False,
              "digest_note": "The historical manifest digest hashes all 579 eligible case IDs, not pack contents; a 200-case subset cannot revalidate it.",
              "copied_dataset_content_sha256": digest, "input_file_sha256": inventory,
              "context_budget": args.context_budget, "scorer_old_source_budget": args.old_source_budget,
              "historical_reconciliation": {k: v for k, v in reconciliation.items() if k != "comparisons"},
              "summary": aggregate(records, args.old_source_budget), "cases": records,
              "limits": ["Regex triage is not C name resolution; candidate categories remain uncertain.",
                         "Exact kernel-side mention omissions do not prove semantic dependencies.",
                         "No model rerun, label correction, adjusted success threshold, or input augmentation.",
                         "Source-prefix gate is diagnostic, not a recomputation of PPL/model token-window behavior."]}
    (args.out_dir / "gate_audit.json").write_text(json.dumps(report, indent=2) + "\n")
    with (args.out_dir / "missing_identifiers.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("case_id", "identifier", "category", "confidence", "reason"))
        writer.writeheader()
        for record in records:
            for miss in record["missing"]:
                writer.writerow({"case_id": record["case_id"], **{key: miss[key] for key in writer.fieldnames if key != "case_id"}})
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
