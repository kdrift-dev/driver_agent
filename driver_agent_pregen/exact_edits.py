"""Compile strict exact edits against immutable old UTF-8 sources.

Only existing, safe repository-relative paths may change. Every old_text must
have exactly one occurrence in the original file, even counting overlapping
occurrences. Edits are simultaneous; one edit cannot create another's match.
LF is the only line separator. Unterminated final lines are supported and
represented with standard no-newline markers in the generated unified diff.
The v1 entry point requires complete lines; the explicit v2 entry point permits
exact substrings without changing, normalizing or inferring any supplied text.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

MAX_EDITS = 16
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["edits", "rationale", "notes"],
    "additionalProperties": False,
}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_sha(sources):
    return _sha(json.dumps(sources, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False))


def _text(value, label):
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if any((ord(char) < 32 and char not in "\t\n") or ord(char) == 127 for char in value):
        raise ValueError(f"{label} contains a forbidden control character; only LF and TAB are allowed")
    return value


def _path(value):
    _text(value, "path")
    # Restrict the protocol to paths representable without Git quoting. This
    # also rejects whitespace, shell metacharacters, absolute paths and drives.
    if (not re.fullmatch(r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*", value)
            or any(part in (".", "..", ".git") for part in value.split("/"))):
        raise ValueError("path must be a safe, unquoted repository-relative path")
    return value


def _fields(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(f"{label} must contain exactly {', '.join(keys)}")


def _lf_lines(text):
    if not text:
        return []
    lines = text.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def _diff(path, before, after):
    result = []
    for line in difflib.unified_diff(_lf_lines(before), _lf_lines(after),
                                     fromfile="a/" + path, tofile="b/" + path,
                                     n=3, lineterm="\n"):
        result.append(line)
        if not line.endswith("\n"):
            result.append("\n\\ No newline at end of file\n")
    return "".join(result)


def compile_edits(sources, output):
    """Compile v1 exact full-line edits, preserving the original contract.

    Invalid inputs raise ValueError. Results and validation behavior are shared
    with the substring compiler except for the v1 line-boundary requirements.
    """
    return _compile_edits(sources, output, whole_lines=True)


def compile_substring_edits(sources, output):
    """Compile v2 exact, unique, disjoint original substring replacements.

    No whitespace, Unicode, indentation or newline normalization occurs. A
    newline omitted from old_text remains in the untouched original suffix.
    Empty old_text and no-op edits remain forbidden.
    """
    return _compile_edits(sources, output, whole_lines=False)


def _compile_edits(sources, output, *, whole_lines):
    """Return deterministic patched sources, unified diff and provenance.

    Invalid inputs raise ValueError. No files are read or written and input
    objects are never mutated. Source digests cover canonical JSON of complete
    source payloads sorted by path. Diff and text digests cover UTF-8 bytes.
    Character offsets and byte offsets in the audit are zero-based, half-open.
    """
    if type(sources) is not list or not sources:
        raise ValueError("sources must be a nonempty list")
    original = {}
    for source in sources:
        _fields(source, ("path", "content"), "source")
        path = _path(source["path"])
        if path in original:
            raise ValueError("duplicate source path")
        original[path] = _text(source["content"], "source content")
    _fields(output, ("edits", "rationale", "notes"), "response")
    _text(output["rationale"], "rationale")
    if type(output["notes"]) is not list:
        raise ValueError("notes must be a list of strings")
    for note in output["notes"]:
        _text(note, "note")
    if type(output["edits"]) is not list or len(output["edits"]) > MAX_EDITS:
        raise ValueError(f"edits must be a list of at most {MAX_EDITS} edits")
    spans = {path: [] for path in original}
    for edit in output["edits"]:
        _fields(edit, ("path", "old_text", "new_text"), "edit")
        path = _path(edit["path"])
        if path not in original:
            raise ValueError("edit path is outside the supplied old sources")
        old = _text(edit["old_text"], "old_text")
        new = _text(edit["new_text"], "new_text")
        if not old:
            raise ValueError("old_text must be nonempty")
        if old == new:
            raise ValueError("no-op edit is forbidden")
        content = original[path]
        start = content.find(old)
        if start < 0:
            raise ValueError("old_text has no exact match in the original source")
        if content.find(old, start + 1) >= 0:
            raise ValueError("old_text is ambiguous in the original source")
        end = start + len(old)
        if whole_lines and ((start and content[start - 1] != "\n")
                or (end < len(content) and not old.endswith("\n"))):
            raise ValueError("old_text must cover complete original lines")
        if whole_lines and end < len(content) and new and not new.endswith("\n"):
            raise ValueError("new_text must end with LF before unchanged following content")
        spans[path].append((start, end, old, new))
    old_sources = [{"path": path, "content": original[path]} for path in sorted(original)]
    patched_sources, audits, diffs = [], [], []
    for source in old_sources:
        path, content = source["path"], source["content"]
        cursor, pieces = 0, []
        for start, end, old, new in sorted(spans[path]):
            if start < cursor:
                raise ValueError("edit spans overlap in the original source")
            pieces.extend((content[cursor:start], new))
            audits.append({
                "path": path,
                "start_char": start, "end_char": end,
                "start_byte": len(content[:start].encode("utf-8")),
                "end_byte": len(content[:end].encode("utf-8")),
                "start_line": content.count("\n", 0, start) + 1,
                "end_line": content.count("\n", 0, end) + (0 if old.endswith("\n") else 1),
                "old_text_sha256": _sha(old), "new_text_sha256": _sha(new),
            })
            cursor = end
        pieces.append(content[cursor:])
        patched = "".join(pieces)
        patched_sources.append({"path": path, "content": patched})
        if patched != content:
            diffs.append(_diff(path, content, patched))
    patch = "".join(diffs)
    if output["edits"] and not patch:
        raise ValueError("combined edits produce no source change")
    return {
        "status": "compiled" if output["edits"] else "no_edits",
        "patched_sources": patched_sources,
        "patch_diff": patch,
        "edits_audit": audits,
        "source_sha256": _source_sha(old_sources),
        "patched_source_sha256": _source_sha(patched_sources),
        "patch_sha256": _sha(patch),
    }
