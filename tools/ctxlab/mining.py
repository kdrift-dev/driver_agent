"""Atomic-migration-commit mining: scan, gold labeling, case packs, catalog.

A commit that changes a kernel-side API and adapts driver callers in the same
change is self-labeling: kernel-side hunks are the gold context, driver-side
hunks are the reference edit, changed-but-unused symbols are distractors.
Everything produced here is evaluation-only (`evaluation_only: true`).

Case-pack layout is pinned to tools/run_ppl_signal_experiment.py::load_case.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .diffs import changed_content_lines, parse_diff_changed_lines, split_diff_by_file
from .gitio import run_git
from .textutil import C_KEYWORDS, identifiers, strip_comments_and_strings

MINER_VERSION = "mine_migration_bench_v1"

# symbols too generic to be useful labels even when they appear in both diffs
SYMBOL_STOPLIST = C_KEYWORDS | {
    "GFP_KERNEL", "GFP_NOWAIT", "GFP_ATOMIC", "EXPORT_SYMBOL",
    "EXPORT_SYMBOL_GPL", "MODULE_LICENSE", "MODULE_AUTHOR",
    "MODULE_DESCRIPTION", "EINVAL", "ENOMEM", "ENODEV", "EBUSY", "ERR_PTR",
    "IS_ERR", "PTR_ERR", "WARN_ON", "BUG_ON", "pr_err", "pr_warn",
    "pr_info", "pr_debug", "dev_err", "dev_warn", "dev_info", "dev_dbg",
    # ubiquitous kernel types/structs: appear in nearly every diff pair and
    # would label almost any case as gold — useless as context-value signal
    "device", "platform_device", "pci_dev", "module", "kobject",
    "list_head", "mutex", "spinlock", "spinlock_t", "work_struct",
    "delayed_work", "timer_list", "completion", "attribute",
    "device_node", "of_device_id", "file", "inode", "page", "sk_buff",
    "net_device", "resource", "regmap", "clk", "irqreturn_t", "notifier_block",
}

DEFINE_RE = re.compile(r"#\s*define\s+([A-Za-z_]\w*)")
STRUCT_RE = re.compile(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)")
CALLABLE_RE = re.compile(r"\b([A-Za-z_]\w{2,})\s*\(")
ENUM_CONST_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


# ---------------------------------------------------------------------------
# commit scan
# ---------------------------------------------------------------------------

def scan_candidate_commits(repo: Path, old_ref: str, new_ref: str,
                           kernel_prefixes: list[str], driver_prefixes: list[str]) -> list[dict[str, Any]]:
    """One git call: commits whose file set touches BOTH prefix groups."""
    # %x01/%x02 are expanded to control bytes by git itself; the argv string
    # stays plain ASCII (an actual NUL in argv is rejected by exec)
    proc = run_git(repo, [
        "log", "--no-merges", "--name-only", "--pretty=format:%x01%H%x02%s",
        f"{old_ref}..{new_ref}",
    ], check=True)
    candidates = []
    sha = subject = None
    files: list[str] = []

    def flush() -> None:
        if sha is None:
            return
        kern = [f for f in files if any(f.startswith(p) for p in kernel_prefixes)]
        drv = [f for f in files if any(f.startswith(p) for p in driver_prefixes) and f.endswith(".c")]
        if kern and drv:
            candidates.append({"sha": sha, "subject": subject, "kernel_files": kern, "driver_files": drv})

    for line in proc.stdout.splitlines():
        if line.startswith("\x01"):
            flush()
            sha, _, subject = line[1:].partition("\x02")
            files = []
        elif line.strip():
            files.append(line.strip())
    flush()
    return candidates


# ---------------------------------------------------------------------------
# symbol extraction from kernel-side hunks
# ---------------------------------------------------------------------------

def kernel_symbols_from_segments(kernel_segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """symbol -> {paths, sample lines} extracted from changed kernel lines."""
    symbols: dict[str, dict[str, Any]] = {}

    def note(sym: str, path: str, text: str) -> None:
        if sym in SYMBOL_STOPLIST or len(sym) < 4:
            return
        rec = symbols.setdefault(sym, {"paths": [], "lines": []})
        if path not in rec["paths"]:
            rec["paths"].append(path)
        pair = (path, text.rstrip())
        if len(rec["lines"]) < 6 and text.strip() and pair not in rec["lines"]:
            rec["lines"].append(pair)

    for seg in kernel_segments:
        for raw in changed_content_lines(seg["lines"]):
            code = strip_comments_and_strings(raw)
            for regex in (DEFINE_RE, STRUCT_RE, CALLABLE_RE, ENUM_CONST_RE):
                for m in regex.finditer(code):
                    note(m.group(1), seg["path"], raw)
    return symbols


def symbol_definition_diff(symbol: str, kernel_segments: list[dict[str, Any]], max_lines: int = 60) -> str:
    """Hunks (with headers) from the kernel segments that mention the symbol."""
    out: list[str] = []
    for seg in kernel_segments:
        hunk: list[str] = []
        keep = False
        for line in seg["lines"]:
            if line.startswith("@@ "):
                if keep:
                    out.extend([f"--- {seg['path']}"] + hunk)
                hunk, keep = [line], False
            elif hunk:
                hunk.append(line)
            if symbol in line:
                keep = True
        if keep:
            out.extend([f"--- {seg['path']}"] + hunk)
    return "\n".join(out[:max_lines])


# ---------------------------------------------------------------------------
# per-commit case extraction
# ---------------------------------------------------------------------------

def extract_cases_for_commit(repo: Path, cand: dict[str, Any], args: argparse.Namespace,
                             rng: random.Random) -> list[dict[str, Any]]:
    sha = cand["sha"]
    show = run_git(repo, ["show", "-M", "--no-color", "--format=", sha])
    if show.returncode != 0:
        return []
    parent = run_git(repo, ["rev-parse", f"{sha}^"], check=True).stdout.strip()
    segments = split_diff_by_file(show.stdout)

    kernel_segments = [s for s in segments
                       if any(s["path"].startswith(p) for p in args.kernel_prefix_list)
                       and s["has_hunks"] and not s["pure_rename"]]
    driver_segments = [s for s in segments
                       if any(s["path"].startswith(p) for p in args.driver_prefix_list)
                       and s["path"].endswith(".c") and s["has_hunks"] and not s["pure_rename"]]
    if not kernel_segments or not driver_segments:
        return []

    kernel_symbols = kernel_symbols_from_segments(kernel_segments)
    if not kernel_symbols:
        return []
    treewide_scale = len(driver_segments)

    if len(driver_segments) > args.max_cases_per_commit:
        driver_segments = rng.sample(driver_segments, args.max_cases_per_commit)

    cases = []
    for seg in driver_segments:
        changed = changed_content_lines(seg["lines"])
        code_lines = [ln for ln in strip_comments_and_strings("\n".join(changed)).splitlines()
                      if ln.strip()]
        if not (args.min_caller_changed_lines <= len(code_lines) <= args.max_caller_changed_lines):
            continue
        caller_tokens = identifiers("\n".join(code_lines))
        gold = sorted(sym for sym in kernel_symbols if sym in caller_tokens)
        distractors = sorted(set(kernel_symbols) - set(gold))
        old_source = run_git(repo, ["show", f"{sha}^:{seg['old_path'] or seg['path']}"])
        if old_source.returncode != 0:  # file created by this commit: no migration
            continue
        reference_diff = "\n".join(seg["lines"]) + "\n"
        # gold invariant: symbol must be present in both diffs by construction;
        # verify against the raw texts anyway (hard fail = extraction bug)
        kernel_text = "\n".join("\n".join(s["lines"]) for s in kernel_segments)
        for sym in gold:
            if sym not in kernel_text or sym not in reference_diff:
                raise AssertionError(f"gold invariant violated: {sym} in {sha} {seg['path']}")
        cases.append({
            "sha": sha, "parent": parent, "subject": cand["subject"],
            "driver_file": seg["path"], "old_driver_path": seg["old_path"] or seg["path"],
            "reference_diff": reference_diff, "old_source": old_source.stdout,
            "gold": gold, "distractors": distractors,
            "kernel_symbols_info": kernel_symbols, "kernel_segments": kernel_segments,
            "caller_changed_lines": len(code_lines), "treewide_scale": treewide_scale,
        })
    return cases


# ---------------------------------------------------------------------------
# case-pack writing
# ---------------------------------------------------------------------------

def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")


def _evidence_items(symbols: list[str], info: dict[str, dict[str, Any]],
                    kernel_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for sym in symbols:
        rec = info.get(sym, {})
        items.append({
            "symbol": sym,
            "definition_diff": symbol_definition_diff(sym, kernel_segments),
            "new_locations": [{"path": p, "line": 0, "text": t}
                              for p, t in rec.get("lines", [])]
                             or [{"path": p, "line": 0, "text": ""} for p in rec.get("paths", [])],
        })
    return items


def write_case_pack(case: dict[str, Any], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    from driver_agent_pregen.symbol_scanner import _extract_c_like_functions

    case_id = f"{case['sha'][:12]}__{_slug(case['driver_file'])}"
    case_dir = out_dir / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    (case_dir / "reference.diff").write_text(case["reference_diff"])
    kernel_diff = "\n".join("\n".join(s["lines"]) for s in case["kernel_segments"]) + "\n"
    (case_dir / "kernel_side.diff").write_text(kernel_diff)
    src_path = case_dir / "old_module_sources" / case["old_driver_path"]
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(case["old_source"])

    evidence = {
        "gold": _evidence_items(case["gold"], case["kernel_symbols_info"], case["kernel_segments"]),
        "distractors": _evidence_items(case["distractors"], case["kernel_symbols_info"],
                                       case["kernel_segments"]),
    }
    (case_dir / "kernel_evidence.json").write_text(json.dumps(evidence, indent=1))

    funcs = [(f.name, int(f.start_line), int(f.end_line))
             for f in _extract_c_like_functions(case["old_source"], case["old_driver_path"])]
    changed = parse_diff_changed_lines(case["reference_diff"])
    old_lines = changed.get(case["old_driver_path"], {}).get("old", set())
    hit_functions = sorted({name for name, start, end in funcs
                            for ln in old_lines if start <= ln <= end})

    meta = {
        "case_id": case_id,
        "commit": case["sha"], "parent": case["parent"], "subject": case["subject"],
        "old_ref": f"{case['sha']}^", "new_ref": case["sha"],
        "module_file": case["driver_file"],
        "module_path": str(Path(case["driver_file"]).parent),
        "kernel_symbols": case["gold"],
        "distractor_symbols": case["distractors"],
        "caller_changed_lines": case["caller_changed_lines"],
        "caller_changed_functions": hit_functions,
        "treewide_scale": case["treewide_scale"],
        "mining": {
            "tool": MINER_VERSION, "mode": args.mode,
            "scan_range": f"{args.old_ref}..{args.new_ref}",
            "kernel_prefixes": args.kernel_prefix_list,
            "driver_prefixes": args.driver_prefix_list,
            "min_caller_changed_lines": args.min_caller_changed_lines,
            "max_caller_changed_lines": args.max_caller_changed_lines,
            "max_cases_per_commit": args.max_cases_per_commit,
            "seed": args.seed,
        },
        "evaluation_only": True,
        "reference_diff_policy": "evaluation-only; must never enter the agent pipeline",
    }
    (case_dir / "case.json").write_text(json.dumps(meta, indent=1))
    return meta


# ---------------------------------------------------------------------------
# sanity gauges + catalog
# ---------------------------------------------------------------------------

def apply_check_sample(cases_meta: list[dict[str, Any]], out_dir: Path,
                       rng: random.Random, sample_size: int = 10) -> tuple[int, int]:
    """`git apply --check` reference.diff against the exported old source."""
    sample = rng.sample(cases_meta, min(sample_size, len(cases_meta)))
    ok = 0
    for meta in sample:
        case_dir = out_dir / "cases" / meta["case_id"]
        with tempfile.TemporaryDirectory() as tmp:
            found = False
            for src in (case_dir / "old_module_sources").rglob("*.c"):
                rel = src.relative_to(case_dir / "old_module_sources")
                dst = Path(tmp) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(errors="replace"))
                found = True
            if not found:
                continue
            proc = subprocess.run(
                ["git", "apply", "--check", str((case_dir / "reference.diff").resolve())],
                cwd=tmp, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode == 0:
                ok += 1
            else:
                print(f"[miner] apply --check FAILED for {meta['case_id']}: "
                      f"{(proc.stderr or '').strip().splitlines()[:1]}")
    return ok, len(sample)


def write_catalog(cases_meta: list[dict[str, Any]], candidates: int, scanned_range: str,
                  out_dir: Path, apply_ok: tuple[int, int], rng: random.Random) -> dict[str, Any]:
    n = len(cases_meta)
    zero_gold = sum(1 for m in cases_meta if not m["kernel_symbols"])
    by_subsystem: dict[str, int] = {}
    for m in cases_meta:
        key = "/".join(m["module_file"].split("/")[:2])
        by_subsystem[key] = by_subsystem.get(key, 0) + 1
    catalog = {
        "tool": MINER_VERSION,
        "scan_range": scanned_range,
        "candidate_commits": candidates,
        "cases": n,
        "zero_gold_cases": zero_gold,
        "zero_gold_rate": round(zero_gold / n, 4) if n else None,
        "by_subsystem": dict(sorted(by_subsystem.items(), key=lambda kv: -kv[1])),
        "apply_check": {"ok": apply_ok[0], "sampled": apply_ok[1]},
        "case_index": [{k: m[k] for k in
                        ("case_id", "commit", "subject", "module_file", "kernel_symbols",
                         "caller_changed_lines", "treewide_scale")} for m in cases_meta],
    }
    (out_dir / "catalog.json").write_text(json.dumps(catalog, indent=1))

    lines = ["# mined_migration_bench catalog", "",
             f"scan range: `{scanned_range}`; candidate commits: {candidates}; cases: {n}",
             f"zero-gold cases: {zero_gold} ({catalog['zero_gold_rate']}); "
             f"apply --check: {apply_ok[0]}/{apply_ok[1]} sampled", "",
             "| subsystem | cases |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in list(catalog["by_subsystem"].items())[:20]]
    lines += ["", "## Random case dump (eyeball check)", ""]
    for m in rng.sample(cases_meta, min(5, n)):
        lines += [f"### {m['case_id']}",
                  f"- subject: {m['subject']}",
                  f"- gold: {m['kernel_symbols']}",
                  f"- changed lines: {m['caller_changed_lines']}, treewide_scale: {m['treewide_scale']}",
                  ""]
    (out_dir / "catalog.md").write_text("\n".join(lines) + "\n")
    return catalog
