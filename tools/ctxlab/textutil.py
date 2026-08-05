"""C-source text utilities shared by miners, metrics and methods.

Canonical home of the identifier/comment logic previously inlined in
tools/ppl_context_scorer.py (which now imports from here).
"""

from __future__ import annotations

import re

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "bool", "true", "false",
    "NULL", "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "size_t", "ssize_t", "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t", "__init", "__exit",
    "__user", "__iomem", "__must_check", "__maybe_unused", "__packed",
    "likely", "unlikely",
}

_LINE_COMMENT_RE = re.compile(r"//.*$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def identifiers(text: str) -> set[str]:
    return {tok for tok in IDENTIFIER_RE.findall(text) if tok not in C_KEYWORDS}


def strip_comments_and_strings(code: str) -> str:
    """Drop comment/string contents so prose words are not treated as identifiers.

    Line-local heuristic (diff added lines are seen out of context): also drops
    text after an unmatched ``/*`` or before an unmatched ``*/`` and leading
    ``*`` continuations of block comments.
    """
    code = _BLOCK_COMMENT_RE.sub(" ", code)
    out = []
    for line in code.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("*") or stripped.startswith("/*"):
            continue
        line = _LINE_COMMENT_RE.sub(" ", line)
        if "/*" in line:
            line = line.split("/*", 1)[0]
        if "*/" in line:
            line = line.split("*/", 1)[1]
        line = _STRING_RE.sub(" ", line)
        out.append(line)
    return "\n".join(out)
