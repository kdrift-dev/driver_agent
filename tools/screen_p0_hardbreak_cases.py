#!/usr/bin/env python3
"""Screen in-tree cases for P0 hard-break reproduction.

This is an evaluation/planning helper. It does not run Driver-Agent, does not
call an LLM, and does not generate patches. It stages old target module source
files into a prepared new-kernel worktree and checks whether the old source
still compiles against the new kernel.

Reference/new target-driver diffs are used only to build the candidate list and
to prioritize manual review candidates. They are not agent inputs.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from tools.list_qemu_testable_intree_cases import classify as classify_testability


SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".S", ".s"}


@dataclass
class CaseRow:
    module_path: str
    modified_loc: int
    code_loc: int
    source_file_count: int
    changed_files: int
    qemu_scope: str
    confidence: str
    test_target: str
    reason: str
    changed_file_samples: list[str]


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> dict[str, Any]:
    start = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    out = {
        "cmd": cmd,
        "cwd": str(cwd) if cwd else None,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_sec": round(time.time() - start, 3),
        "ok": proc.returncode == 0,
    }
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{proc.stderr[-4000:]}")
    return out


def truncate(text: str, limit: int = 12000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:] + "\n...[truncated]"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git_text(repo: Path, args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git failed: git -C {repo} {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def git_bytes(repo: Path, args: list[str], *, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git failed: git -C {repo} {' '.join(args)}\n{proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def source_files_at_ref(repo: Path, ref: str, module_path: str) -> list[str]:
    out = git_text(repo, ["ls-tree", "-r", "--name-only", ref, "--", module_path], check=False)
    return [line.strip() for line in out.splitlines() if Path(line.strip()).suffix in SOURCE_SUFFIXES]


def changed_files(repo: Path, old_ref: str, new_ref: str, module_path: str) -> list[str]:
    out = git_text(repo, ["diff", "--name-only", f"{old_ref}..{new_ref}", "--", module_path], check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def makefile_dirs(tree: Path, module_path: str, files: list[str]) -> list[str]:
    dirs: list[str] = []
    candidates = sorted({Path(p).parent.as_posix() for p in files} | {module_path.rstrip("/")})
    for rel in candidates:
        cur = tree / rel
        while cur != tree and tree in cur.parents:
            if (cur / "Makefile").exists() or (cur / "Kbuild").exists():
                out = cur.relative_to(tree).as_posix()
                if out not in dirs:
                    dirs.append(out)
                break
            cur = cur.parent
    return dirs or [module_path.rstrip("/")]


def prepare_worktree(repo: Path, ref: str, dest: Path) -> None:
    if dest.exists():
        run_cmd(["git", "-C", str(repo), "worktree", "remove", "-f", str(dest)], timeout=600)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
    run_cmd(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), ref], timeout=1200, check=True)


def restore_module(new_tree: Path, module_path: str) -> None:
    run_cmd(["git", "checkout", "--", module_path], cwd=new_tree, timeout=600)
    run_cmd(["git", "clean", "-fdx", "--", module_path], cwd=new_tree, timeout=600)


def stage_old_sources(repo: Path, old_ref: str, new_tree: Path, module_path: str) -> list[str]:
    files = source_files_at_ref(repo, old_ref, module_path)
    copied: list[str] = []
    for rel in files:
        content = git_bytes(repo, ["show", f"{old_ref}:{rel}"], check=False)
        if not content:
            continue
        dst = new_tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)
        copied.append(rel)
    return copied


def classify_compile_error(text: str) -> tuple[str, list[str]]:
    patterns = [
        ("missing_function_or_symbol", r"implicit declaration of function|undeclared \(first use|undefined!"),
        ("struct_field_or_type_change", r"has no member named|unknown type name|invalid use of undefined type"),
        ("signature_or_callback_mismatch", r"too few arguments|too many arguments|incompatible pointer type|from incompatible pointer type|initialization of .* from incompatible"),
        ("missing_header", r"fatal error: .*: No such file or directory"),
        ("kbuild_or_config_issue", r"No rule to make target|No such file or directory.*Makefile|CONFIG_"),
    ]
    matches: list[str] = []
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            matches.append(label)
    if not matches and text.strip():
        matches.append("unknown_compile_error")
    return (matches[0] if matches else "", matches)


def normalize_partial_module_compile_result(run: dict[str, Any]) -> dict[str, Any]:
    """Ignore modpost-only unresolved symbols from partial module builds.

    The P0 screen prepares the new kernel with modules_prepare, not a full
    vmlinux/modules build. In that environment Module.symvers is commonly
    absent, so modpost can report unresolved in-kernel symbols even when the
    old source still C-compiles. Treat only real compiler/header/API errors as
    P0 hard breaks.
    """
    if run.get("ok"):
        return run
    text = (run.get("stdout", "") or "") + "\n" + (run.get("stderr", "") or "")
    if "Module.symvers is missing" not in text or "ERROR: modpost:" not in text:
        return run
    for line in text.splitlines():
        lower = line.lower()
        if "fatal error:" in lower:
            return run
        if " error:" in lower and "error: modpost:" not in lower:
            return run
    out = dict(run)
    out["ok"] = True
    out["partial_module_compile_note"] = (
        "C compilation reached modpost; unresolved symbols ignored because "
        "Module.symvers is unavailable in the partial P0 screen worktree."
    )
    out["original_returncode"] = run.get("returncode")
    return out


def count_compiled_units(tree: Path, module_path: str) -> int:
    root = tree / module_path
    if not root.exists():
        return 0
    suffixes = (".o", ".ko", ".mod", ".mod.c")
    count = 0
    for path in root.rglob("*"):
        if path.is_file() and any(path.name.endswith(s) for s in suffixes):
            count += 1
    return count


def load_cases(args: argparse.Namespace, repo: Path) -> list[CaseRow]:
    rows: list[CaseRow] = []
    with Path(args.cases_csv).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            module = (row.get("module_path") or "").strip()
            if not module:
                continue
            if row.get("status") and row.get("status") != "existing":
                continue
            modified_loc = to_int(row.get("modified_loc"))
            code_loc = to_int(row.get("code_loc"))
            src_count = to_int(row.get("source_file_count"))
            ch_files = to_int(row.get("changed_files"))
            if modified_loc <= 0 or modified_loc > args.max_modified_loc:
                continue
            if code_loc <= 0 or code_loc > args.max_code_loc:
                continue
            if src_count <= 0 or src_count > args.max_source_files:
                continue
            if args.max_changed_files and ch_files > args.max_changed_files:
                continue

            files = changed_files(repo, args.old_ref, args.new_ref, module)
            scope, confidence, target, reason = classify_testability(module, files, str(repo), args.new_ref)
            if not args.include_hardware_required and scope == "hardware_required":
                continue
            if args.only_qemu and scope not in {"qemu_sufficient", "qemu_partial"}:
                continue

            rows.append(
                CaseRow(
                    module_path=module,
                    modified_loc=modified_loc,
                    code_loc=code_loc,
                    source_file_count=src_count,
                    changed_files=ch_files,
                    qemu_scope=scope,
                    confidence=confidence,
                    test_target=target,
                    reason=reason,
                    changed_file_samples=files[:12],
                )
            )

    scope_order = {"qemu_sufficient": 0, "qemu_partial": 1, "hardware_required": 2}
    conf_order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(
        key=lambda c: (
            scope_order.get(c.qemu_scope, 9),
            conf_order.get(c.confidence, 9),
            c.modified_loc,
            c.code_loc,
            c.module_path,
        )
    )
    if args.max_cases:
        rows = rows[: args.max_cases]
    return rows


def write_candidate_csv(path: Path, cases: list[CaseRow]) -> None:
    fields = [
        "module_path",
        "modified_loc",
        "code_loc",
        "source_file_count",
        "changed_files",
        "qemu_scope",
        "confidence",
        "test_target",
        "reason",
        "changed_file_samples",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in cases:
            writer.writerow(
                {
                    "module_path": c.module_path,
                    "modified_loc": c.modified_loc,
                    "code_loc": c.code_loc,
                    "source_file_count": c.source_file_count,
                    "changed_files": c.changed_files,
                    "qemu_scope": c.qemu_scope,
                    "confidence": c.confidence,
                    "test_target": c.test_target,
                    "reason": c.reason,
                    "changed_file_samples": ";".join(c.changed_file_samples),
                }
            )


def screen_cases(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    new_tree = out_dir / "worktrees" / f"new_{args.new_ref.replace('/', '_')}"

    cases = load_cases(args, repo)
    write_candidate_csv(out_dir / "screen_candidates.csv", cases)
    write_json(
        out_dir / "screen_config.json",
        {
            **vars(args),
            "candidate_count": len(cases),
            "note": "P0 screen only; new target driver diff is evaluation-only and not used by Driver-Agent.",
        },
    )

    print(f"[screen] candidates={len(cases)} out={out_dir}", flush=True)
    if not cases:
        write_json(out_dir / "summary.json", {"total": 0, "hard_break": 0, "builds": 0})
        return 0

    prepare_worktree(repo, args.new_ref, new_tree)
    prep_runs = [
        run_cmd(["make", args.config_target], cwd=new_tree, timeout=args.prepare_timeout_sec),
        run_cmd(["make", "olddefconfig"], cwd=new_tree, timeout=args.prepare_timeout_sec),
        run_cmd(["make", "modules_prepare"], cwd=new_tree, timeout=args.prepare_timeout_sec),
    ]
    prep_ok = all(r["ok"] for r in prep_runs)
    write_json(
        out_dir / "compile_prepare.json",
        [{**r, "stdout": truncate(r["stdout"]), "stderr": truncate(r["stderr"])} for r in prep_runs],
    )
    if not prep_ok:
        raise SystemExit("new kernel compile prepare failed; see compile_prepare.json")

    result_fields = [
        "idx",
        "module_path",
        "modified_loc",
        "code_loc",
        "source_file_count",
        "changed_files",
        "qemu_scope",
        "confidence",
        "test_target",
        "P0_status",
        "P0_hard_break",
        "compile_success",
        "compile_attempted",
        "compiled_unit_count",
        "failure_kind",
        "failure_labels",
        "compiled_dirs",
        "failed_dirs",
        "duration_sec",
        "stderr_excerpt",
    ]
    csv_path = out_dir / "p0_results.csv"
    json_rows: list[dict[str, Any]] = []
    hard_break_rows: list[dict[str, Any]] = []
    start_all = time.time()

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=result_fields)
        writer.writeheader()
        for idx, case in enumerate(cases, start=1):
            t0 = time.time()
            restore_module(new_tree, case.module_path)
            copied = stage_old_sources(repo, args.old_ref, new_tree, case.module_path)
            dirs = makefile_dirs(new_tree, case.module_path, copied)
            compile_runs: list[dict[str, Any]] = []
            failed_dirs: list[str] = []
            stderr_joined = ""
            success = True
            for rel_dir in dirs:
                run = run_cmd(
                    ["make", f"-j{max(1, args.jobs)}", f"M={rel_dir}", "modules"],
                    cwd=new_tree,
                    timeout=args.compile_timeout_sec,
                )
                run = normalize_partial_module_compile_result(run)
                compile_runs.append(run)
                stderr_joined += "\n" + run.get("stdout", "") + "\n" + run.get("stderr", "")
                if not run["ok"]:
                    success = False
                    failed_dirs.append(rel_dir)
                    if args.stop_after_first_dir_failure:
                        break

            failure_kind, labels = classify_compile_error(stderr_joined) if not success else ("", [])
            compiled_unit_count = count_compiled_units(new_tree, case.module_path)
            compile_attempted = compiled_unit_count > 0 or not success
            if not success:
                p0_status = "old_source_fails_on_new_kernel"
            elif compile_attempted:
                p0_status = "old_source_builds_on_new_kernel"
            else:
                p0_status = "no_compile_units"
            row = {
                "idx": idx,
                "module_path": case.module_path,
                "modified_loc": case.modified_loc,
                "code_loc": case.code_loc,
                "source_file_count": case.source_file_count,
                "changed_files": case.changed_files,
                "qemu_scope": case.qemu_scope,
                "confidence": case.confidence,
                "test_target": case.test_target,
                "P0_status": p0_status,
                "P0_hard_break": str(not success),
                "compile_success": str(success),
                "compile_attempted": str(compile_attempted),
                "compiled_unit_count": compiled_unit_count,
                "failure_kind": failure_kind,
                "failure_labels": ";".join(labels),
                "compiled_dirs": ";".join(dirs),
                "failed_dirs": ";".join(failed_dirs),
                "duration_sec": round(time.time() - t0, 3),
                "stderr_excerpt": truncate(stderr_joined, args.stderr_excerpt_chars).replace("\n", "\\n"),
            }
            writer.writerow(row)
            f.flush()

            detail = {
                **row,
                "reason": case.reason,
                "changed_file_samples": case.changed_file_samples,
                "copied_files": copied,
                "compile_runs": [
                    {**r, "stdout": truncate(r["stdout"]), "stderr": truncate(r["stderr"])}
                    for r in compile_runs
                ],
            }
            json_rows.append(detail)
            case_dir = out_dir / "case_details" / case.module_path.replace("/", "_")
            write_json(case_dir / "result.json", detail)
            if not success:
                hard_break_rows.append(detail)

            elapsed = max(time.time() - start_all, 1e-6)
            rate = idx / elapsed
            eta = (len(cases) - idx) / rate if rate > 0 else 0
            print(
                f"[progress] {idx}/{len(cases)} module={case.module_path} "
                f"status={row['P0_status']} units={compiled_unit_count} failure={failure_kind or '-'} "
                f"rate={rate:.2f}/s eta={eta/60:.1f}m",
                flush=True,
            )

    write_json(out_dir / "p0_results.json", json_rows)
    hard_csv = out_dir / "p0_hard_break_candidates.csv"
    with hard_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=result_fields + ["reason", "changed_file_samples"])
        writer.writeheader()
        for row in hard_break_rows:
            out = {k: row.get(k, "") for k in result_fields}
            out["reason"] = row.get("reason", "")
            out["changed_file_samples"] = ";".join(row.get("changed_file_samples") or [])
            writer.writerow(out)

    summary = {
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "total": len(cases),
        "hard_break": len(hard_break_rows),
        "builds": sum(1 for row in json_rows if row.get("P0_status") == "old_source_builds_on_new_kernel"),
        "no_compile_units": sum(1 for row in json_rows if row.get("P0_status") == "no_compile_units"),
        "hard_break_by_failure_kind": {},
        "qemu_scope_counts": {},
        "outputs": {
            "screen_candidates": str(out_dir / "screen_candidates.csv"),
            "p0_results": str(csv_path),
            "hard_break_candidates": str(hard_csv),
        },
    }
    for c in cases:
        summary["qemu_scope_counts"][c.qemu_scope] = summary["qemu_scope_counts"].get(c.qemu_scope, 0) + 1
    for row in hard_break_rows:
        key = row.get("failure_kind") or "unknown"
        summary["hard_break_by_failure_kind"][key] = summary["hard_break_by_failure_kind"].get(key, 0) + 1
    write_json(out_dir / "summary.json", summary)
    print("[done] " + json.dumps(summary, ensure_ascii=False), flush=True)
    if args.cleanup_worktree:
        run_cmd(["git", "-C", str(repo), "worktree", "remove", "-f", str(new_tree)], timeout=600)
        shutil.rmtree(new_tree, ignore_errors=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Screen old in-tree driver source against a new kernel for P0 hard breaks")
    p.add_argument("--repo", default="/mnt/disk7/jw_ai4os/linux_source_code/linux.git")
    p.add_argument("--cases-csv", required=True)
    p.add_argument("--old-ref", required=True)
    p.add_argument("--new-ref", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument(
        "--config-target",
        choices=["defconfig", "allmodconfig", "allyesconfig"],
        default="allmodconfig",
        help="kernel config target used before modules_prepare; allmodconfig avoids false PASS from disabled drivers",
    )
    p.add_argument("--max-cases", type=int, default=80)
    p.add_argument("--max-modified-loc", type=int, default=500)
    p.add_argument("--max-code-loc", type=int, default=8000)
    p.add_argument("--max-source-files", type=int, default=30)
    p.add_argument("--max-changed-files", type=int, default=0, help="0 disables this filter")
    p.add_argument("--only-qemu", action="store_true", help="keep only qemu_sufficient/qemu_partial candidates")
    p.add_argument("--include-hardware-required", action="store_true")
    p.add_argument("--prepare-timeout-sec", type=int, default=2400)
    p.add_argument("--compile-timeout-sec", type=int, default=900)
    p.add_argument("--stderr-excerpt-chars", type=int, default=12000)
    p.add_argument("--stop-after-first-dir-failure", action="store_true", default=True)
    p.add_argument("--cleanup-worktree", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return screen_cases(args)


if __name__ == "__main__":
    raise SystemExit(main())
