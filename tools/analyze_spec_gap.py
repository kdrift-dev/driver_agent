#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT))

from driver_agent_pregen.symbol_scanner import _extract_c_like_functions, _extract_rust_functions
from driver_agent_pregen.utils import now_timestamp, slugify, write_json, write_text

SOURCE_SUFFIXES = {".c", ".h", ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".S", ".s"}

LABELS = [
    "NO_GAP",
    "OVER_STRICT_SPEC",
    "UNDER_SPECIFIED",
    "WRONG_OBLIGATION",
    "AFFECTED_FUNCTION_MISS",
    "AFFECTED_FUNCTION_OVERBROAD",
    "PATCH_GENERATION_FAILURE",
    "VALIDATION_ENV_LIMITATION",
    "UNKNOWN",
]


@dataclass
class CaseSummary:
    case_id: str
    module_path: str
    run_dir: Path
    modified_loc: int
    affected_functions: set[tuple[str, str]]
    pre_verdict: str
    post_verdict: str
    patch_apply_status: str
    patch_apply_reason: str
    compile_pass: bool
    suite_status: str
    qemu_status: str
    runtime_validation_available: bool
    reference_changed_functions: set[tuple[str, str]]
    generated_changed_functions: set[tuple[str, str]]
    missing_reference_functions: set[tuple[str, str]]
    extra_generated_functions: set[tuple[str, str]]
    post_match_or_pass: bool
    patch_matches_reference_behavior: Optional[bool]
    spec_gap_label: str
    notes: list[str]


