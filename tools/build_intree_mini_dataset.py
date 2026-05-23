#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.utils import ensure_dir, slugify, write_json, write_text

SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}
# Keep evidence kernel-side and bounded. Do not mine driver-tree migration examples.
# These files cover the current mini dataset symbols and keep no-hit lookups fast.
KERNEL_PATHS = [
    "include/linux/slab.h",
    "include/linux/ioport.h",
    "include/linux/gfp.h",
    "include/linux/gfp_types.h",
    "include/linux/highmem.h",
    "include/asm-generic/page.h",
    "arch/x86/include/asm/page.h",
    "arch/x86/include/asm/page_64.h",
    "kernel/resource.c",
    "mm/slab_common.c",
    "mm/slub.c",
    "mm/page_alloc.c",
]

CASES: list[dict[str, Any]] = [
    {
        "module_path": "drivers/dax/hmem",
        "category": "soft_reserved_resource_helper_migration",
        "driver_change_summary": "walk_iomem_res_desc -> walk_soft_reserve_res; region_intersects -> region_intersects_soft_reserve",
        "kernel_cause_summary": "v7.0-rc1 adds soft-reserve resource helpers used by hmem.",
        "why_kernel_evolution_driven": "The reference patch replaces generic iomem/resource queries parameterized by IORES_DESC_SOFT_RESERVED with new kernel helpers that encode the Soft Reserved contract directly.",
        "workflow_focus": "Non-allocation API replacement; affected-function discovery should include both hmem_init and hmem_register_device.",
        "kernel_symbols": ["walk_iomem_res_desc", "walk_soft_reserve_res", "region_intersects", "region_intersects_soft_reserve", "IORES_DESC_SOFT_RESERVED"],
        "required_added_symbols": ["walk_soft_reserve_res", "region_intersects_soft_reserve"],
        "expected_removed_patterns": ["walk_iomem_res_desc", "region_intersects("],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/pmdomain/st",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*genpd_data), GFP_KERNEL) -> kzalloc_obj(*genpd_data)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The old call allocates exactly one object; the new kernel provides an object-aware allocation helper for this pattern.",
        "workflow_focus": "Simple semantic migration in init/probe-style code.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/soc/nuvoton",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*attr), GFP_KERNEL) -> kzalloc_obj(*attr)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The allocation is a single-object allocation and matches the new object-aware helper contract.",
        "workflow_focus": "Small module with init/exit resource pairing.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/soc/ux500",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*soc_dev_attr), GFP_KERNEL) -> kzalloc_obj(*soc_dev_attr)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The allocation is a single-object allocation in an init path; the new helper preserves allocation semantics while making object size explicit.",
        "workflow_focus": "Init path and error cleanup preservation.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/video/fbdev/mmp/panel",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*plat_data), GFP_KERNEL) -> kzalloc_obj(*plat_data)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The old probe allocates one platform-data object; the new kernel helper captures that object allocation idiom.",
        "workflow_focus": "Probe callback and allocation failure behavior.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/gpu/vga",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*client), GFP_KERNEL) -> kzalloc_obj(*client)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The driver registers a client allocated as one object; the helper replacement is a source-compatible semantic migration.",
        "workflow_focus": "Registration path and cleanup pairing.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/media/mmc/siano",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(struct smssdio_device), GFP_KERNEL) -> kzalloc_obj(struct smssdio_device)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The reference patch converts a one-struct allocation to the new object-aware helper.",
        "workflow_focus": "Probe path in media/mmc.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/media/dvb-frontends/cxd2880",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(struct cxd2880_priv), GFP_KERNEL) -> kzalloc_obj(struct cxd2880_priv)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The modified call is a direct single-struct allocation; the new helper removes the manual sizeof expression.",
        "workflow_focus": "Large module and focused-source prompt stress test.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/target/tcm_remote",
        "category": "allocator_object_helper_migration",
        "driver_change_summary": "kzalloc(sizeof(*remote_hba), GFP_KERNEL) -> kzalloc_obj(*remote_hba)",
        "kernel_cause_summary": "slab introduces kmalloc_obj/kzalloc_obj family and treewide object allocation migration.",
        "why_kernel_evolution_driven": "The old code allocates one HBA object and then initializes it; the reference patch adopts the object-aware allocator helper.",
        "workflow_focus": "Target subsystem; spec may over- or under-constrain string/configfs behavior.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_obj"],
        "required_added_symbols": ["kzalloc_obj"],
        "expected_removed_patterns": ["kzalloc(sizeof"],
        "kselftest_targets": [],
    },
    {
        "module_path": "drivers/dma-buf/heaps",
        "category": "allocator_and_memory_semantics_migration",
        "driver_change_summary": "kzalloc_obj/kmalloc_objs conversions plus clear_page and GFP accounting adjustment",
        "kernel_cause_summary": "object-aware allocator helpers are introduced; memory zeroing/accounting semantics are adjusted in heap allocation paths.",
        "why_kernel_evolution_driven": "The patch combines allocator helper migration with memory-management semantic updates in dma-buf heap allocation code.",
        "workflow_focus": "kselftest-friendly multi-edit case; tests can exercise dma-buf heap allocation paths.",
        "kernel_symbols": ["kzalloc", "kzalloc_obj", "kmalloc_array", "kmalloc_objs", "clear_page", "__GFP_ACCOUNT"],
        "required_added_symbols": ["kzalloc_obj", "kmalloc_objs", "clear_page", "__GFP_ACCOUNT"],
        "expected_removed_patterns": ["kzalloc(sizeof", "kmalloc_array", "memset(vaddr, 0, PAGE_SIZE)"],
        "kselftest_targets": ["dmabuf-heaps", "drivers/dma-buf"],
    },
]

