from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .utils import CommandError, run_cmd, tool_available


_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE: "OrderedDict[tuple, list['SearchHit']]" = OrderedDict()
_SEARCH_CACHE_MAX = 12000


def _search_cache_get(key: tuple) -> Optional[list["SearchHit"]]:
    with _SEARCH_CACHE_LOCK:
        val = _SEARCH_CACHE.get(key)
        if val is None:
            return None
        _SEARCH_CACHE.move_to_end(key)
        return [SearchHit(path=h.path, line=h.line, text=h.text, locator=h.locator) for h in val]


def _search_cache_put(key: tuple, value: list["SearchHit"]) -> None:
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE[key] = [SearchHit(path=h.path, line=h.line, text=h.text, locator=h.locator) for h in value]
        _SEARCH_CACHE.move_to_end(key)
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)


def _git_grep_threads() -> int | None:
    raw = os.getenv("DRIVER_AGENT_GIT_GREP_THREADS", "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    if val <= 0:
        return None
    return val


@dataclass
class SearchHit:
    path: str
    line: int
    text: str
    locator: str


@dataclass
class CommitRecord:
    commit: str
    subject: str


class RepositoryAccessor:
    """Unified accessor for git-ref mode and old/new tree fallback mode."""

    def __init__(
        self,
        *,
        repo: Optional[Path] = None,
        old_ref: Optional[str] = None,
        new_ref: Optional[str] = None,
        old_tree: Optional[Path] = None,
        new_tree: Optional[Path] = None,
    ) -> None:
        self.repo = repo
        self.old_ref = old_ref
        self.new_ref = new_ref
        self.old_tree = old_tree
        self.new_tree = new_tree
        self.mode = "git" if repo else "tree"

    @classmethod
    def from_args(
        cls,
        repo: Optional[str],
        old_ref: Optional[str],
        new_ref: Optional[str],
        old_tree: Optional[str],
        new_tree: Optional[str],
    ) -> "RepositoryAccessor":
        if repo:
            if not old_ref or not new_ref:
                raise ValueError("--repo mode requires both --old-ref and --new-ref")
            return cls(repo=Path(repo), old_ref=old_ref, new_ref=new_ref)

        if not old_tree or not new_tree:
            raise ValueError("tree mode requires both --old-tree and --new-tree")
        return cls(old_tree=Path(old_tree), new_tree=Path(new_tree))

    def validate(self) -> None:
        if self.mode == "git":
            assert self.repo is not None
            if not self.repo.exists():
                raise FileNotFoundError(f"repo path not found: {self.repo}")
            run_cmd(["git", "-C", str(self.repo), "rev-parse", "--git-dir"], check=True)
            # Ensure refs exist.
            run_cmd(["git", "-C", str(self.repo), "rev-parse", "--verify", self.old_ref], check=True)
            run_cmd(["git", "-C", str(self.repo), "rev-parse", "--verify", self.new_ref], check=True)
            return

        assert self.old_tree is not None and self.new_tree is not None
        if not self.old_tree.exists() or not self.old_tree.is_dir():
            raise FileNotFoundError(f"old tree not found: {self.old_tree}")
        if not self.new_tree.exists() or not self.new_tree.is_dir():
            raise FileNotFoundError(f"new tree not found: {self.new_tree}")

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "repo": str(self.repo) if self.repo else None,
            "old_ref": self.old_ref,
            "new_ref": self.new_ref,
            "old_tree": str(self.old_tree) if self.old_tree else None,
            "new_tree": str(self.new_tree) if self.new_tree else None,
        }

    def _side_to_ref_or_tree(self, side: str) -> str | Path:
        if side not in {"old", "new"}:
            raise ValueError(f"invalid side: {side}")
        if self.mode == "git":
            return self.old_ref if side == "old" else self.new_ref
        return self.old_tree if side == "old" else self.new_tree

    def module_path_exists(self, side: str, module_path: str) -> bool:
        target = self._side_to_ref_or_tree(side)
        if self.mode == "git":
            ref = str(target)
            try:
                run_cmd(
                    ["git", "-C", str(self.repo), "cat-file", "-e", f"{ref}:{module_path}"],
                    check=True,
                )
                return True
            except CommandError:
                # Directory objects may not be directly addressable in some contexts;
                # fallback to ls-tree prefix search.
                proc = run_cmd(
                    ["git", "-C", str(self.repo), "ls-tree", "--name-only", ref, "--", module_path],
                    check=False,
                )
                return bool(proc.stdout.strip())

        tree = Path(target)
        return (tree / module_path).exists()

    def list_module_files(self, side: str, module_path: str, extensions: Optional[set[str]] = None) -> list[str]:
        target = self._side_to_ref_or_tree(side)
        out: list[str] = []
        if self.mode == "git":
            ref = str(target)
            proc = run_cmd(
                ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", ref, "--", module_path],
                check=False,
            )
            for line in proc.stdout.splitlines():
                p = line.strip()
                if not p:
                    continue
                if extensions and Path(p).suffix.lower() not in extensions:
                    continue
                out.append(p)
            return sorted(out)

        tree = Path(target)
        root = tree / module_path
        if not root.exists():
            return []
        for cur, _, files in os.walk(root):
            for name in files:
                p = Path(cur) / name
                rel = p.relative_to(tree).as_posix()
                if extensions and p.suffix.lower() not in extensions:
                    continue
                out.append(rel)
        return sorted(out)

    def read_file(self, side: str, relative_path: str) -> str:
        target = self._side_to_ref_or_tree(side)
        if self.mode == "git":
            ref = str(target)
            proc = run_cmd(
                ["git", "-C", str(self.repo), "show", f"{ref}:{relative_path}"],
                check=True,
            )
            return proc.stdout

        tree = Path(target)
        return (tree / relative_path).read_text(encoding="utf-8", errors="replace")

    def path_exists(self, side: str, relative_path: str) -> bool:
        target = self._side_to_ref_or_tree(side)
        if self.mode == "git":
            ref = str(target)
            try:
                run_cmd(
                    ["git", "-C", str(self.repo), "cat-file", "-e", f"{ref}:{relative_path}"],
                    check=True,
                )
                return True
            except CommandError:
                return False

        tree = Path(target)
        return (tree / relative_path).exists()

    def search(
        self,
        side: str,
        pattern: str,
        *,
        regex: bool = True,
        fixed: bool = False,
        pathspecs: Optional[Iterable[str]] = None,
        max_hits: int = 80,
    ) -> list[SearchHit]:
        pathspecs = list(pathspecs or [])
        cache_key = (
            self.mode,
            str(self.repo) if self.repo else "",
            str(self._side_to_ref_or_tree(side)),
            pattern,
            bool(regex),
            bool(fixed),
            tuple(pathspecs),
            int(max_hits),
        )
        cached = _search_cache_get(cache_key)
        if cached is not None:
            return cached

        if self.mode == "git":
            ref = str(self._side_to_ref_or_tree(side))
            cmd = ["git", "-C", str(self.repo), "grep", "-n", "--full-name", "-I"]
            grep_threads = _git_grep_threads()
            if grep_threads:
                cmd.append(f"--threads={grep_threads}")
            if fixed:
                cmd.append("-F")
            elif regex:
                cmd.append("-E")
            cmd.append(pattern)
            cmd.append(ref)
            cmd.append("--")
            cmd.extend(pathspecs)
            proc = run_cmd(cmd, check=False)
            hits: list[SearchHit] = []
            for raw in proc.stdout.splitlines():
                # format: ref:path:line:text
                parts = raw.split(":", 3)
                if len(parts) < 4:
                    continue
                _, path, line_s, text = parts
                line = int(line_s) if line_s.isdigit() else 0
                hits.append(SearchHit(path=path, line=line, text=text, locator="regex"))
                if len(hits) >= max_hits:
                    break
            _search_cache_put(cache_key, hits)
            return hits

        tree = Path(self._side_to_ref_or_tree(side))
        cmd = None
        if tool_available("rg"):
            cmd = ["rg", "-n", "--no-heading"]
            if fixed:
                cmd.append("-F")
            elif regex:
                cmd.append("-e")
            cmd.append(pattern)
            cmd.append(str(tree))
        else:
            cmd = ["grep", "-R", "-n"]
            if fixed:
                cmd.append("-F")
            elif regex:
                cmd.append("-E")
            cmd.append(pattern)
            cmd.append(str(tree))

        proc = run_cmd(cmd, check=False)
        hits = []
        for raw in proc.stdout.splitlines():
            # format: /abs/path:line:text
            parts = raw.split(":", 2)
            if len(parts) < 3:
                continue
            abs_path, line_s, text = parts
            abs_p = Path(abs_path)
            if pathspecs:
                rel = abs_p.relative_to(tree).as_posix()
                matched = False
                for ps in pathspecs:
                    if rel.startswith(ps.rstrip("*")):
                        matched = True
                        break
                if not matched:
                    continue
            line = int(line_s) if line_s.isdigit() else 0
            rel_path = abs_p.relative_to(tree).as_posix()
            hits.append(SearchHit(path=rel_path, line=line, text=text, locator="ripgrep" if tool_available("rg") else "regex"))
            if len(hits) >= max_hits:
                break
        _search_cache_put(cache_key, hits)
        return hits

    def file_excerpt(self, side: str, relative_path: str, line: int, context: int = 20) -> str:
        try:
            text = self.read_file(side, relative_path)
        except Exception:
            return ""
        lines = text.splitlines()
        if not lines:
            return ""
        line = max(1, min(line, len(lines)))
        start = max(1, line - context)
        end = min(len(lines), line + context)
        out = []
        for idx in range(start, end + 1):
            out.append(f"{idx:5d}: {lines[idx - 1]}")
        return "\n".join(out)

    def refs_diff(self, pathspecs: Optional[Iterable[str]] = None, max_chars: int = 200000) -> str:
        if self.mode != "git":
            return ""
        pathspecs = list(pathspecs or [])
        cmd = [
            "git",
            "-C",
            str(self.repo),
            "diff",
            "--no-color",
            "--unified=3",
            f"{self.old_ref}",
            f"{self.new_ref}",
            "--",
        ]
        cmd.extend(pathspecs)
        proc = run_cmd(cmd, check=False)
        text = proc.stdout
        if len(text) > max_chars:
            return text[:max_chars] + "\n...[truncated]"
        return text

    def log_s(
        self,
        symbol: str,
        max_count: int = 8,
        *,
        exclude_path_prefixes: Optional[Iterable[str]] = None,
    ) -> list[CommitRecord]:
        if self.mode != "git":
            return []
        excludes: list[str] = []
        for raw in (exclude_path_prefixes or []):
            pref = self._normalize_prefix(raw)
            if pref:
                excludes.append(pref)
        scan_limit = max(max_count * 12, max_count + 8)
        cmd = [
            "git",
            "-C",
            str(self.repo),
            "log",
            f"{self.old_ref}..{self.new_ref}",
            "--pretty=format:%H%x01%s",
            f"--max-count={scan_limit}",
            "-S",
            symbol,
        ]
        proc = run_cmd(cmd, check=False)
        records = self._parse_commit_records(proc.stdout)
        records = self._filter_records_excluding_paths(records, excludes, max_count=max_count)
        if records:
            return records

        # fallback regex-sensitive search
        safe = re.escape(symbol)
        cmd_g = [
            "git",
            "-C",
            str(self.repo),
            "log",
            f"{self.old_ref}..{self.new_ref}",
            "--pretty=format:%H%x01%s",
            f"--max-count={scan_limit}",
            "-G",
            safe,
        ]
        proc_g = run_cmd(cmd_g, check=False)
        records_g = self._parse_commit_records(proc_g.stdout)
        return self._filter_records_excluding_paths(records_g, excludes, max_count=max_count)

    @staticmethod
    def _parse_commit_records(text: str) -> list[CommitRecord]:
        out: list[CommitRecord] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if "\x01" not in line:
                continue
            commit, subject = line.split("\x01", 1)
            out.append(CommitRecord(commit=commit.strip(), subject=subject.strip()))
        return out

    def commit_details(self, commit: str, max_diff_chars: int = 8000) -> dict:
        if self.mode != "git":
            return {
                "hash": commit,
                "subject": "",
                "message": "",
                "changed_files": [],
                "diff_excerpt": "",
            }

        msg_proc = run_cmd(
            [
                "git",
                "-C",
                str(self.repo),
                "show",
                "--no-color",
                "--pretty=format:%H%n%s%n%b",
                "--no-patch",
                commit,
            ],
            check=False,
        )
        lines = msg_proc.stdout.splitlines()
        commit_hash = lines[0].strip() if lines else commit
        subject = lines[1].strip() if len(lines) > 1 else ""

        body_lines = []
        for raw in lines[2:]:
            body_lines.append(raw)

        files_proc = run_cmd(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commit,
            ],
            check=False,
        )
        changed_files = [line.strip() for line in files_proc.stdout.splitlines() if line.strip()]

        diff_proc = run_cmd(
            ["git", "-C", str(self.repo), "show", "--no-color", "--unified=3", "--pretty=format:", commit],
            check=False,
        )
        diff_excerpt = diff_proc.stdout
        if len(diff_excerpt) > max_diff_chars:
            diff_excerpt = diff_excerpt[:max_diff_chars] + "\n...[truncated]"

        return {
            "hash": commit_hash,
            "subject": subject,
            "message": "\n".join(body_lines).strip(),
            "changed_files": changed_files,
            "diff_excerpt": diff_excerpt,
        }

    @staticmethod
    def _normalize_prefix(path: str) -> str:
        p = (path or "").strip().strip("/")
        return f"{p}/" if p else ""

    def _commit_touches_prefixes(self, commit: str, prefixes: list[str]) -> bool:
        if not prefixes:
            return False
        proc = run_cmd(
            [
                "git",
                "-C",
                str(self.repo),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commit,
            ],
            check=False,
        )
        for raw in proc.stdout.splitlines():
            path = raw.strip()
            if not path:
                continue
            for pref in prefixes:
                if path.startswith(pref):
                    return True
        return False

    def _filter_records_excluding_paths(
        self,
        records: list[CommitRecord],
        prefixes: list[str],
        *,
        max_count: int,
    ) -> list[CommitRecord]:
        if not records:
            return []
        out: list[CommitRecord] = []
        for rec in records:
            if prefixes and self._commit_touches_prefixes(rec.commit, prefixes):
                continue
            out.append(rec)
            if len(out) >= max_count:
                break
        return out
