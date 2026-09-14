"""Opt-in, evaluation-only identifier requirement gate with explicit uncertainty.

This is a conservative lexical/scope diagnostic, not a C compiler, declaration
completeness proof, or production input. Reviewed reference-derived judgments
must never be passed to a context selector or patch-generation prompt.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from typing import Any

VERSION = "gate_v2.1"
_KEYWORDS = set("auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while _Alignas _Alignof _Atomic _Bool _Complex _Generic _Imaginary _Noreturn _Static_assert _Thread_local typeof __typeof__ __alignof__ __attribute__ __extension__".split())
_TYPES = set("char double float int long short signed unsigned void _Bool bool".split())
_QUALIFIERS = set("const volatile restrict static register auto".split())
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"(?:[0-9]|\.[0-9])(?:[A-Za-z_0-9.']|(?<=[eEpP])[+-])*")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lex_c(text: str) -> dict[str, Any]:
    """Tokenize relevant C lexical units, retaining source coordinates.

    Literals, comments and include filenames produce no identifier tokens.
    Unsupported line splicing and unterminated literals are surfaced, not
    silently treated as evidence that a missing requirement is absent.
    """
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    tokens, issues, directives = [], [], []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        line_index = bisect.bisect_right(starts, i) - 1
        line, column = line_index + 1, i - starts[line_index]
        if text.startswith("\\\n", i):
            issues.append({"kind": "unsupported_line_splice", "line": line})
            i += 2
            continue
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                issues.append({"kind": "unterminated_comment", "line": line})
                break
            i = end + 2
            continue
        if ch == "#" and not text[starts[line_index]:i].strip():
            match = re.match(r"#[ \t]*([A-Za-z_][A-Za-z0-9_]*)", text[i:])
            if match:
                directive = match.group(1)
                directives.append({"name": directive, "line": line})
                i += match.end()
                if directive in ("include", "include_next", "import"):
                    while i < len(text) and text[i] in " \t":
                        i += 1
                    if i < len(text) and text[i] == "<":
                        end = text.find(">", i + 1)
                        newline = text.find("\n", i)
                        if end < 0 or (newline >= 0 and newline < end):
                            issues.append({"kind": "unterminated_include", "line": line})
                            i = len(text) if newline < 0 else newline
                        else:
                            i = end + 1
                continue
        quote = re.match(r'(?:u8|u|U|L)?(["\'])', text[i:])
        if quote:
            delimiter = quote.group(1)
            end = i + quote.end()
            terminated = False
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                elif text[end] == delimiter:
                    end += 1
                    terminated = True
                    break
                elif text[end] == "\n":
                    break
                else:
                    end += 1
            if not terminated:
                issues.append({"kind": "unterminated_literal", "line": line})
            i = end
            continue
        number = _NUMBER.match(text, i)
        if number:
            i = number.end()
            continue
        identifier = _IDENTIFIER.match(text, i)
        if identifier:
            word = identifier.group()
            kind = "keyword" if word in _KEYWORDS or (word == "defined" and any(d["line"] == line for d in directives)) else "identifier"
            end = identifier.end()
        else:
            end = i + (2 if text[i:i + 2] in ("->", "++", "--", "&&", "||", "##") else 1)
            word, kind = text[i:end], "punctuation"
        tokens.append({"text": word, "kind": kind, "line": line, "column": column,
                       "offset": i, "end_offset": end})
        i = end
    return {"tokens": tokens, "issues": issues, "directives": directives}


def _physical_lines(text):
    # Git/C source coordinates count LF, not formfeed/vertical-tab separators.
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _parse_diff(reference_diff: str) -> dict[str, Any]:
    files, added, issues = {}, [], []
    old_path = new_path = None
    hunk = None
    for number, raw in enumerate(_physical_lines(reference_diff), 1):
        if raw.startswith("diff --git "):
            old_path = new_path = None
            hunk = None
        elif raw.startswith("--- ") and hunk is None:
            path = raw[4:].split("\t", 1)[0]
            old_path = path[2:] if path.startswith("a/") else path
        elif raw.startswith("+++ ") and hunk is None:
            path = raw[4:].split("\t", 1)[0]
            new_path = path[2:] if path.startswith("b/") else path
        elif _HUNK.match(raw):
            match = _HUNK.match(raw)
            if old_path is None or new_path is None:
                issues.append("hunk_without_file_headers")
                hunk = None
                continue
            hunk = {"old_start": int(match[1]), "old_count": int(match[2] or 1),
                    "new_start": int(match[3]), "new_count": int(match[4] or 1), "lines": []}
            entry = files.setdefault(new_path, {"old_path": old_path, "hunks": []})
            entry["hunks"].append(hunk)
        elif hunk is not None and raw[:1] in ("+", "-", " "):
            marker, text = raw[0], raw[1:]
            new_offset = sum(line[0] != "-" for line in hunk["lines"])
            if marker == "+":
                added.append({"file": new_path, "new_line": hunk["new_start"] + new_offset,
                              "diff_line": number, "text": text})
            hunk["lines"].append((marker, text))
        elif raw.startswith("\\ No newline at end of file"):
            continue
        elif raw.startswith(("--- ", "+++ ")):
            # Multiple plain unified diffs without git headers: no guessing.
            issues.append("ambiguous_file_headers")
    for entry in files.values():
        for h in entry["hunks"]:
            if (sum(marker != "+" for marker, _ in h["lines"]) != h["old_count"]
                    or sum(marker != "-" for marker, _ in h["lines"]) != h["new_count"]):
                issues.append("hunk_count_mismatch")
    if not files:
        issues.append("no_parseable_file_hunks")
    return {"files": files, "added_lines": added, "issues": sorted(set(issues))}


def _reconstruct(parsed, old_source_text, source_files):
    if source_files is not None:
        if not isinstance(source_files, dict) or any(not isinstance(p, str) or not isinstance(t, str) for p, t in source_files.items()):
            return {}, ["invalid_source_files_mapping"]
        combined = "\n\n".join(source_files[path] for path in sorted(source_files))
        if combined != old_source_text:
            return {}, ["source_files_disagree_with_exact_old_source"]
        sources = source_files
    elif len(parsed["files"]) == 1:
        sources = {next(iter(parsed["files"].values()))["old_path"]: old_source_text}
    else:
        return {}, ["multifile_source_map_required"]
    rebuilt, issues = {}, []
    for new_path, entry in parsed["files"].items():
        old_path = entry["old_path"]
        if old_path not in sources:
            issues.append(f"old_source_path_unavailable:{old_path}")
            continue
        original = _physical_lines(sources[old_path])
        output, cursor = [], 0
        valid = True
        for h in entry["hunks"]:
            start = h["old_start"] - 1 if h["old_count"] else h["old_start"]
            if start < cursor or start > len(original):
                valid = False
                break
            output.extend(original[cursor:start])
            cursor = start
            if len(output) != (h["new_start"] - 1 if h["new_count"] else h["new_start"]):
                valid = False
                break
            for marker, line in h["lines"]:
                if marker != "+":
                    if cursor >= len(original) or original[cursor] != line:
                        valid = False
                        break
                    cursor += 1
                if marker != "-":
                    output.append(line)
            if not valid:
                break
        if not valid:
            issues.append(f"reference_hunk_source_mismatch:{old_path}")
            continue
        output.extend(original[cursor:])
        rebuilt[new_path] = "\n".join(output)
    return rebuilt, issues


def _declaration_name(parts, reviewed=False):
    """Recognize a deliberately small, auditable declaration prefix."""
    if not parts or "extern" in parts or "typedef" in parts:
        return False
    words = [p for p in parts if p not in _QUALIFIERS and p != "*"]
    if words and all(word in _TYPES for word in words):
        return True
    if len(words) == 2 and words[0] in ("struct", "union", "enum"):
        return bool(_IDENTIFIER.fullmatch(words[1]))
    # A reviewer may identify a typedef, but scope still must be proven.
    return reviewed and len(words) == 1 and words[0] not in _KEYWORDS and bool(_IDENTIFIER.fullmatch(words[0]))


def _scope_proofs(lexed, macro_names):
    """Resolve simple block declarations without crossing scopes/namespaces.

    Conditional compilation, macro definitions, and other active directives
    disable this limited proof. Includes alone do not establish macro safety;
    known macro-name collisions from the supplied context also block a proof.
    """
    if lexed["issues"] or any(d["name"] not in ("include", "include_next") for d in lexed["directives"]):
        return {}
    tokens = lexed["tokens"]
    braces, parens, brace_end, paren_open = [], [], {}, {}
    for index, token in enumerate(tokens):
        if token["text"] == "{":
            braces.append(index)
        elif token["text"] == "}":
            if not braces:
                return {}
            brace_end[braces.pop()] = index
        elif token["text"] == "(":
            parens.append(index)
        elif token["text"] == ")":
            if not parens:
                return {}
            paren_open[index] = parens.pop()
    if braces or parens:
        return {}
    scopes, declarations, enclosing = [], [], {}
    for index, token in enumerate(tokens):
        text = token["text"]
        if text == "{":
            is_function = False
            if not scopes and index and tokens[index - 1]["text"] == ")":
                opening = paren_open.get(index - 1, -1)
                prefix = opening - 2
                while prefix >= 0 and tokens[prefix]["text"] not in (";", "}", "{"):
                    prefix -= 1
                return_type = [t["text"] for t in tokens[prefix + 1:opening - 1]]
                is_function = opening >= 1 and _declaration_name(return_type)
            prior = tokens[index - 1]["text"] if index else ""
            before = tokens[index - 2]["text"] if index > 1 else ""
            record_scope = prior in ("struct", "union", "enum") or before in ("struct", "union", "enum")
            active = (is_function or bool(scopes and scopes[-1][2])) and not record_scope
            scopes.append((index, brace_end[index], active))
        if scopes:
            enclosing[index] = scopes[-1]
        if text == "}" and scopes:
            scopes.pop()
        if token["kind"] != "identifier" or index not in enclosing or not enclosing[index][2]:
            continue
        if token["text"] in macro_names:
            continue
        previous = index - 1
        while previous >= 0 and tokens[previous]["text"] not in (";", "{", "}"):
            previous -= 1
        prefix = [t["text"] for t in tokens[previous + 1:index]]
        following = tokens[index + 1]["text"] if index + 1 < len(tokens) else ""
        if following not in ("=", ";", ","):
            continue
        automatic = _declaration_name(prefix)
        reviewed = _declaration_name(prefix, reviewed=True)
        if automatic or reviewed:
            scope_start, scope_end, _ = enclosing[index]
            declarations.append({"symbol": text, "index": index, "scope_start": scope_start,
                                 "scope_end": scope_end, "automatic": automatic,
                                 "declaration_line": token["line"],
                                 "declaration_prefix": prefix})
    proofs = {}
    for index, token in enumerate(tokens):
        if token["kind"] != "identifier" or token["text"] in macro_names:
            continue
        prior = tokens[index - 1]["text"] if index else ""
        following = tokens[index + 1]["text"] if index + 1 < len(tokens) else ""
        if prior in (".", "->", "struct", "union", "enum") or following == ":":
            continue
        visible = [d for d in declarations if d["symbol"] == token["text"]
                   and d["index"] <= index < d["scope_end"]]
        if visible:
            declaration = max(visible, key=lambda d: (d["scope_start"], d["index"]))
            proofs[(token["line"], token["column"])] = {
                "kind": "simple_block_declaration", "declaration_line": declaration["declaration_line"],
                "scope_start_line": tokens[declaration["scope_start"]]["line"],
                "scope_end_line": tokens[declaration["scope_end"]]["line"],
                "declaration_prefix": declaration["declaration_prefix"],
                "automatic": declaration["automatic"],
            }
    return proofs


def _member_mentions(lexed):
    """Locate member/tag namespaces that a plain name match cannot resolve."""
    tokens = lexed["tokens"]
    stack, result = [], set()
    for index, token in enumerate(tokens):
        prior = tokens[index - 1]["text"] if index else ""
        before = tokens[index - 2]["text"] if index > 1 else ""
        if token["text"] == "{":
            stack.append(prior in ("struct", "union", "enum") or before in ("struct", "union", "enum")
                         or bool(stack and stack[-1]))
        if token["kind"] == "identifier" and (prior in (".", "->", "struct", "union", "enum")
                                                  or (stack and stack[-1])):
            result.add(token["text"])
        if token["text"] == "}" and stack:
            stack.pop()
    return result


def _annotation_validation(annotation, old_source_text, reference_diff):
    if annotation is None:
        return [], []
    if not isinstance(annotation, dict):
        return [], ["annotation_not_an_object"]
    issues = []
    if annotation.get("schema_version", 1) != 1:
        issues.append("unsupported_annotation_schema")
    bound = annotation.get("input_sha256", {})
    if not isinstance(bound, dict):
        return [], ["annotation_input_hashes_not_an_object"]
    for key, text in (("old_source", old_source_text), ("reference_diff", reference_diff)):
        if bound.get(key) != sha256(text):
            issues.append(f"annotation_{key}_hash_mismatch")
    judgments = annotation.get("judgments", [])
    if not isinstance(judgments, list):
        return [], issues + ["annotation_judgments_not_a_list"]
    for j in judgments:
        if (not isinstance(j, dict) or j.get("classification") not in
                ("external", "local", "non_requirement", "unknown")
                or not j.get("rationale") or not j.get("evidence")
                or not isinstance(j.get("symbol"), str)):
            issues.append("malformed_annotation_judgment")
            continue
        if not isinstance(j["evidence"], list) or any(not isinstance(e, dict) for e in j["evidence"]):
            issues.append("malformed_annotation_evidence")
            continue
        for evidence in j["evidence"]:
            source = evidence.get("source")
            quote = evidence.get("quote")
            bound_text = {"old_source": old_source_text, "reference_diff": reference_diff}.get(source)
            if bound_text is not None and quote and quote not in bound_text:
                issues.append(f"annotation_evidence_quote_mismatch:{j['symbol']}")
    return ([] if issues else judgments), sorted(set(issues))


def evaluate_gate(context_text: str, old_source_text: str, reference_diff: str, *,
                  annotation: dict[str, Any] = None,
                  source_files: dict[str, str] = None) -> dict[str, Any]:
    """Evaluate the exact supplied context+source, with optional bound review.

    Occurrence judgments use symbol + file/new_line (or diff_line); occurrence
    is a zero-based index among that symbol on the line. A judgment without
    occurrence may cover homogeneous repeats on the same line. External
    availability remains lexical, not proof of external ownership. Unreviewed
    observed-old-source identifiers are reported separately and unresolved.
    """
    context_lex, old_lex = lex_c(context_text), lex_c(old_source_text)
    parsed = _parse_diff(reference_diff)
    rebuilt, reconstruction_issues = _reconstruct(parsed, old_source_text, source_files)
    judgments, annotation_issues = _annotation_validation(annotation, old_source_text, reference_diff)
    input_issues = parsed["issues"] + reconstruction_issues + annotation_issues
    context_ids = {t["text"] for t in context_lex["tokens"] if t["kind"] == "identifier"}
    old_ids = {t["text"] for t in old_lex["tokens"] if t["kind"] == "identifier"}
    macro_names = set(re.findall(r"(?m)^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)", context_text + "\n" + old_source_text))
    target_lex = {path: lex_c(text) for path, text in rebuilt.items()}
    target_proofs = {path: _scope_proofs(lexed, macro_names) for path, lexed in target_lex.items()}
    old_proofs = _scope_proofs(old_lex, macro_names)
    context_proofs = _scope_proofs(context_lex, macro_names)
    member_names = {"context": _member_mentions(context_lex), "old_source": _member_mentions(old_lex)}
    occurrences, matched, lexical_exclusions = [], set(), []
    for line in parsed["added_lines"]:
        path, new_line = line["file"], line["new_line"]
        full_lex = target_lex.get(path)
        if full_lex is not None:
            line_tokens = [t for t in full_lex["tokens"] if t["line"] == new_line and t["kind"] == "identifier"]
        else:
            # No cross-line scope/lexical claims if reconstruction is unavailable.
            fragment = lex_c(line["text"])
            line_tokens = [dict(t, line=new_line) for t in fragment["tokens"] if t["kind"] == "identifier"]
        counts = {}
        for token in line_tokens:
            symbol = token["text"]
            occurrence = counts.get(symbol, 0)
            counts[symbol] = occurrence + 1
            record = {"symbol": symbol, "file": path, "new_line": new_line,
                      "diff_line": line["diff_line"], "column": token["column"], "occurrence": occurrence,
                      "observed_old_source": symbol in old_ids,
                      "observed_context": symbol in context_ids,
                      "classification": "unknown", "provenance": "unreviewed"}
            proof = target_proofs.get(path, {}).get((new_line, token["column"]))
            if proof and proof["automatic"]:
                record.update(classification="local", provenance="automatic_scope_proof", scope_proof=proof)
            matches = []
            for index, judgment in enumerate(judgments):
                if judgment["symbol"] != symbol:
                    continue
                if judgment.get("file", path) != path:
                    continue
                if judgment.get("new_line", new_line) != new_line or judgment.get("diff_line", line["diff_line"]) != line["diff_line"]:
                    continue
                if judgment.get("occurrence", occurrence) != occurrence:
                    continue
                if "new_line" not in judgment and "diff_line" not in judgment:
                    continue
                matches.append((index, judgment))
            if len(matches) > 1:
                record.update(classification="unknown", provenance="conflicting_annotation_judgments")
                matched.update(index for index, _ in matches)
            elif matches:
                index, judgment = matches[0]
                matched.add(index)
                classification = judgment["classification"]
                record.update(provenance="reviewed_annotation", review=judgment)
                if classification == "local":
                    if proof:
                        record.update(classification="local", scope_proof=proof)
                    else:
                        record.update(classification="unknown", reason="reviewed_local_scope_unproven")
                elif classification == "non_requirement":
                    # Surviving C identifiers cannot be removed merely by a
                    # lexical label. Macro formals/pastes need expansion evidence.
                    record.update(classification="unknown", reason="reviewed_non_requirement_is_identifier")
                elif classification == "external" and proof:
                    record.update(classification="unknown", reason="reviewed_external_conflicts_with_local_scope")
                else:
                    record["classification"] = classification
            if record["classification"] == "external":
                old_occurrences = [t for t in old_lex["tokens"] if t["kind"] == "identifier" and t["text"] == symbol]
                old_unshadowed = any((t["line"], t["column"]) not in old_proofs for t in old_occurrences)
                context_occurrences = [t for t in context_lex["tokens"] if t["kind"] == "identifier" and t["text"] == symbol]
                context_unshadowed = any((t["line"], t["column"]) not in context_proofs for t in context_occurrences)
                if context_unshadowed:
                    availability = "context_lexical"
                elif old_unshadowed:
                    availability = "old_source_lexical"
                else:
                    availability = "missing"
                tentative_local = any(
                    proof and not proof["automatic"]
                    for lexed_tokens, proofs in ((context_occurrences, context_proofs), (old_occurrences, old_proofs))
                    for t in lexed_tokens for proof in [proofs.get((t["line"], t["column"]))])
                if availability == "missing" and tentative_local:
                    availability = "unknown_ambiguous_local_declaration"
                availability_lex = context_lex if availability == "context_lexical" else old_lex if availability == "old_source_lexical" else None
                if availability_lex is not None and any(
                        d["name"] not in ("include", "include_next") for d in availability_lex["directives"]):
                    availability = "unknown_preprocessor_scope"
                if symbol in macro_names:
                    availability = "unknown_macro_shadow"
                if ((availability == "context_lexical" and symbol in member_names["context"])
                        or (availability == "old_source_lexical" and symbol in member_names["old_source"])):
                    availability = "unknown_member_or_tag_namespace"
                if context_lex["issues"] or old_lex["issues"] or input_issues:
                    availability = "unknown_input_consistency"
                record["availability"] = availability
            occurrences.append(record)
    for index, judgment in enumerate(judgments):
        if index in matched:
            continue
        lines = [line for line in parsed["added_lines"]
                 if line["file"] == judgment.get("file", line["file"])
                 and line["new_line"] == judgment.get("new_line", line["new_line"])
                 and line["diff_line"] == judgment.get("diff_line", line["diff_line"])
                 and judgment["symbol"] in line["text"]]
        if judgment["classification"] == "non_requirement" and lines:
            lexical_exclusions.append(judgment)
        else:
            input_issues.append(f"annotation_occurrence_not_found:{index}:{judgment['symbol']}")
    external = [r for r in occurrences if r["classification"] == "external"]
    missing = [r for r in external if r.get("availability") == "missing"]
    available_kinds = ("context_lexical", "old_source_lexical")
    covered = [r for r in external if r.get("availability") in available_kinds]
    unknown = [r for r in occurrences if r["classification"] == "unknown" or
               (r["classification"] == "external" and r.get("availability") not in ("missing", *available_kinds))]
    lexical_issues = {"context": context_lex["issues"], "old_source": old_lex["issues"],
                      "target": {p: lexed["issues"] for p, lexed in target_lex.items() if lexed["issues"]}}
    uncertain_input = bool(input_issues or any(lexical_issues.values()))
    if missing and not uncertain_input:
        status = "fail"
    elif unknown or uncertain_input:
        status = "unknown"
    elif external:
        status = "pass"
    else:
        status = "not_applicable"
    reviewed_occurrences = [r for r in occurrences if r["provenance"] == "reviewed_annotation"]
    unsupported_reasons = {}
    for record in reviewed_occurrences:
        if record.get("reason"):
            unsupported_reasons[record["reason"]] = unsupported_reasons.get(record["reason"], 0) + 1
    return {"schema_version": 1, "gate_version": VERSION, "evaluation_only": True, "status": status,
            "coverage": len(covered) / len(external) if external else None,
            "coverage_scope": "confirmed_external_occurrences_with_unshadowed_context_or_source_lexical_mentions",
            "input_sha256": {"context": sha256(context_text), "old_source": sha256(old_source_text),
                             "reference_diff": sha256(reference_diff)},
            "annotation_sha256": sha256(json.dumps(annotation, sort_keys=True)) if annotation else None,
            "input_issues": sorted(set(input_issues)), "lexical_issues": lexical_issues,
            "source_reconstruction": {"complete": not parsed["issues"] and not reconstruction_issues,
                                      "reconstructed_files": sorted(rebuilt)},
            "occurrences": occurrences, "confirmed_external": external,
            "missing_external": missing, "covered_external": covered, "unknown": unknown,
            "local": [r for r in occurrences if r["classification"] == "local"],
            "observed_old_source": [r for r in occurrences if r["observed_old_source"]],
            "lexical_exclusion_annotations": lexical_exclusions,
            "review_scope": {"annotation_provided": annotation is not None,
                             "added_identifier_occurrences": len(occurrences),
                             "matched_review_judgments": len(matched), "review_judgments": len(judgments),
                             "honored_review_occurrences": sum(not r.get("reason") for r in reviewed_occurrences),
                             "unsupported_review_occurrences": sum(bool(r.get("reason")) for r in reviewed_occurrences),
                             "unsupported_review_reasons": unsupported_reasons,
                             "explicit_unknown_review_occurrences": sum(r.get("review", {}).get("classification") == "unknown" for r in reviewed_occurrences),
                             "conflicting_review_occurrences": sum(r["provenance"] == "conflicting_annotation_judgments" for r in occurrences),
                             "lexical_exclusion_review_judgments": len(lexical_exclusions),
                             "unreviewed_occurrences": sum("review" not in r for r in occurrences),
                             "unresolved_unreviewed_occurrences": sum(r["provenance"] == "unreviewed" for r in occurrences),
                             "automatically_resolved_occurrences": sum(r["provenance"] == "automatic_scope_proof" for r in occurrences),
                             "partial_review": any("review" not in r for r in occurrences)},
            "limitations": ["lexical availability is not external declaration ownership",
                            "unreviewed identifiers are not assumed external or irrelevant",
                            "scope proof supports simple C block declarations only",
                            "review-derived judgments are evaluation-only"]}
