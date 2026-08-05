"""Unified-diff parsing shared by the miner and method zoo.

`parse_diff_changed_lines` is ported from
tools/build_candidate_small_dataset.py (same semantics, public name).
"""

from __future__ import annotations

import re
from typing import Any, Optional


def parse_diff_changed_lines(diff_text: str) -> dict[str, dict[str, set[int]]]:
    out: dict[str, dict[str, set[int]]] = {}
    old_path = None
    new_path = None
    old_ln = None
    new_ln = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git a/"):
            old_path = new_path = old_ln = new_ln = None
            continue
        if raw.startswith("--- "):
            tok = raw[4:].strip()
            old_path = None if tok == "/dev/null" else (tok[2:] if tok.startswith("a/") else tok)
            continue
        if raw.startswith("+++ "):
            tok = raw[4:].strip()
            new_path = None if tok == "/dev/null" else (tok[2:] if tok.startswith("b/") else tok)
            continue
        if raw.startswith("@@ "):
            try:
                mid = raw.split("@@", 2)[1].strip()
                a, b = mid.split(" ")[:2]
                old_ln = int(a.split(",")[0].lstrip("-"))
                new_ln = int(b.split(",")[0].lstrip("+"))
            except Exception:
                old_ln = new_ln = None
            continue
        if old_ln is None or new_ln is None:
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue
        if raw.startswith(" "):
            old_ln += 1
            new_ln += 1
        elif raw.startswith("-"):
            if old_path:
                out.setdefault(old_path, {"old": set(), "new": set()})["old"].add(old_ln)
            old_ln += 1
        elif raw.startswith("+"):
            if new_path:
                out.setdefault(new_path, {"old": set(), "new": set()})["new"].add(new_ln)
            new_ln += 1
    return out


def split_diff_by_file(diff_text: str) -> list[dict[str, Any]]:
    """Split a `git show` diff into per-file segments, flagging pure renames."""
    segments: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            if current:
                segments.append(current)
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            new_path = m.group(2) if m else ""
            current = {"path": new_path, "old_path": m.group(1) if m else "",
                       "lines": [line], "pure_rename": False, "has_hunks": False}
            continue
        if current is None:
            continue
        current["lines"].append(line)
        if line.startswith("similarity index 100%"):
            current["pure_rename"] = True
        if line.startswith("@@ "):
            current["has_hunks"] = True
    if current:
        segments.append(current)
    return segments


def changed_content_lines(segment_lines: list[str]) -> list[str]:
    out = []
    for line in segment_lines:
        if (line.startswith("+") and not line.startswith("+++")) or \
           (line.startswith("-") and not line.startswith("---")):
            out.append(line[1:])
    return out
