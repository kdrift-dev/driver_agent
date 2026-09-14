#!/usr/bin/env python3
"""Adversarial contracts for the independent evaluation-only gate v2."""

from __future__ import annotations

import copy
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctxlab.gate_v2 import evaluate_gate, lex_c, sha256


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def diff(old, new):
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                       fromfile="a/x.c", tofile="b/x.c"))


def review(old, reference, judgments):
    return {"schema_version": 1, "case_id": "fixture", "evaluation_only": True,
            "input_sha256": {"old_source": sha256(old), "reference_diff": sha256(reference)},
            "review": {"reviewer": "adversarial fixture"},
            "judgments": [{"file": "x.c", "rationale": "Explicit adversarial fixture requirement.",
                           "evidence": [{"source": "reference_diff", "quote": reference}], **j}
                          for j in judgments]}


def main():
    lexical = lex_c('''#include <linux/not_an_identifier.h>
#include "nor/this.h"
/* hidden_from_comment */
int fn(void) {
 unsigned n = 0xFFu + 1e-3F + .5L + 0x1.fp+2 + 0b1010;
 char c = 'z';
 char *s = u8"hidden_from_string";
 return n;
}
''')
    ids = {t["text"] for t in lexical["tokens"] if t["kind"] == "identifier"}
    check("C literals, suffixes, comments and include paths are not identifiers",
          ids == {"fn", "n", "c", "s"}, str(sorted(ids)))
    check("defined is only a preprocessor operator inside a directive",
          any(t["text"] == "defined" and t["kind"] == "identifier" for t in lex_c("int defined(void);")["tokens"]))
    check("macro include names remain requirements", any(t["text"] == "HEADER" and t["kind"] == "identifier"
                                                          for t in lex_c("#include HEADER")["tokens"]))
    check("kernel bool/true/static_assert spellings remain identifiers, not assumed C23 keywords",
          {t["text"] for t in lex_c("bool flag = true; static_assert(flag);")["tokens"] if t["kind"] == "identifier"}
          == {"bool", "flag", "true", "static_assert"})
    check("unsupported line splicing is explicit", bool(lex_c("new_\\\napi();")["issues"]))

    old = "int f(void)\n{\n return 0;\n}\n"
    new = "int f(void)\n{\n return needed_api();\n}\n"
    reference = diff(old, new)
    annotation = review(old, reference, [{"symbol": "needed_api", "new_line": 3, "classification": "external"}])
    present = evaluate_gate("int needed_api(void);", old, reference, annotation=annotation)
    absent = evaluate_gate("", old, reference, annotation=annotation)
    unreviewed = evaluate_gate("int needed_api(void);", old, reference)
    check("confirmed external requirement passes with exact supplied evidence", present["status"] == "pass" and present["coverage"] == 1.0)
    check("removing actual evidence fails the same reviewed requirement", absent["status"] == "fail" and absent["coverage"] == 0.0)
    check("lexical presence never invents external ownership", unreviewed["status"] == "unknown"
          and unreviewed["coverage"] is None and unreviewed["review_scope"]["partial_review"])
    source_with_api = "int needed_api(void);\n" + old
    reference_with_api = diff(source_with_api, "int needed_api(void);\n" + new)
    source_review = review(source_with_api, reference_with_api,
                           [{"symbol": "needed_api", "new_line": 4, "classification": "external"}])
    source_available = evaluate_gate("", source_with_api, reference_with_api, annotation=source_review)
    check("reviewed external can be lexically available in exact old source",
          source_available["status"] == "pass" and source_available["covered_external"][0]["availability"] == "old_source_lexical")

    local_new = "int f(void)\n{\n int tmp = 1;\n return tmp;\n}\n"
    local_diff = diff(old, local_new)
    local = evaluate_gate("", old, local_diff)
    check("simple introduced local has an auditable block scope",
          len(local["local"]) == 2 and all(r.get("scope_proof") for r in local["local"]))
    check("no external requirements is NA with null coverage, never success",
          local["status"] == "not_applicable" and local["coverage"] is None)
    macro_shadow = evaluate_gate("#define tmp external_api", old, local_diff)
    check("known macro shadow blocks local exemption", macro_shadow["status"] == "unknown" and not macro_shadow["local"])

    conditional_old = "int f(void)\n{\n#if ENABLED\n return 0;\n#endif\n}\n"
    conditional_new = conditional_old.replace(" return 0;", " int tmp = 1;\n return tmp;")
    conditional = evaluate_gate("", conditional_old, diff(conditional_old, conditional_new))
    check("conditional branches refuse automatic scope proof", conditional["status"] == "unknown" and not conditional["local"])

    two_old = old + old.replace("f(void)", "g(void)")
    two_new = local_new.replace("tmp", "needed_api") + new.replace("f(void)", "g(void)")
    two_diff = diff(two_old, two_new)
    two_review = review(two_old, two_diff, [{"symbol": "needed_api", "new_line": 8, "classification": "external"}])
    collision = evaluate_gate("", two_old, two_diff, annotation=two_review)
    check("a local introduction cannot erase an external same-name use in another scope",
          collision["status"] == "fail" and len(collision["local"]) == 2 and len(collision["missing_external"]) == 1,
          collision["status"])
    misleading_context = evaluate_gate("int other(void) { int needed_api = 0; return needed_api; }",
                                       old, reference, annotation=annotation)
    check("context-local same-name declarations do not satisfy external evidence",
          misleading_context["status"] == "fail")

    member_context = evaluate_gate("struct object { int needed_api; };", old, reference, annotation=annotation)
    check("member-name matches do not prove external function availability",
          member_context["status"] == "unknown"
          and member_context["confirmed_external"][0]["availability"] == "unknown_member_or_tag_namespace")
    macro_context = evaluate_gate("#define needed_api unrelated_api", old, reference, annotation=annotation)
    check("macro-name collisions remain unresolved even when mentioned",
          macro_context["status"] == "unknown"
          and macro_context["confirmed_external"][0]["availability"] == "unknown_macro_shadow")

    conditional_context = evaluate_gate("#if FLAG\nint f(void) { int needed_api; }\n#endif",
                                        old, reference, annotation=annotation)
    check("conditional context cannot turn unavailable scope proof into external coverage",
          conditional_context["status"] == "unknown"
          and conditional_context["confirmed_external"][0]["availability"] == "unknown_preprocessor_scope")

    nested_new = "int f(void)\n{\n { int needed_api = 1; }\n return needed_api();\n}\n"
    nested_diff = diff(old, nested_new)
    nested_review = review(old, nested_diff, [{"symbol": "needed_api", "new_line": 4, "classification": "external"}])
    nested = evaluate_gate("", old, nested_diff, annotation=nested_review)
    check("local proof stops at nested block end", nested["status"] == "fail" and len(nested["local"]) == 1)

    member_new = "int f(void)\n{\n int member = 1;\n return obj.member + member;\n}\n"
    member = evaluate_gate("", old, diff(old, member_new))
    member_uses = [r for r in member["occurrences"] if r["symbol"] == "member" and r["new_line"] == 4]
    check("member namespace cannot inherit same-name variable proof",
          [r["classification"] for r in member_uses] == ["unknown", "local"])

    local_struct_new = "int f(void)\n{\n struct data { int member; };\n return 0;\n}\n"
    local_struct = evaluate_gate("", old, diff(old, local_struct_new))
    check("block-local struct fields are not mistaken for local variables",
          all(r["classification"] == "unknown" for r in local_struct["occurrences"] if r["symbol"] == "member"))
    ambiguous_local = evaluate_gate("int other(void) { type *needed_api; }", old, reference, annotation=annotation)
    check("unproven typedef/expression ambiguity is unknown, not missing evidence",
          ambiguous_local["status"] == "unknown"
          and ambiguous_local["confirmed_external"][0]["availability"] == "unknown_ambiguous_local_declaration")

    comment_old = "/*\n old_comment\n*/\n" + old
    comment_new = comment_old.replace("old_comment", "needed_api fake_symbol")
    comment = evaluate_gate("", comment_old, diff(comment_old, comment_new))
    check("comment state comes from reconstructed target, not isolated added lines",
          comment["status"] == "not_applicable" and not comment["occurrences"])
    broken_new = "/* unterminated\n" + old
    broken = evaluate_gate("", old, diff(old, broken_new))
    check("unterminated target lexical state is unknown", broken["status"] == "unknown")

    formfeed_old = "/* page\fseparator */\n" + old
    formfeed_new = "/* page\fseparator */\n" + new
    # Build with LF-only physical lines, as git does, rather than Python's
    # broader splitlines() interpretation of formfeed as a new source line.
    lf_lines = lambda text: [line + "\n" for line in text.split("\n")[:-1]]
    formfeed_diff = "".join(difflib.unified_diff(lf_lines(formfeed_old), lf_lines(formfeed_new),
                                                fromfile="a/x.c", tofile="b/x.c"))
    formfeed_review = review(formfeed_old, formfeed_diff, [{"symbol": "needed_api", "new_line": 4, "classification": "external"}])
    formfeed_result = evaluate_gate("int needed_api(void);", formfeed_old, formfeed_diff, annotation=formfeed_review)
    check("Git physical line numbers survive source formfeeds",
          formfeed_result["status"] == "pass" and not formfeed_result["input_issues"])

    truncated = evaluate_gate("int needed_api(void);", old[:10], reference, annotation=annotation)
    check("source truncation rejects stale review/scope proof", truncated["status"] == "unknown"
          and "annotation_old_source_hash_mismatch" in truncated["input_issues"])
    mismatch = evaluate_gate("int needed_api(void);", old[:10], reference, source_files={"x.c": old})
    check("hidden full source cannot override exact supplied source",
          mismatch["status"] == "unknown" and "source_files_disagree_with_exact_old_source" in mismatch["input_issues"])
    stale = copy.deepcopy(annotation)
    stale["input_sha256"]["reference_diff"] = "0" * 64
    check("stale reference annotations cannot produce a pass",
          evaluate_gate("int needed_api(void);", old, reference, annotation=stale)["status"] == "unknown")
    conflicting = copy.deepcopy(annotation)
    conflicting["judgments"].append(dict(conflicting["judgments"][0], classification="local"))
    check("conflicting occurrence judgments are unknown",
          evaluate_gate("int needed_api(void);", old, reference, annotation=conflicting)["status"] == "unknown")
    fake_local = review(old, reference, [{"symbol": "needed_api", "new_line": 3, "classification": "local"}])
    rejected_local = evaluate_gate("", old, reference, annotation=fake_local)
    check("review cannot globally subtract an unproven local name",
          rejected_local["status"] == "unknown"
          and rejected_local["review_scope"]["unsupported_review_occurrences"] == 1
          and rejected_local["review_scope"]["honored_review_occurrences"] == 0)

    include_new = '#include <linux/ghost_name.h>\n' + old
    include_result = evaluate_gate("", old, diff(old, include_new))
    check("header path changes alone have no identifier gate target",
          include_result["status"] == "not_applicable" and include_result["coverage"] is None)
    partial_new = new.replace("needed_api()", "needed_api(unreviewed_arg)")
    partial_diff = diff(old, partial_new)
    partial_annotation = review(old, partial_diff, [{"symbol": "needed_api", "new_line": 3, "classification": "external"}])
    partial = evaluate_gate("int needed_api(void); unreviewed_arg", old, partial_diff, annotation=partial_annotation)
    check("partial review stays unknown despite available reviewed external",
          partial["status"] == "unknown" and partial["review_scope"]["partial_review"])
    malformed = copy.deepcopy(annotation)
    malformed["judgments"][0]["evidence"] = ["not an evidence object"]
    check("malformed review evidence is rejected without scoring success",
          evaluate_gate("int needed_api(void);", old, reference, annotation=malformed)["status"] == "unknown")
    check("legacy expressibility gate stays unchanged",
          __import__("ctxlab.metrics", fromlist=["expressibility_check"]).expressibility_check(
              "", "", "+int n = 0xFFu;")["missing_identifiers"] == ["n", "xFFu"])
    print(f"\n{len(FAILURES)} failure(s)")
    return int(bool(FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
