from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class HunkInfo:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str
    lines: List[str]

    def recalculate_counts(self) -> Tuple[int, int]:
        old_count = 0
        new_count = 0
        for line in self.lines:
            if not line:
                continue
            lead = line[0]
            if lead == " ":
                old_count += 1
                new_count += 1
            elif lead == "-":
                old_count += 1
            elif lead == "+":
                new_count += 1
        return old_count, new_count

    def header(self) -> str:
        old_count, new_count = self.recalculate_counts()
        old_part = f"-{self.old_start}" if old_count <= 1 else f"-{self.old_start},{old_count}"
        new_part = f"+{self.new_start}" if new_count <= 1 else f"+{self.new_start},{new_count}"
        ctx = f" {self.context}" if self.context else ""
        return f"@@ {old_part} {new_part} @@{ctx}"


@dataclass
class PatchFile:
    file_path: str
    header_lines: List[str]
    hunks: List[HunkInfo]


class PatchLineFixer:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s*(.*)$")

    def fix_patch(
        self,
        patch_content: str,
        file_contents: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str, List[str]]:
        msgs: List[str] = []
        patch_files = self.parse_patch(patch_content)
        if not patch_files:
            return False, patch_content, ["No diff hunks found in patch"]

        for pf in patch_files:
            content = None
            if file_contents:
                content = file_contents.get(pf.file_path)
            pf.hunks = self.fix_file_hunks(pf.hunks, content)
            msgs.append(f"fixed_hunks={len(pf.hunks)} file={pf.file_path}")

        rebuilt = self.rebuild_patch(patch_files)
        ok, vmsgs = self.validate_patch(rebuilt)
        msgs.extend(vmsgs)
        return ok, rebuilt, msgs

    def parse_patch(self, patch_content: str) -> List[PatchFile]:
        lines = patch_content.splitlines()
        out: List[PatchFile] = []
        cur: Optional[PatchFile] = None
        i = 0

        while i < len(lines):
            line = lines[i]
            if line.startswith("diff --git "):
                if cur:
                    out.append(cur)
                m = re.match(r"^diff --git a/(.*?) b/(.*?)$", line)
                if not m:
                    cur = None
                    i += 1
                    continue
                header = [line]
                file_path = m.group(2)
                i += 1
                while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("diff --git "):
                    header.append(lines[i])
                    i += 1
                cur = PatchFile(file_path=file_path, header_lines=header, hunks=[])
                continue

            if line.startswith("@@") and cur:
                hm = self.hunk_re.match(line)
                if hm:
                    old_start = int(hm.group(1))
                    old_count = int(hm.group(2) or "1")
                    new_start = int(hm.group(3))
                    new_count = int(hm.group(4) or "1")
                    ctx = (hm.group(5) or "").strip()
                    hlines: List[str] = []
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        if nxt.startswith("@@") or nxt.startswith("diff --git "):
                            i -= 1
                            break
                        if not nxt:
                            break
                        if nxt[0] in (" ", "+", "-"):
                            hlines.append(nxt)
                        else:
                            i -= 1
                            break
                        i += 1
                    cur.hunks.append(
                        HunkInfo(
                            old_start=old_start,
                            old_count=old_count,
                            new_start=new_start,
                            new_count=new_count,
                            context=ctx,
                            lines=hlines,
                        )
                    )
            i += 1

        if cur:
            out.append(cur)
        return out

    def fix_file_hunks(self, hunks: List[HunkInfo], original_content: Optional[str]) -> List[HunkInfo]:
        if not hunks:
            return []
        if original_content:
            return self._fix_hunks_with_original(hunks, original_content)

        fixed: List[HunkInfo] = []
        cumulative_offset = 0
        for idx, hunk in enumerate(hunks):
            old_count, new_count = hunk.recalculate_counts()
            hunk.old_count = old_count
            hunk.new_count = new_count
            if idx > 0:
                hunk.new_start = hunk.old_start + cumulative_offset
            cumulative_offset += new_count - old_count
            fixed.append(hunk)
        return fixed

    def _fix_hunks_with_original(self, hunks: List[HunkInfo], original_content: str) -> List[HunkInfo]:
        orig_lines = original_content.splitlines()
        fixed: List[HunkInfo] = []
        cumulative_offset = 0

        for idx, hunk in enumerate(hunks):
            pattern = [ln[1:] for ln in hunk.lines if ln and ln[0] in (" ", "-")]
            match_line = self._find_best_match(orig_lines, pattern, start_hint=max(0, hunk.old_start - 1))

            if match_line is not None:
                hunk = self._normalize_hunk_lines(hunk, orig_lines, match_line)
                hunk = self._add_context_lines(hunk, orig_lines, match_line)
                old_count, new_count = hunk.recalculate_counts()
                old_start = match_line + 1
                hunk.old_start = old_start
                hunk.old_count = old_count
                hunk.new_start = old_start + cumulative_offset
                hunk.new_count = new_count
                cumulative_offset += new_count - old_count
            else:
                old_count, new_count = hunk.recalculate_counts()
                hunk.old_count = old_count
                hunk.new_count = new_count
                if idx > 0:
                    hunk.new_start = hunk.old_start + cumulative_offset
                cumulative_offset += new_count - old_count

            fixed.append(hunk)
        return fixed

    def _find_best_match(self, orig_lines: List[str], pattern_lines: List[str], start_hint: int) -> Optional[int]:
        if not orig_lines or not pattern_lines:
            return None

        search_start = max(0, start_hint - 20)
        search_end = min(len(orig_lines), start_hint + 120)

        best_pos: Optional[int] = None
        best_score = 0.0
        for pos in range(search_start, search_end):
            if pos + len(pattern_lines) > len(orig_lines):
                break
            matches = 0
            for i, pat in enumerate(pattern_lines):
                if self._line_equal(orig_lines[pos + i], pat):
                    matches += 1
            score = matches / float(len(pattern_lines))
            if score > best_score:
                best_score = score
                best_pos = pos
            if score == 1.0:
                break

        if best_score >= 0.8:
            return best_pos
        return None

    @staticmethod
    def _line_equal(a: str, b: str) -> bool:
        return a == b or a.lstrip(" \t") == b.lstrip(" \t")

    def _normalize_hunk_lines(self, hunk: HunkInfo, orig_lines: List[str], match_pos: int) -> HunkInfo:
        normalized: List[str] = []
        idx = match_pos
        for line in hunk.lines:
            if line and line[0] in (" ", "-") and 0 <= idx < len(orig_lines):
                normalized.append(line[0] + orig_lines[idx])
                idx += 1
            else:
                normalized.append(line)
        hunk.lines = normalized
        return hunk

    def _add_context_lines(self, hunk: HunkInfo, orig_lines: List[str], match_pos: int, context_size: int = 3) -> HunkInfo:
        has_leading = bool(hunk.lines and hunk.lines[0].startswith(" "))
        has_trailing = bool(hunk.lines and hunk.lines[-1].startswith(" "))

        lines = list(hunk.lines)
        if not has_leading:
            cstart = max(0, match_pos - context_size)
            leading = [" " + orig_lines[i] for i in range(cstart, match_pos)]
            lines = leading + lines

        if not has_trailing:
            covered = 0
            for ln in hunk.lines:
                if ln and ln[0] in (" ", "-"):
                    covered += 1
            tstart = match_pos + covered
            tend = min(len(orig_lines), tstart + context_size)
            trailing = [" " + orig_lines[i] for i in range(tstart, tend)]
            lines.extend(trailing)

        hunk.lines = lines
        return hunk

    def rebuild_patch(self, patch_files: List[PatchFile]) -> str:
        out: List[str] = []
        for pf in patch_files:
            out.extend(pf.header_lines)
            for hunk in pf.hunks:
                out.append(hunk.header())
                out.extend(hunk.lines)
        patch = "\n".join(out)
        if patch and not patch.endswith("\n"):
            patch += "\n"
        return patch

    def validate_patch(self, patch_content: str) -> Tuple[bool, List[str]]:
        msgs: List[str] = []
        lines = patch_content.splitlines()
        in_hunk = False
        exp_old = exp_new = act_old = act_new = 0
        ok = True

        for idx, line in enumerate(lines, start=1):
            if line.startswith("@@"):
                if in_hunk:
                    if act_old != exp_old:
                        ok = False
                        msgs.append(f"hunk old_count mismatch before line {idx}: expected {exp_old}, got {act_old}")
                    if act_new != exp_new:
                        ok = False
                        msgs.append(f"hunk new_count mismatch before line {idx}: expected {exp_new}, got {act_new}")
                hm = self.hunk_re.match(line)
                if not hm:
                    ok = False
                    msgs.append(f"invalid hunk header at line {idx}")
                    in_hunk = False
                    continue
                exp_old = int(hm.group(2) or "1")
                exp_new = int(hm.group(4) or "1")
                act_old = 0
                act_new = 0
                in_hunk = True
                continue

            if not in_hunk:
                continue

            if line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("+++ "):
                if act_old != exp_old:
                    ok = False
                    msgs.append(f"hunk old_count mismatch at line {idx}: expected {exp_old}, got {act_old}")
                if act_new != exp_new:
                    ok = False
                    msgs.append(f"hunk new_count mismatch at line {idx}: expected {exp_new}, got {act_new}")
                in_hunk = False
                continue

            if not line:
                continue
            lead = line[0]
            if lead == " ":
                act_old += 1
                act_new += 1
            elif lead == "-":
                act_old += 1
            elif lead == "+":
                act_new += 1

        if in_hunk:
            if act_old != exp_old:
                ok = False
                msgs.append(f"last hunk old_count mismatch: expected {exp_old}, got {act_old}")
            if act_new != exp_new:
                ok = False
                msgs.append(f"last hunk new_count mismatch: expected {exp_new}, got {act_new}")

        if ok:
            msgs.append("patch validation successful")
        return ok, msgs


def fix_patch_line_numbers(
    patch_content: str,
    file_contents: Optional[Dict[str, str]] = None,
    verbose: bool = False,
) -> Tuple[bool, str, List[str]]:
    fixer = PatchLineFixer(verbose=verbose)
    return fixer.fix_patch(patch_content, file_contents=file_contents)
