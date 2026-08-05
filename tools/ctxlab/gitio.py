"""Subprocess git access (ported from tools/build_candidate_small_dataset.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(repo: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo)] + args
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(cmd)}\n{proc.stderr}")
    return proc