KSELFTEST_V0 = [
    {
        "module_path": "drivers/dma-buf/heaps",
        "targets": ["dmabuf-heaps", "drivers/dma-buf"],
        "recommended_command": "make -C tools/testing/selftests TARGETS=dmabuf-heaps run_tests",
        "qemu_notes": "Good first candidate; tests allocate dma-buf heaps and can run without target hardware when heap devices are enabled.",
        "dataset_role": "primary",
    },
    {
        "module_path": "drivers/nfc/nxp-nci",
        "targets": ["nci"],
        "recommended_command": "make -C tools/testing/selftests TARGETS=nci run_tests",
        "qemu_notes": "Use cautiously: the reference patch is IRQ trigger / hardware or Device Tree semantics, so NCI tests are not a clean semantic oracle.",
        "dataset_role": "limitation_candidate",
    },
    {
        "module_path": "drivers/block/zram",
        "targets": ["zram"],
        "recommended_command": "make -C tools/testing/selftests TARGETS=zram run_tests",
        "qemu_notes": "Strong selftest coverage but patch size is outside modified_loc<=50; keep in separate kselftest dataset.",
        "dataset_role": "kselftest_only",
    },
    {
        "module_path": "drivers/net/netconsole",
        "targets": ["drivers/net/netconsole"],
        "recommended_command": "make -C tools/testing/selftests TARGETS=drivers/net/netconsole run_tests",
        "qemu_notes": "Good network namespace/QEMU candidate when a matching driver migration exists.",
        "dataset_role": "kselftest_only",
    },
    {
        "module_path": "generic-device-probe",
        "targets": ["devices/error_logs", "devices/probe", "devices/exist"],
        "recommended_command": "make -C tools/testing/selftests TARGETS=devices/error_logs run_tests",
        "qemu_notes": "Generic smoke checks for probe/error regressions; not a semantic proof for a specific driver migration.",
        "dataset_role": "generic_smoke",
    },
]


def run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def git(repo: Path, args: list[str], *, check: bool = True) -> str:
    return run(["git", *args], cwd=repo, check=check).stdout


