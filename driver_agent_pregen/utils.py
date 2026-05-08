from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional


class CommandError(RuntimeError):
    """Raised when a shell command fails."""


def now_timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.strip().lower()
    ascii_text = re.sub(r"[^a-z0-9._-]+", "-", ascii_text)
    ascii_text = re.sub(r"-+", "-", ascii_text).strip("-")
    return ascii_text or "case"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path, default: Optional[Any] = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def run_cmd(
    cmd: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise CommandError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def which(name: str) -> Optional[str]:
    from shutil import which as _which

    return _which(name)


def tool_available(name: str) -> bool:
    return which(name) is not None


def dedupe_keep_order(items: Iterable[Any]) -> list[Any]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def safe_filename(name: str, suffix: str = "") -> str:
    base = slugify(name)
    if not base:
        base = "item"
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    return base + suffix


def relpath_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def dataclass_to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return obj


def extract_first_json_object(text: str) -> Optional[str]:
    """Extract the first balanced JSON object from a text blob."""
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def fenced_json_blocks(text: str) -> list[str]:
    pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
    return [m.group(1).strip() for m in pattern.finditer(text)]


def truncate_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def flatten_list(items: Iterable[Iterable[Any]]) -> list[Any]:
    out: list[Any] = []
    for sub in items:
        out.extend(sub)
    return out


def maybe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