def _run_cmd(cmd: list[str], *, cwd: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _find_stage(stage_results: list[dict[str, Any]], stage_name: str) -> tuple[str, str]:
    for item in stage_results:
        if item.get("stage") == stage_name:
            return str(item.get("status", "")), str(item.get("reason", ""))
    return "", ""


def _runtime_validation_available(validation: dict[str, Any], suite_status: str, qemu_status: str) -> bool:
    # "available" means module-relevant runtime checks actually executed.
    # If suite stage is passed/failed, tests were executed.
    if suite_status in {"passed", "failed"}:
        return True
    # Fallback KUnit run (fallback_qemu=true) does not validate driver-specific behavior.
    if qemu_status == "passed":
        qemu = validation.get("qemu", {})
        runs = qemu.get("runs", []) if isinstance(qemu, dict) else []
        for run in runs:
            if not isinstance(run, dict):
                continue
            if not bool(run.get("ok")):
                continue
            if bool(run.get("fallback_qemu")):
                continue
            return True
    return False


def _parse_diff_changed_lines(diff_text: str) -> dict[str, dict[str, set[int]]]:
    out: dict[str, dict[str, set[int]]] = {}
    cur_old_path = None
    cur_new_path = None
    old_line = None
    new_line = None

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git a/"):
            cur_old_path = None
            cur_new_path = None
            old_line = None
            new_line = None
            continue

        if raw.startswith("--- "):
            token = raw[4:].strip()
            if token.startswith("a/"):
                cur_old_path = token[2:]
            elif token == "/dev/null":
                cur_old_path = None
            else:
                cur_old_path = token
            continue

        if raw.startswith("+++ "):
            token = raw[4:].strip()
            if token.startswith("b/"):
                cur_new_path = token[2:]
            elif token == "/dev/null":
                cur_new_path = None
            else:
                cur_new_path = token
            continue

        if raw.startswith("@@ "):
            try:
                parts = raw.split("@@", 2)[1].strip().split(" ")
                old_part = parts[0]
                new_part = parts[1]
                old_line = int(old_part.split(",")[0].lstrip("-"))
                new_line = int(new_part.split(",")[0].lstrip("+"))
            except Exception:
                old_line = None
                new_line = None
            continue

        if old_line is None or new_line is None:
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue

        if raw.startswith(" "):
            old_line += 1
            new_line += 1
            continue

        if raw.startswith("-"):
            if cur_old_path:
                file_rec = out.setdefault(cur_old_path, {"old": set(), "new": set()})
                file_rec["old"].add(old_line)
            old_line += 1
            continue

        if raw.startswith("+"):
            if cur_new_path:
                file_rec = out.setdefault(cur_new_path, {"old": set(), "new": set()})
                file_rec["new"].add(new_line)
            new_line += 1
            continue

    return out


def _extract_functions(text: str, rel_file: str) -> list[tuple[str, int, int]]:
    suffix = Path(rel_file).suffix.lower()
    if suffix == ".rs":
        funcs = _extract_rust_functions(text, rel_file)
    elif suffix in SOURCE_SUFFIXES:
        funcs = _extract_c_like_functions(text, rel_file)
    else:
        return []
    out: list[tuple[str, int, int]] = []
    for fn in funcs:
        out.append((fn.name, int(fn.start_line), int(fn.end_line)))
    return out


def _map_lines_to_functions(line_set: set[int], funcs: list[tuple[str, int, int]]) -> set[str]:
    hits: set[str] = set()
    for ln in line_set:
        for name, start, end in funcs:
            if start <= ln <= end:
                hits.add(name)
                break
    return hits


class ReferenceRepo:
    def __init__(self, repo_root: Path, old_ref: str, new_ref: str) -> None:
        self.repo_root = repo_root
        self.old_ref = old_ref
        self.new_ref = new_ref
        self._file_cache: dict[tuple[str, str], Optional[str]] = {}

    def module_diff(self, module_path: str) -> str:
        proc = _run_cmd(
            ["git", "-C", str(self.repo_root), "diff", "--no-color", f"{self.old_ref}..{self.new_ref}", "--", module_path],
            check=False,
        )
        return proc.stdout

    def module_modified_loc(self, module_path: str) -> int:
        proc = _run_cmd(
            ["git", "-C", str(self.repo_root), "diff", "--numstat", f"{self.old_ref}..{self.new_ref}", "--", module_path],
            check=False,
        )
        total = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, _ = parts
            try:
                ai = 0 if a == "-" else int(a)
                di = 0 if d == "-" else int(d)
            except ValueError:
                ai = 0
                di = 0
            total += ai + di
        return total

    def read_file(self, ref: str, rel_path: str) -> Optional[str]:
        key = (ref, rel_path)
        if key in self._file_cache:
            return self._file_cache[key]
        proc = _run_cmd(
            ["git", "-C", str(self.repo_root), "show", f"{ref}:{rel_path}"],
            check=False,
        )
        if proc.returncode != 0:
            self._file_cache[key] = None
            return None
        self._file_cache[key] = proc.stdout
        return proc.stdout

    def changed_functions_for_module(self, module_path: str) -> set[tuple[str, str]]:
        diff_text = self.module_diff(module_path)
        changed = _parse_diff_changed_lines(diff_text)
        out: set[tuple[str, str]] = set()
        for rel_file, lines in changed.items():
            old_lines = lines.get("old", set())
            new_lines = lines.get("new", set())
            if old_lines:
                old_text = self.read_file(self.old_ref, rel_file)
                if old_text is not None:
                    old_funcs = _extract_functions(old_text, rel_file)
                    for fn in _map_lines_to_functions(old_lines, old_funcs):
                        out.add((rel_file, fn))
            if new_lines:
                new_text = self.read_file(self.new_ref, rel_file)
                if new_text is not None:
                    new_funcs = _extract_functions(new_text, rel_file)
                    for fn in _map_lines_to_functions(new_lines, new_funcs):
                        out.add((rel_file, fn))
        return out


def _parse_function_pairs_json(text: str) -> set[tuple[str, str]]:
    if not text:
        return set()
    try:
        arr = json.loads(text)
    except Exception:
        return set()
    out: set[tuple[str, str]] = set()
    if not isinstance(arr, list):
        return out
    for item in arr:
        if not isinstance(item, str):
            continue
        if ":" not in item:
            continue
        p, f = item.rsplit(":", 1)
        p = p.strip()
        f = f.strip()
        if p and f:
            out.add((p, f))
    return out


def _format_fn_set(items: set[tuple[str, str]]) -> str:
    arr = [f"{p}:{f}" for p, f in sorted(items)]
    return json.dumps(arr, ensure_ascii=False)


def _discover_run_cases(
    runs_dir: Path,
    *,
    modules_filter: Optional[set[str]] = None,
    latest_per_module: bool = True,
) -> list[Path]:
    candidates: list[Path] = []
    for p in runs_dir.rglob("validation.json"):
        case_dir = p.parent
        if not (case_dir / "extracted").exists():
            continue
        meta = _load_json(case_dir / "metadata.json", {})
        mod = str(meta.get("module_path", "")).strip()
        if not mod:
            ms = _load_json(case_dir / "extracted" / "module_source.json", {})
            mod = str(ms.get("module_path", "")).strip()
        if modules_filter and mod not in modules_filter:
            continue
        candidates.append(case_dir)

    if not latest_per_module:
        return sorted(candidates)

    by_mod: dict[str, Path] = {}
    for c in sorted(candidates):
        meta = _load_json(c / "metadata.json", {})
        mod = str(meta.get("module_path", "")).strip()
        if not mod:
            ms = _load_json(c / "extracted" / "module_source.json", {})
            mod = str(ms.get("module_path", "")).strip()
        if not mod:
            continue
        prev = by_mod.get(mod)
        if prev is None or c.as_posix() > prev.as_posix():
            by_mod[mod] = c
    return sorted(by_mod.values(), key=lambda x: x.as_posix())


def _load_module_old_file(case_dir: Path, rel_file: str) -> Optional[str]:
    p = case_dir / "extracted" / "old_module_sources" / rel_file
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def _generated_changed_functions(case_dir: Path, ref: ReferenceRepo) -> set[tuple[str, str]]:
    pg = _load_json(case_dir / "patch_generation.json", {})
    cands = pg.get("patch_candidates", [])
    if not isinstance(cands, list) or not cands:
        return set()
    sid = pg.get("selected_candidate_id")
    selected = None
    for c in cands:
        if isinstance(c, dict) and c.get("candidate_id") == sid:
            selected = c
            break
    if selected is None:
        selected = cands[0] if isinstance(cands[0], dict) else None
    if not selected:
        return set()
    fixed_rel = selected.get("fixed_patch_path")
    if not fixed_rel:
        return set()
    fixed_path = case_dir / str(fixed_rel)
    if not fixed_path.exists():
        return set()
    patch_text = fixed_path.read_text(encoding="utf-8", errors="replace")
    if not patch_text.strip():
        return set()
    changed = _parse_diff_changed_lines(patch_text)
    out: set[tuple[str, str]] = set()
    for rel_file, lines in changed.items():
        old_lines = lines.get("old", set())
        new_lines = lines.get("new", set())
        old_text = _load_module_old_file(case_dir, rel_file) or ref.read_file(ref.old_ref, rel_file)
        if old_text is not None:
            funcs = _extract_functions(old_text, rel_file)
            for fn in _map_lines_to_functions(old_lines | new_lines, funcs):
                out.add((rel_file, fn))
        else:
            if old_lines or new_lines:
                out.add((rel_file, "<global_or_unknown>"))
    return out


def _affected_functions(case_dir: Path) -> set[tuple[str, str]]:
    impact = _load_json(case_dir / "extracted" / "impact_graph.json", {})
    out: set[tuple[str, str]] = set()
    for afn in impact.get("affected_functions", []):
        if not isinstance(afn, dict):
            continue
        p = str(afn.get("file", "")).strip()
        f = str(afn.get("function", "")).strip()
        if p and f:
            out.add((p, f))
    return out


def _kernel_evidence_summary(case_dir: Path, max_items: int = 12) -> list[str]:
    kd = _load_json(case_dir / "extracted" / "kernel_definition_diffs.json", {})
    items = kd.get("items", [])
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol", ""))
        if not sym:
            continue
        if it.get("definition_changed") or it.get("migration_signal"):
            ct = str(it.get("change_type_guess", "unknown"))
            mig = bool(it.get("migration_signal"))
            out.append(f"{sym} ({ct}, migration_signal={mig})")
            if len(out) >= max_items:
                break
    return out


def _classify_gap(
    *,
    patch_apply_status: str,
    patch_apply_reason: str,
    patch_matches_reference: Optional[bool],
    missing_reference_functions: set[tuple[str, str]],
    extra_generated_functions: set[tuple[str, str]],
    affected_functions: set[tuple[str, str]],
    pre_verdict: str,
    post_verdict: str,
    runtime_validation_available: bool,
) -> str:
    if patch_apply_status in {"failed"}:
        return "PATCH_GENERATION_FAILURE"
    if patch_apply_status in {"skipped"} and "no_patch_candidate_available" in (patch_apply_reason or ""):
        return "PATCH_GENERATION_FAILURE"
    if patch_matches_reference is None:
        if not runtime_validation_available:
            return "VALIDATION_ENV_LIMITATION"
        return "UNKNOWN"
    if patch_matches_reference:
        if post_verdict in {"PASS", "MATCH"}:
            return "NO_GAP"
        if pre_verdict == "MISMATCH" or post_verdict == "FAIL":
            return "OVER_STRICT_SPEC"
        return "UNKNOWN"

    if missing_reference_functions:
        if not affected_functions:
            return "AFFECTED_FUNCTION_MISS"
        for fn in missing_reference_functions:
            if fn not in affected_functions:
                return "AFFECTED_FUNCTION_MISS"
        return "UNDER_SPECIFIED"

    if extra_generated_functions:
        if len(affected_functions) >= max(3, len(extra_generated_functions) + 1):
            return "AFFECTED_FUNCTION_OVERBROAD"
        return "OVER_STRICT_SPEC"

    if post_verdict in {"PASS", "MATCH"}:
        return "WRONG_OBLIGATION"
    return "UNKNOWN"


def _case_markdown(case: CaseSummary, case_dir: Path) -> str:
    pre_spec = _load_json(case_dir / "llm_calls" / "01_pre_spec" / "parsed_output.json", {})
    pre_reason = _load_json(case_dir / "llm_calls" / "02_pre_reasoner" / "parsed_output.json", {})
    post_reason = _load_json(case_dir / "llm_calls" / "04_post_reasoner" / "parsed_output.json", {})
    patch_gen = _load_json(case_dir / "patch_generation.json", {})
    kd = _load_json(case_dir / "extracted" / "kernel_definition_diffs.json", {})

    spec_lines = []
    for fs in (pre_spec.get("function_specs") or [])[:6]:
        if not isinstance(fs, dict):
            continue
        fn = fs.get("function", "")
        obl = fs.get("compatibility_obligation", "")
        spec_lines.append(f"- `{fn}`: {obl[:220]}")
    if not spec_lines:
        spec_lines.append("- (no function_specs)")

    patch_candidates = patch_gen.get("patch_candidates") or []
    selected_id = patch_gen.get("selected_candidate_id")
    selected = None
    for c in patch_candidates:
        if isinstance(c, dict) and c.get("candidate_id") == selected_id:
            selected = c
            break
    if selected is None and patch_candidates and isinstance(patch_candidates[0], dict):
        selected = patch_candidates[0]

    patch_lines = []
    if selected:
        patch_lines.append(f"- selected_candidate_id: `{selected.get('candidate_id', '')}`")
        patch_lines.append(f"- strategy: {str(selected.get('strategy', ''))[:260]}")
        patch_lines.append(f"- touched_files: {selected.get('touched_files', [])}")
    else:
        patch_lines.append("- (no patch candidate)")

    md: list[str] = []
    md.append(f"# {case.case_id}")
    md.append("")
    md.append(f"- module_path: `{case.module_path}`")
    md.append(f"- run_dir: `{case.run_dir}`")
    md.append(f"- spec_gap_label: `{case.spec_gap_label}`")
    md.append("")
    md.append("1. What kernel-side evidence was extracted?")
    evid = _kernel_evidence_summary(case_dir)
    if evid:
        for e in evid:
            md.append(f"- {e}")
    else:
        md.append("- (no changed/migration symbols captured)")
    md.append("")
    md.append("2. What affected functions were detected?")
    if case.affected_functions:
        for p, f in sorted(case.affected_functions):
            md.append(f"- `{p}:{f}`")
    else:
        md.append("- (none)")
    md.append("")
    md.append("3. What spec did the Pre-Spec Agent generate?")
    md.extend(spec_lines)
    md.append("")
    md.append("4. Was the pre-reasoning verdict reasonable?")
    if case.pre_verdict == "MISMATCH" and case.patch_matches_reference_behavior:
        md.append("- Likely over-strict or unsupported assumption (patch matches reference at function-level).")
    elif case.pre_verdict in {"MATCH", "UNKNOWN"} and not case.patch_matches_reference_behavior:
        md.append("- Potential under-specification (pre verdict did not force required edits).")
    else:
        md.append("- Verdict appears consistent with available function-level evidence.")
    md.append("")
    md.append("5. What patch was generated?")
    md.extend(patch_lines)
    md.append("")
    md.append("6. Did the patch modify only necessary functions?")
    if not case.extra_generated_functions:
        md.append("- Yes (no extra generated functions at function-level).")
    else:
        md.append("- No. Extra functions:")
        for p, f in sorted(case.extra_generated_functions):
            md.append(f"- `{p}:{f}`")
    md.append("")
    md.append("7. Did the patch match the upstream/reference diff at function level?")
    md.append(f"- patch_matches_reference_behavior: `{case.patch_matches_reference_behavior}`")
    if case.missing_reference_functions:
        md.append("- Missing reference functions:")
        for p, f in sorted(case.missing_reference_functions):
            md.append(f"- `{p}:{f}`")
    md.append("")
    md.append("8. Did post-reasoning say MATCH/PASS?")
    md.append(f"- post_verdict: `{case.post_verdict}` (post_match_or_pass={case.post_match_or_pass})")
    md.append("")
    md.append("9. If post-reasoning said MATCH/PASS but patch was wrong, what spec gap caused it?")
    if case.post_match_or_pass and case.patch_matches_reference_behavior is False:
        md.append(f"- Gap label: `{case.spec_gap_label}`")
    else:
        md.append("- Not triggered in this case.")
    md.append("")
    md.append("10. If the patch changed unnecessary functions, was the spec too strict or the affected set too broad?")
    if case.extra_generated_functions:
        md.append(f"- Classified as `{case.spec_gap_label}` based on extra function edits.")
    else:
        md.append("- No unnecessary function modifications detected at function-level.")
    md.append("")
    if case.notes:
        md.append("## Notes")
        for n in case.notes:
            md.append(f"- {n}")
        md.append("")
    return "\n".join(md)


def _short_reason(case: CaseSummary) -> str:
    if case.spec_gap_label == "NO_GAP":
        return "post verdict aligned with function-level patch quality"
    if case.spec_gap_label == "UNDER_SPECIFIED":
        return "required reference-changed functions were missing from generated patch"
    if case.spec_gap_label == "OVER_STRICT_SPEC":
        return "generated patch modified extra functions not required by reference"
    if case.spec_gap_label == "AFFECTED_FUNCTION_MISS":
        return "real modified function not included in affected set"
    if case.spec_gap_label == "AFFECTED_FUNCTION_OVERBROAD":
        return "affected set over-expanded and induced unrelated edits"
    if case.spec_gap_label == "PATCH_GENERATION_FAILURE":
        return "no valid patch candidate or patch application failure"
    if case.spec_gap_label == "VALIDATION_ENV_LIMITATION":
        return "runtime environment limits prevented conclusive validation"
    if case.spec_gap_label == "WRONG_OBLIGATION":
        return "obligation-level mismatch with generated patch behavior"
    return "insufficient evidence"


def _build_case_summary(case_dir: Path, ref: ReferenceRepo) -> Optional[CaseSummary]:
    meta = _load_json(case_dir / "metadata.json", {})
    ms = _load_json(case_dir / "extracted" / "module_source.json", {})
    case_id = str(meta.get("case_id", "")).strip() or str(ms.get("module_path", "")).replace("/", "_")
    module_path = str(meta.get("module_path", "")).strip() or str(ms.get("module_path", "")).strip()
    if not case_id or not module_path:
        return None

    validation = _load_json(case_dir / "validation.json", {})
    stage_results = validation.get("validation_stage_results", [])
    patch_apply_status, patch_apply_reason = _find_stage(stage_results, "patch_apply")
    compile_status, _ = _find_stage(stage_results, "compile_modules")
    suite_status, _ = _find_stage(stage_results, "suite_execution")
    qemu_status, _ = _find_stage(stage_results, "qemu_execution")

    pre_reason = _load_json(case_dir / "llm_calls" / "02_pre_reasoner" / "parsed_output.json", {})
    post_reason = _load_json(case_dir / "llm_calls" / "04_post_reasoner" / "parsed_output.json", {})
    pre_verdict = str(pre_reason.get("overall_verdict", "UNKNOWN")).upper()
    post_verdict = str(post_reason.get("overall_verdict", "UNKNOWN")).upper()

    affected = _affected_functions(case_dir)
    reference_changed = ref.changed_functions_for_module(module_path)
    generated_changed = _generated_changed_functions(case_dir, ref)
    missing_ref = reference_changed - generated_changed
    extra_gen = generated_changed - reference_changed

    patch_matches_reference: Optional[bool]
    if not reference_changed and not generated_changed:
        patch_matches_reference = True
    elif reference_changed or generated_changed:
        patch_matches_reference = (not missing_ref) and (not extra_gen)
    else:
        patch_matches_reference = None

    compile_pass = bool(validation.get("compile", {}).get("success")) or compile_status == "passed"
    runtime_validation_available = _runtime_validation_available(validation, suite_status, qemu_status)
    post_match_or_pass = post_verdict in {"MATCH", "PASS"}

    label = _classify_gap(
        patch_apply_status=patch_apply_status,
        patch_apply_reason=patch_apply_reason,
        patch_matches_reference=patch_matches_reference,
        missing_reference_functions=missing_ref,
        extra_generated_functions=extra_gen,
        affected_functions=affected,
        pre_verdict=pre_verdict,
        post_verdict=post_verdict,
        runtime_validation_available=runtime_validation_available,
    )

    notes: list[str] = []
    if post_match_or_pass and patch_matches_reference is False:
        notes.append("post_reasoning indicates MATCH/PASS while function-level diff comparison indicates incorrect patch")
    if suite_status == "skipped":
        notes.append("suite execution skipped (likely root/hardware limitations)")
    if qemu_status == "skipped":
        notes.append("qemu stage skipped or disabled")
    if not runtime_validation_available:
        notes.append("no module-relevant runtime validation available (suite skipped or only fallback qemu run)")

    return CaseSummary(
        case_id=case_id,
        module_path=module_path,
        run_dir=case_dir,
        modified_loc=ref.module_modified_loc(module_path),
        affected_functions=affected,
        pre_verdict=pre_verdict,
        post_verdict=post_verdict,
        patch_apply_status=patch_apply_status,
        patch_apply_reason=patch_apply_reason,
        compile_pass=compile_pass,
        suite_status=suite_status,
        qemu_status=qemu_status,
        runtime_validation_available=runtime_validation_available,
        reference_changed_functions=reference_changed,
        generated_changed_functions=generated_changed,
        missing_reference_functions=missing_ref,
        extra_generated_functions=extra_gen,
        post_match_or_pass=post_match_or_pass,
        patch_matches_reference_behavior=patch_matches_reference,
        spec_gap_label=label,
        notes=notes,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Analyze FM-Agent-style spec/reasoning gaps on driver-agent runs")
    p.add_argument("--runs-dir", required=True, help="root directory containing pregen run outputs")
    p.add_argument("--reference-root", required=True, help="linux git repo used only for evaluation")
    p.add_argument("--out-dir", required=True, help="output directory for gap analysis artifacts")
    p.add_argument("--old-ref", default="v6.19")
    p.add_argument("--new-ref", default="v7.0-rc1")
    p.add_argument("--modules-file", default="", help="optional newline-separated module path list")
    p.add_argument("--module-regex", default="", help="optional module-path regex filter")
    p.add_argument("--latest-per-module", action="store_true", default=True)
    p.add_argument("--no-latest-per-module", dest="latest_per_module", action="store_false")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--emit-8case-table", action="store_true", help="also emit 8case_spec_gap_table.csv")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runs_dir = Path(args.runs_dir).resolve()
    ref_root = Path(args.reference_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "case_reports").mkdir(parents=True, exist_ok=True)

    proc = _run_cmd(["git", "-C", str(ref_root), "rev-parse", "--is-inside-work-tree"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"--reference-root is not a git repository: {ref_root}")

    modules_filter: Optional[set[str]] = None
    if args.modules_file:
        mf = Path(args.modules_file).resolve()
        modules_filter = {line.strip() for line in mf.read_text(encoding="utf-8").splitlines() if line.strip()}

    case_dirs = _discover_run_cases(runs_dir, modules_filter=modules_filter, latest_per_module=bool(args.latest_per_module))
    if args.module_regex:
        rx = re.compile(args.module_regex)
        filtered = []
        for c in case_dirs:
            meta = _load_json(c / "metadata.json", {})
            mod = str(meta.get("module_path", "")).strip()
            if mod and rx.search(mod):
                filtered.append(c)
        case_dirs = filtered
    if args.limit and args.limit > 0:
        case_dirs = case_dirs[: args.limit]

    ref = ReferenceRepo(ref_root, args.old_ref, args.new_ref)
    summaries: list[CaseSummary] = []
    for case_dir in case_dirs:
        cs = _build_case_summary(case_dir, ref)
        if cs is None:
            continue
        summaries.append(cs)

    csv_path = out_dir / "spec_gap_cases.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "case_id",
                "module_path",
                "run_dir",
                "modified_loc",
                "affected_functions",
                "pre_reasoning_verdict",
                "post_reasoning_verdict",
                "generated_patch_status",
                "generated_patch_reason",
                "compile_pass",
                "suite_status",
                "qemu_status",
                "runtime_validation_available",
                "reference_changed_functions",
                "generated_patch_changed_functions",
                "unnecessary_modified_functions",
                "missing_modified_functions",
                "post_reasoning_match_or_pass",
                "patch_matches_reference_behavior",
                "spec_gap_label",
                "notes",
            ],
        )
        writer.writeheader()
        for c in summaries:
            writer.writerow(
                {
                    "case_id": c.case_id,
                    "module_path": c.module_path,
                    "run_dir": c.run_dir.as_posix(),
                    "modified_loc": c.modified_loc,
                    "affected_functions": _format_fn_set(c.affected_functions),
                    "pre_reasoning_verdict": c.pre_verdict,
                    "post_reasoning_verdict": c.post_verdict,
                    "generated_patch_status": c.patch_apply_status,
                    "generated_patch_reason": c.patch_apply_reason,
                    "compile_pass": c.compile_pass,
                    "suite_status": c.suite_status,
                    "qemu_status": c.qemu_status,
                    "runtime_validation_available": c.runtime_validation_available,
                    "reference_changed_functions": _format_fn_set(c.reference_changed_functions),
                    "generated_patch_changed_functions": _format_fn_set(c.generated_changed_functions),
                    "unnecessary_modified_functions": _format_fn_set(c.extra_generated_functions),
                    "missing_modified_functions": _format_fn_set(c.missing_reference_functions),
                    "post_reasoning_match_or_pass": c.post_match_or_pass,
                    "patch_matches_reference_behavior": c.patch_matches_reference_behavior,
                    "spec_gap_label": c.spec_gap_label,
                    "notes": " | ".join(c.notes),
                }
            )

    # Per-case markdown reports.
    for c in summaries:
        text = _case_markdown(c, c.run_dir)
        report_name = f"{slugify(c.case_id)}.md"
        write_text(out_dir / "case_reports" / report_name, text)

    label_counts = {k: 0 for k in LABELS}
    for c in summaries:
        label_counts[c.spec_gap_label] = label_counts.get(c.spec_gap_label, 0) + 1

    summary = {
        "runs_dir": runs_dir.as_posix(),
        "reference_root": ref_root.as_posix(),
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "selected_case_dirs": [p.as_posix() for p in case_dirs],
        "analyzed_cases": len(summaries),
        "label_counts": label_counts,
        "post_match_but_patch_wrong": [
            c.case_id for c in summaries if c.post_match_or_pass and c.patch_matches_reference_behavior is False
        ],
    }
    write_json(out_dir / "spec_gap_summary.json", summary)

    md_lines: list[str] = []
    md_lines.append("# Spec Gap Analysis Report")
    md_lines.append("")
    md_lines.append(f"- analyzed_cases: {len(summaries)}")
    md_lines.append(f"- runs_dir: `{runs_dir}`")
    md_lines.append(f"- reference_root: `{ref_root}`")
    md_lines.append("")
    md_lines.append("## Label Counts")
    for k in LABELS:
        md_lines.append(f"- {k}: {label_counts.get(k, 0)}")
    md_lines.append("")
    if summary["post_match_but_patch_wrong"]:
        md_lines.append("## Post MATCH/PASS but Patch Wrong")
        for cid in summary["post_match_but_patch_wrong"]:
            md_lines.append(f"- {cid}")
        md_lines.append("")
    md_lines.append("## Files")
    md_lines.append(f"- `{csv_path}`")
    md_lines.append(f"- `{out_dir / 'spec_gap_summary.json'}`")
    md_lines.append(f"- `{out_dir / 'case_reports'}`")
    write_text(out_dir / "spec_gap_report.md", "\n".join(md_lines) + "\n")

    if args.emit_8case_table:
        table_path = out_dir / "8case_spec_gap_table.csv"
        with table_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "case_id",
                    "module_path",
                    "pre_verdict",
                    "post_verdict",
                    "patch_correct",
                    "compile_pass",
                    "runtime_validation_available",
                    "reference_function_overlap",
                    "missing_reference_functions",
                    "extra_generated_functions",
                    "spec_gap_label",
                    "manual_review_required",
                    "short_reason",
                ],
            )
            writer.writeheader()
            for c in summaries:
                overlap = c.reference_changed_functions & c.generated_changed_functions
                manual_review = c.spec_gap_label not in {"NO_GAP"}
                writer.writerow(
                    {
                        "case_id": c.case_id,
                        "module_path": c.module_path,
                        "pre_verdict": c.pre_verdict,
                        "post_verdict": c.post_verdict,
                        "patch_correct": c.patch_matches_reference_behavior,
                        "compile_pass": c.compile_pass,
                        "runtime_validation_available": c.runtime_validation_available,
                        "reference_function_overlap": _format_fn_set(overlap),
                        "missing_reference_functions": _format_fn_set(c.missing_reference_functions),
                        "extra_generated_functions": _format_fn_set(c.extra_generated_functions),
                        "spec_gap_label": c.spec_gap_label,
                        "manual_review_required": manual_review,
                        "short_reason": _short_reason(c),
                    }
                )

    print(
        json.dumps(
            {
                "out_dir": out_dir.as_posix(),
                "analyzed_cases": len(summaries),
                "labels": label_counts,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