def read_csv_metadata(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return {row.get("module_path", ""): row for row in csv.DictReader(f) if row.get("module_path")}


def list_source_files(repo: Path, ref: str, module_path: str) -> list[str]:
    out = git(repo, ["ls-tree", "-r", "--name-only", ref, "--", module_path], check=False)
    files = []
    for line in out.splitlines():
        p = line.strip()
        if p and Path(p).suffix in SOURCE_SUFFIXES:
            files.append(p)
    return sorted(files)


def write_old_sources(repo: Path, ref: str, module_path: str, out_dir: Path) -> list[str]:
    files = list_source_files(repo, ref, module_path)
    for rel in files:
        content = git(repo, ["show", f"{ref}:{rel}"], check=False)
        write_text(out_dir / rel, content)
    return files


def first_location(repo: Path, ref: str, symbol: str) -> tuple[str, int] | None:
    proc = run(
        ["git", "grep", "-n", "--fixed-strings", symbol, ref, "--", *KERNEL_PATHS],
        cwd=repo,
        check=False,
    )
    for line in proc.stdout.splitlines():
        # ref:path:line:text; paths may contain ':' rarely, but kernel paths here do not.
        try:
            _, rest = line.split(":", 1)
            path, line_no, _ = rest.split(":", 2)
            return path, int(line_no)
        except Exception:
            continue
    return None


def excerpt(repo: Path, ref: str, path: str, line_no: int, radius: int = 4) -> str:
    text = git(repo, ["show", f"{ref}:{path}"], check=False)
    lines = text.splitlines()
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    out = []
    for i in range(start, end + 1):
        marker = ">" if i == line_no else " "
        out.append(f"{marker}{i}: {lines[i - 1]}")
    return "\n".join(out)


def grep_hits(repo: Path, ref: str, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    proc = run(
        ["git", "grep", "-n", "--fixed-strings", symbol, ref, "--", *KERNEL_PATHS],
        cwd=repo,
        check=False,
    )
    hits = []
    for line in proc.stdout.splitlines()[:limit]:
        try:
            _, rest = line.split(":", 1)
            path, line_no, text = rest.split(":", 2)
            hits.append({"path": path, "line": int(line_no), "text": text.strip()})
        except Exception:
            continue
    return hits


def log_s(repo: Path, old_ref: str, new_ref: str, symbol: str, limit: int = 8) -> list[dict[str, str]]:
    proc = run(
        [
            "git",
            "log",
            "--oneline",
            "--reverse",
            f"{old_ref}..{new_ref}",
            "-S",
            symbol,
            "--",
            *KERNEL_PATHS,
        ],
        cwd=repo,
        check=False,
    )
    commits = []
    for line in proc.stdout.splitlines()[:limit]:
        parts = line.strip().split(" ", 1)
        if not parts:
            continue
        commits.append({"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
    return commits


def kernel_evidence(
    repo: Path,
    old_ref: str,
    new_ref: str,
    symbols: list[str],
    *,
    include_commit_info: bool = False,
) -> dict[str, Any]:
    items = []
    for symbol in symbols:
        old_loc = first_location(repo, old_ref, symbol)
        new_loc = first_location(repo, new_ref, symbol)
        item: dict[str, Any] = {
            "symbol": symbol,
            "old_locations": grep_hits(repo, old_ref, symbol),
            "new_locations": grep_hits(repo, new_ref, symbol),
            "old_definition_excerpt": "",
            "new_definition_excerpt": "",
            "commits_touching_kernel_side_symbol": (
                log_s(repo, old_ref, new_ref, symbol) if include_commit_info else []
            ),
            "definition_presence_change": "unchanged",
        }
        if old_loc:
            item["old_definition_excerpt"] = excerpt(repo, old_ref, old_loc[0], old_loc[1])
        if new_loc:
            item["new_definition_excerpt"] = excerpt(repo, new_ref, new_loc[0], new_loc[1])
        if old_loc and not new_loc:
            item["definition_presence_change"] = "removed_or_not_found_in_new"
        elif new_loc and not old_loc:
            item["definition_presence_change"] = "new_or_newly_visible_in_kernel_paths"
        items.append(item)
    return {
        "items": items,
        "kernel_paths": KERNEL_PATHS,
        "include_kernel_commit_info": bool(include_commit_info),
        "commit_info_policy": "enabled_explicitly" if include_commit_info else "disabled_by_default",
    }


def case_id(module_path: str, old_ref: str, new_ref: str) -> str:
    return module_path.replace("/", "_") + f"__{old_ref}__{new_ref}"


def latest_runs_for_modules(runs_dir: Path | None, modules: set[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not runs_dir or not runs_dir.exists():
        return out
    found = []
    for meta_path in runs_dir.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        module_path = str(meta.get("module_path", "")).strip()
        if module_path in modules:
            found.append((module_path, meta_path.parent))
    for module_path, run_dir in sorted(found, key=lambda x: x[1].as_posix()):
        out[module_path] = run_dir
    return out


def load_run_summary(run_dir: Path | None) -> dict[str, Any]:
    if not run_dir:
        return {"status": "not_available"}
    out: dict[str, Any] = {"status": "available", "run_dir": str(run_dir)}
    impact_path = run_dir / "extracted" / "impact_graph.json"
    if impact_path.exists():
        impact = json.loads(impact_path.read_text(encoding="utf-8", errors="replace"))
        out["edit_scope_counts"] = impact.get("edit_scope_counts", {})
        out["edit_required_functions"] = [
            {"file": x.get("file"), "function": x.get("function"), "tier": x.get("tier"), "edit_scope": x.get("edit_scope")}
            for x in impact.get("edit_required_functions", [])
            if isinstance(x, dict)
        ]
        out["edit_candidate_functions"] = [
            {
                "file": x.get("file"),
                "function": x.get("function"),
                "candidate_reasons": x.get("candidate_reasons", []),
                "edit_scope": x.get("edit_scope"),
            }
            for x in impact.get("edit_candidate_functions", [])
            if isinstance(x, dict)
        ]
        out["context_only_functions"] = [
            {
                "file": x.get("file"),
                "function": x.get("function"),
                "context_reasons": x.get("context_reasons", []),
                "edit_scope": x.get("edit_scope"),
            }
            for x in impact.get("context_only_functions", impact.get("context_functions", []))
            if isinstance(x, dict)
        ]
        out["affected_functions"] = [
            {"file": x.get("file"), "function": x.get("function"), "tier": x.get("tier"), "edit_scope": x.get("edit_scope")}
            for x in impact.get("affected_functions", [])
            if isinstance(x, dict)
        ]
    validation_path = run_dir / "validation.json"
    if validation_path.exists():
        validation = json.loads(validation_path.read_text(encoding="utf-8", errors="replace"))
        out["validation"] = {
            "status": validation.get("status"),
            "post_reasoning": validation.get("post_reasoning", {}),
            "stage_results": validation.get("validation_stage_results", validation.get("stage_results", [])),
        }
    pg_path = run_dir / "patch_generation.json"
    if pg_path.exists():
        pg = json.loads(pg_path.read_text(encoding="utf-8", errors="replace"))
        out["patch_generation"] = {
            "selected_candidate_id": pg.get("selected_candidate_id"),
            "patch_candidates": [
                {
                    "candidate_id": c.get("candidate_id"),
                    "no_op": c.get("no_op"),
                    "fixed_patch_path": c.get("fixed_patch_path"),
                    "touched_files": c.get("touched_files"),
                }
                for c in pg.get("patch_candidates", [])
                if isinstance(c, dict)
            ],
        }
        for c in pg.get("patch_candidates", []):
            if isinstance(c, dict) and c.get("candidate_id") == pg.get("selected_candidate_id"):
                rel = c.get("fixed_patch_path")
                if rel and (run_dir / str(rel)).exists():
                    out["generated_patch_path"] = str(run_dir / str(rel))
                break
    return out


def write_case_markdown(case_dir: Path, case: dict[str, Any], meta: dict[str, str], evidence: dict[str, Any]) -> None:
    lines = [
        f"# {case['module_path']}",
        "",
        "This case pack is evaluation-only. The reference driver diff must not be used as Driver-Agent input.",
        "",
        f"- category: `{case['category']}`",
        f"- driver-side change: {case['driver_change_summary']}",
        f"- kernel-side cause: {case['kernel_cause_summary']}",
        f"- workflow focus: {case['workflow_focus']}",
        f"- modified_loc: `{meta.get('modified_loc', '')}`",
        f"- code_loc: `{meta.get('code_loc', '')}`",
        f"- source_file_count: `{meta.get('source_file_count', '')}`",
        "",
        "## Causal Note",
        case["why_kernel_evolution_driven"],
        "",
        "## Required Semantic Keys",
        "",
        "Required added symbols:",
    ]
    for sym in case.get("required_added_symbols", []):
        lines.append(f"- `{sym}`")
    lines.extend(["", "Expected removed old patterns:"])
    for pat in case.get("expected_removed_patterns", []):
        lines.append(f"- `{pat}`")
    lines.extend(["", "## Kernel Evidence Snapshot", ""])
    for item in evidence.get("items", []):
        commits = item.get("commits_touching_kernel_side_symbol", [])
        lines.append(f"- `{item['symbol']}`: {item.get('definition_presence_change')}; commits={len(commits)}")
    lines.extend(["", "## Files", "", "- `reference.diff`: upstream target driver diff for evaluation only.", "- `old_module_sources/`: old target module source snapshot.", "- `kernel_evidence.json`: old/new kernel-side symbol evidence.", "- `workflow_artifacts.json`: imported workflow outputs if available."])
    write_text(case_dir / "README.md", "\n".join(lines) + "\n")


def build_dataset(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    out_dir = Path(args.out_dir).resolve()
    if args.force and out_dir.exists():
        if out_dir.name not in {"intree_mini_v0", "kselftest_v0"} and "intree_mini" not in out_dir.name:
            raise SystemExit(f"refusing to --force delete unexpected directory: {out_dir}")
        shutil.rmtree(out_dir)
    ensure_dir(out_dir)

    metadata_by_module = read_csv_metadata(Path(args.cases_csv).resolve()) if args.cases_csv else {}
    manifest_rows = []
    manifest_json = []
    case_root = out_dir / "cases"
    ensure_dir(case_root)
    modules = {case["module_path"] for case in CASES}
    run_map = latest_runs_for_modules(Path(args.runs_dir).resolve() if args.runs_dir else None, modules)
    evidence_cache: dict[tuple[str, ...], dict[str, Any]] = {}

    for case in CASES:
        module_path = case["module_path"]
        slug = slugify(module_path.replace("/", "_"))
        cid = case_id(module_path, args.old_ref, args.new_ref)
        case_dir = case_root / slug
        ensure_dir(case_dir)
        meta = metadata_by_module.get(module_path, {})

        ref_diff = git(repo, ["diff", "--no-color", f"{args.old_ref}..{args.new_ref}", "--", module_path], check=False)
        write_text(case_dir / "reference.diff", ref_diff)
        old_files = write_old_sources(repo, args.old_ref, module_path, case_dir / "old_module_sources")
        evidence_key = tuple(sorted(str(s) for s in case.get("kernel_symbols", [])))
        if evidence_key not in evidence_cache:
            evidence_cache[evidence_key] = kernel_evidence(
                repo,
                args.old_ref,
                args.new_ref,
                list(evidence_key),
                include_commit_info=bool(args.include_kernel_commit_info),
            )
        evidence = evidence_cache[evidence_key]
        write_json(case_dir / "kernel_evidence.json", evidence)
        keys = {
            "required_added_symbols": case.get("required_added_symbols", []),
            "expected_removed_patterns": case.get("expected_removed_patterns", []),
            "notes": "Semantic keys are evaluation metrics, not exact patch equality requirements.",
        }
        write_json(case_dir / "expected_semantic_keys.json", keys)
        kselftest = {
            "targets": case.get("kselftest_targets", []),
            "has_direct_kselftest_candidate": bool(case.get("kselftest_targets")),
        }
        write_json(case_dir / "kselftest.json", kselftest)
        run_dir = run_map.get(module_path)
        run_summary = load_run_summary(run_dir)
        write_json(case_dir / "workflow_artifacts.json", run_summary)
        case_payload = {
            **case,
            "case_id": cid,
            "old_ref": args.old_ref,
            "new_ref": args.new_ref,
            "evaluation_only": True,
            "reference_diff_policy": "reference.diff is for evaluation/manual annotation only and must not be used as Driver-Agent input",
            "include_kernel_commit_info": bool(args.include_kernel_commit_info),
            "metadata_from_candidate_csv": meta,
            "old_source_files": old_files,
            "case_pack_files": {
                "reference_diff": "reference.diff",
                "old_module_sources": "old_module_sources/",
                "kernel_evidence": "kernel_evidence.json",
                "expected_semantic_keys": "expected_semantic_keys.json",
                "workflow_artifacts": "workflow_artifacts.json",
            },
        }
        write_json(case_dir / "case.json", case_payload)
        write_case_markdown(case_dir, case, meta, evidence)

        row = {
            "case_id": cid,
            "module_path": module_path,
            "category": case["category"],
            "modified_loc": meta.get("modified_loc", ""),
            "code_loc": meta.get("code_loc", ""),
            "source_file_count": meta.get("source_file_count", ""),
            "required_added_symbols": ";".join(case.get("required_added_symbols", [])),
            "expected_removed_patterns": ";".join(case.get("expected_removed_patterns", [])),
            "kselftest_targets": ";".join(case.get("kselftest_targets", [])),
            "case_pack_dir": str(case_dir.relative_to(out_dir)),
            "workflow_run_status": run_summary.get("status", "not_available"),
        }
        manifest_rows.append(row)
        manifest_json.append(case_payload)

    with (out_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(manifest_rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest_rows)
    write_json(out_dir / "manifest.json", {"cases": manifest_json})

    with (out_dir / "kselftest_v0.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["module_path", "targets", "recommended_command", "dataset_role", "qemu_notes"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for item in KSELFTEST_V0:
            writer.writerow({**item, "targets": ";".join(item["targets"])})
    write_json(out_dir / "kselftest_v0.json", {"items": KSELFTEST_V0})

    readme = [
        "# In-Tree Mini Dataset v0",
        "",
        f"Release pair: `{args.old_ref} -> {args.new_ref}`.",
        "",
        "This dataset is for evaluation and workflow refinement. It contains upstream target-driver reference diffs, so case packs must not be used as Driver-Agent input.",
        "",
        "Allowed Driver-Agent inputs remain: old target driver source plus old/new kernel source/definitions.",
        "",
        "## Contents",
        "",
        "- `manifest.csv` / `manifest.json`: case metadata and semantic keys.",
        "- `cases/<case>/reference.diff`: evaluation-only upstream driver diff.",
        "- `cases/<case>/old_module_sources/`: old target module source snapshot.",
        "- `cases/<case>/kernel_evidence.json`: old/new kernel-side symbol evidence; kernel commit info is disabled by default.",
        "- `kselftest_v0.csv` / `kselftest_v0.json`: small list of kselftest-friendly candidates.",
        "",
        "## Rebuild",
        "",
        "```bash",
        "python tools/build_intree_mini_dataset.py \\",
        "  --repo /mnt/disk7/jw_ai4os/linux_source_code/linux.git \\",
        "  --old-ref v6.19 \\",
        "  --new-ref v7.0-rc1 \\",
        "  --cases-csv /mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv \\",
        "  --out-dir datasets/intree_mini_v0 \\",
        "  --force",
        "```",
        "",
    ]
    write_text(out_dir / "README.md", "\n".join(readme))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build evaluation-only in-tree mini dataset case packs")
    p.add_argument("--repo", required=True)
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--cases-csv", default="/mnt/disk7/jw_ai4os/driver-agent/tools/driver_module_cases_v6.19_to_v7.0-rc1_all_marker_modules.csv")
    p.add_argument("--runs-dir", default=None, help="optional workflow runs root to import affected/patch/validation summaries")
    p.add_argument("--out-dir", default="datasets/intree_mini_v0")
    p.add_argument(
        "--include-kernel-commit-info",
        action="store_true",
        help="include sanitized kernel commit info in case-pack evidence (default: disabled; private/debug ablation only)",
    )
    p.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    build_dataset(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
