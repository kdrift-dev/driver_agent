"""Content fingerprints and reproducibility records for evaluation runs.

Only explicitly selected case packs and repository source files are hashed.
Credentials and the process environment are never copied into metadata.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dataset import case_dirs

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIGEST_ALGORITHM = "sha256-casepack-content-v2"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def case_fingerprint(case_dir: Path) -> dict:
    """Hash every regular file in one pack, including paths and file boundaries."""
    files = {}
    for path in sorted(Path(case_dir).rglob("*")):
        if path.is_symlink():
            raise ValueError(f"case pack contains a symlink: {path}")
        if path.is_file():
            files[path.relative_to(case_dir).as_posix()] = _sha256(path.read_bytes())
    if not files or "case.json" not in files:
        raise ValueError(f"not a case pack: {case_dir}")
    digest = _sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
    return {"sha256": digest, "files": files}


def case_index(dataset_dir: Path) -> dict[str, Path]:
    result = {}
    for directory in case_dirs(Path(dataset_dir)):
        meta = json.loads((directory / "case.json").read_text())
        case_id = str(meta.get("case_id", directory.name))
        if case_id in result:
            raise ValueError(f"duplicate case_id: {case_id}")
        result[case_id] = directory
    return result


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], text=True,
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def code_identity(repo: Path = REPO_ROOT) -> dict:
    """Record HEAD plus content identity, including uncommitted executable sources."""
    files = {}
    patterns = ("pregen_driver_agent.py", "driver_agent_pregen/*.py", "driver_agent_pregen/*.json",
                "tools/*.py", "tools/ctxlab/**/*.py", "scripts/*.sh",
                "nix/*.nix", "flake.nix", "flake.lock")
    for pattern in patterns:
        for path in sorted(repo.glob(pattern)):
            if path.is_file():
                files[path.relative_to(repo).as_posix()] = _sha256(path.read_bytes())
    return {
        "commit": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "branch", "--show-current"),
        "tracked_worktree_dirty": bool(_git(repo, "status", "--porcelain", "--untracked-files=no")),
        "source_digest": _sha256(json.dumps(files, sort_keys=True).encode()),
        "source_files": files,
    }


def _safe_config(config) -> dict:
    def safe(key, value):
        if any(part in key.lower() for part in ("api_key", "password", "secret", "credential", "access_token")):
            return "<redacted>"
        if isinstance(value, dict):
            return {str(k): safe(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(key, v) for v in value]
        if isinstance(value, Path):
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)
    config = config if isinstance(config, dict) else vars(config)
    return {str(k): safe(str(k), v) for k, v in config.items()}


def write_run_metadata(out_root, args, cases, *, backend=None, extra=None) -> dict:
    """Write exact selected pack hashes, code/environment/model and kernel refs."""
    config = _safe_config(args)
    dataset_dir = Path(config["dataset_dir"])
    directories = case_index(dataset_dir)
    selected = {}
    for case in cases:
        cid = case["case_id"]
        if cid not in directories:
            raise ValueError(f"selected case absent from dataset: {cid}")
        selected[cid] = case_fingerprint(directories[cid])
    declared_refs = sorted({str(case.get("meta", {}).get(key))
                            for case in cases for key in ("old_ref", "new_ref", "commit")
                            if case.get("meta", {}).get(key)})
    kernel_repo = config.get("repo") or os.environ.get("REPO") or os.environ.get("LINUX_REPO")
    resolved = {}
    if kernel_repo and Path(kernel_repo).is_dir():
        for ref in declared_refs:
            resolved[ref] = _git(Path(kernel_repo), "rev-parse", "--verify", ref + "^{commit}")
    packages = {}
    for package in ("torch", "transformers", "openai", "tokenizers"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    manifest = config.get("manifest")
    metadata = {
        "schema_version": 2,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "code": code_identity(),
        "config": config,
        "environment": {"python": sys.version, "executable": sys.executable,
                        "platform": platform.platform(), "packages": packages},
        "backend": {"class": type(backend).__name__ if backend is not None else None,
                    "model": getattr(backend, "model_name", None),
                    "max_input_tokens": getattr(backend, "max_input_tokens", None),
                    "model_commit": getattr(getattr(getattr(backend, "_model", None), "config", None), "_commit_hash", None),
                    "tokenizer_commit": getattr(getattr(backend, "_tokenizer", None), "init_kwargs", {}).get("_commit_hash")},
        "kernel": {"repository": kernel_repo, "declared_refs": declared_refs,
                   "resolved_commits": resolved,
                   "checkout_commit": _git(Path(kernel_repo), "rev-parse", "HEAD") if kernel_repo else None},
        "manifest_sha256": _sha256(Path(manifest).read_bytes()) if manifest else None,
        "dataset_digest_algorithm": CONTENT_DIGEST_ALGORITHM,
        "selected_cases": selected,
        "selected_content_digest": _sha256(json.dumps(selected, sort_keys=True).encode()),
        "extra": _safe_config(extra or {}),
    }
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / "run_metadata.json"
    started_path = out_root / "run_metadata.started.json"
    if not started_path.exists():
        with started_path.open("x") as fh:
            fh.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    started = json.loads(started_path.read_text())
    metadata["integrity"] = {
        "initial_record": started_path.name,
        "initial_record_sha256": _sha256(started_path.read_bytes()),
        "selected_content_unchanged": started["selected_content_digest"] == metadata["selected_content_digest"],
        "code_unchanged": (started["code"]["source_digest"] == metadata["code"]["source_digest"]
                           and started["code"]["commit"] == metadata["code"]["commit"]),
        "manifest_unchanged": started["manifest_sha256"] == metadata["manifest_sha256"],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return metadata
