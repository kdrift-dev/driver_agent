#!/usr/bin/env python3
"""Screen Kbuild module-target level hard-break cases.

This helper is for dataset construction/evaluation. It can use reference driver
path diffs or existing directory-level P0 results to choose candidate dirs, but
it only writes evaluation artifacts and does not feed target new-driver diffs to
Driver-Agent.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

SOURCE_SUFFIXES = {".c", ".h", ".S", ".s", ".rs"}
OBJ_RE = re.compile(r"^\s*obj-\$?\(?[A-Za-z0-9_!+\-]*\)?\s*(?::=|\+=|=)\s*(.*)$")
# Kbuild target composition can use target-y, target-objs, or
# target-$(CONFIG_FOO). Treat all of them as target-owned object lists.
COMP_RE_TEMPLATE = r"^\s*{name}-(?:y|m|objs|\$\([^)]+\)|\$[A-Za-z0-9_]+|[A-Za-z0-9_!+\-]+)\s*(?::=|\+=|=)\s*(.*)$"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, check: bool = False) -> dict[str, Any]:
    st = time.time()
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    out = {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": p.returncode, "ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr, "duration_sec": round(time.time() - st, 3)}
    if check and not out["ok"]:
        raise RuntimeError(json.dumps({**out, "stdout": p.stdout[-1000:], "stderr": p.stderr[-2000:]}, indent=2))
    return out


def git(repo: Path, args: list[str], check: bool = False) -> str:
    r = run(["git", "-C", str(repo), *args], timeout=600, check=check)
    return r["stdout"]


def exists(repo: Path, ref: str, path: str) -> bool:
    return run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"], timeout=60)["ok"]


def show(repo: Path, ref: str, path: str) -> str:
    r = run(["git", "-C", str(repo), "show", f"{ref}:{path}"], timeout=120)
    return r["stdout"] if r["ok"] else ""


def list_files(repo: Path, ref: str, path: str) -> list[str]:
    return [x for x in git(repo, ["ls-tree", "-r", "--name-only", ref, "--", path]).splitlines() if x]


def parse_obj_tokens(rhs: str) -> list[str]:
    rhs = rhs.split("#", 1)[0]
    out: list[str] = []
    for tok in re.split(r"\s+", rhs.strip()):
        tok = tok.strip().rstrip("\\")
        if not tok or tok.endswith("/"):
            continue
        if tok.endswith(".o") and "/" not in tok:
            out.append(tok[:-2])
    return out


def logical_makefile_lines(makefile_text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in makefile_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line and not current:
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        if current.strip():
            lines.append(current)
        current = ""
    if current.strip():
        lines.append(current)
    return lines


def parse_module_targets(makefile_text: str) -> list[str]:
    targets: list[str] = []
    for line in logical_makefile_lines(makefile_text):
        m = OBJ_RE.match(line)
        if not m:
            continue
        for name in parse_obj_tokens(m.group(1)):
            if name not in targets:
                targets.append(name)
    return targets


def parse_target_objects(makefile_text: str, target: str) -> list[str]:
    objs: list[str] = []
    cre = re.compile(COMP_RE_TEMPLATE.format(name=re.escape(target)))
    for line in logical_makefile_lines(makefile_text):
        m = cre.match(line)
        if not m:
            continue
        for name in parse_obj_tokens(m.group(1)):
            if name not in objs:
                objs.append(name)
    return objs or [target]


def target_source_files(repo: Path, ref: str, module_path: str, target: str) -> list[str]:
    mf = show(repo, ref, f"{module_path}/Makefile")
    objs = parse_target_objects(mf, target)
    paths: list[str] = []
    for obj in objs:
        for suffix in [".c", ".S", ".s", ".rs"]:
            rel = f"{module_path}/{obj}{suffix}"
            if exists(repo, ref, rel):
                paths.append(rel)
                break
    # Local headers are included because target C files often include private headers.
    for p in list_files(repo, ref, module_path):
        if Path(p).parent.as_posix() == module_path and Path(p).suffix == ".h":
            paths.append(p)
    # Keep old Makefile so direct target build uses the old composition.
    if exists(repo, ref, f"{module_path}/Makefile"):
        paths.append(f"{module_path}/Makefile")
    return sorted(dict.fromkeys(paths))


def prepare_worktree(repo: Path, ref: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        run(["git", "-C", str(repo), "worktree", "remove", "-f", str(path)], timeout=600)
        shutil.rmtree(path, ignore_errors=True)
    run(["git", "-C", str(repo), "worktree", "add", "--detach", str(path), ref], timeout=1200, check=True)


def write_file_from_git(repo: Path, ref: str, rel: str, dest_root: Path) -> None:
    out = run(["git", "-C", str(repo), "show", f"{ref}:{rel}"], timeout=120, check=True)["stdout"]
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8", errors="surrogateescape")


def stage_old_target(repo: Path, old_ref: str, new_tree: Path, module_path: str, target: str) -> list[str]:
    run(["git", "checkout", "--", module_path], cwd=new_tree, timeout=300)
    run(["git", "clean", "-fdx", "--", module_path], cwd=new_tree, timeout=300)
    files = target_source_files(repo, old_ref, module_path, target)
    for rel in files:
        write_file_from_git(repo, old_ref, rel, new_tree)
    return files


def classify(stderr: str) -> str:
    s = stderr
    labels: list[str] = []
    if "fatal error:" in s and "No such file" in s:
        labels.append("missing_header")
    if "implicit declaration of function" in s or "undeclared" in s:
        labels.append("missing_function_or_symbol")
    if "has no member named" in s or "no member named" in s:
        labels.append("struct_field_or_type_change")
    if "incompatible-pointer-types" in s or "conflicting types for" in s or "too many arguments" in s or "too few arguments" in s:
        labels.append("signature_or_callback_mismatch")
    return ";".join(dict.fromkeys(labels)) or "unknown_compile_error"


def truncate(s: str, n: int = 10000) -> str:
    return s if len(s) <= n else s[: n // 2] + "\n...[truncated]\n" + s[-n // 2 :]


def load_dirs(path: Path, limit: int) -> list[dict[str, str]]:
    rows = list(csv.DictReader(path.open()))
    out = []
    seen = set()
    for r in rows:
        m = r.get("module_path", "").strip()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    ap.add_argument("--dirs-csv", required=True)
    ap.add_argument("--old-ref", default="v5.0")
    ap.add_argument("--new-ref", default="v7.0")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-dirs", type=int, default=80)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--config-target", default="allmodconfig", choices=["defconfig", "allmodconfig", "allyesconfig"])
    ap.add_argument("--prepare-timeout-sec", type=int, default=2400)
    ap.add_argument("--compile-timeout-sec", type=int, default=600)
    ap.add_argument("--cleanup-worktree", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    new_tree = out_dir / "worktrees" / f"new_{args.new_ref}"
    dirs = load_dirs(Path(args.dirs_csv), args.max_dirs)

    candidates: list[dict[str, Any]] = []
    for row in dirs:
        module_path = row["module_path"]
        mf_old = show(repo, args.old_ref, f"{module_path}/Makefile")
        if not mf_old:
            continue
        for target in parse_module_targets(mf_old):
            srcs = target_source_files(repo, args.old_ref, module_path, target)
            c_srcs = [p for p in srcs if Path(p).suffix in {".c", ".S", ".s", ".rs"}]
            if not c_srcs:
                continue
            candidates.append({"module_path": module_path, "module_target": f"{target}.ko", "target_name": target, "source_files_old": srcs, "dir_row": row})
    (out_dir / "target_candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"[target-screen] dirs={len(dirs)} targets={len(candidates)} out={out_dir}", flush=True)

    prepare_worktree(repo, args.new_ref, new_tree)
    prep = [
        run(["make", args.config_target], cwd=new_tree, timeout=args.prepare_timeout_sec),
        run(["make", "olddefconfig"], cwd=new_tree, timeout=args.prepare_timeout_sec),
        run(["make", "modules_prepare"], cwd=new_tree, timeout=args.prepare_timeout_sec),
    ]
    (out_dir / "compile_prepare.json").write_text(json.dumps([{**r, "stdout": truncate(r["stdout"]), "stderr": truncate(r["stderr"])} for r in prep], indent=2), encoding="utf-8")
    if not all(r["ok"] for r in prep):
        raise SystemExit("compile prepare failed")

    fields = ["module_path", "module_target", "target_name", "source_file_count", "source_files_old", "P0_status", "P0_hard_break", "compile_success", "failure_kind", "duration_sec", "stderr_excerpt", "dir_failure_kind", "dir_qemu_scope", "dir_test_target"]
    rows_out: list[dict[str, Any]] = []
    hard: list[dict[str, Any]] = []
    for idx, c in enumerate(candidates, 1):
        t0 = time.time()
        module_path = c["module_path"]
        target = c["target_name"]
        copied = stage_old_target(repo, args.old_ref, new_tree, module_path, target)
        res = run(
            ["make", f"-j{args.jobs}", f"M={module_path}", "KBUILD_MODPOST_WARN=1", f"{target}.ko"],
            cwd=new_tree,
            timeout=args.compile_timeout_sec,
        )
        stderr_joined = res.get("stdout", "") + "\n" + res.get("stderr", "")
        ok = bool(res["ok"])
        row = {
            "module_path": module_path,
            "module_target": f"{target}.ko",
            "target_name": target,
            "source_file_count": len([p for p in copied if Path(p).suffix in SOURCE_SUFFIXES]),
            "source_files_old": ";".join(copied),
            "P0_status": "old_target_builds_on_new_kernel" if ok else "old_target_fails_on_new_kernel",
            "P0_hard_break": str(not ok),
            "compile_success": str(ok),
            "failure_kind": "" if ok else classify(stderr_joined),
            "duration_sec": round(time.time() - t0, 3),
            "stderr_excerpt": truncate(stderr_joined).replace("\n", "\\n"),
            "dir_failure_kind": c["dir_row"].get("failure_kind", ""),
            "dir_qemu_scope": c["dir_row"].get("qemu_scope", ""),
            "dir_test_target": c["dir_row"].get("test_target", ""),
        }
        rows_out.append(row)
        detail = {"row": row, "compile": {**res, "stdout": truncate(res["stdout"]), "stderr": truncate(res["stderr"])}}
        d = out_dir / "case_details" / f"{module_path.replace('/', '_')}__{target}.json"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(json.dumps(detail, indent=2), encoding="utf-8")
        if not ok:
            hard.append(row)
        print(f"[progress] {idx}/{len(candidates)} {module_path}:{target}.ko {row['P0_status']} failure={row['failure_kind'] or '-'}", flush=True)

    for name, data in [("target_p0_results.csv", rows_out), ("target_p0_hard_break_candidates.csv", hard)]:
        with (out_dir / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(data)
    summary = {
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "dirs": len(dirs),
        "targets": len(candidates),
        "hard_break_targets": len(hard),
        "builds": sum(1 for r in rows_out if r["compile_success"] == "True"),
        "by_failure_kind": {},
    }
    for r in hard:
        summary["by_failure_kind"][r["failure_kind"]] = summary["by_failure_kind"].get(r["failure_kind"], 0) + 1
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("[done] " + json.dumps(summary, ensure_ascii=False), flush=True)
    if args.cleanup_worktree:
        run(["git", "-C", str(repo), "worktree", "remove", "-f", str(new_tree)], timeout=600)
        shutil.rmtree(new_tree, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
