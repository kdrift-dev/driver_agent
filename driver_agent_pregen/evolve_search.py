"""Evolutionary patch-search controller built on OpenEvolve.

Alternative to the linear repair loop in ``cli.py``: OpenEvolve mutates the
in-scope driver source (the genome), ``evolve_evaluator.py`` derives a unified
diff from each mutant, enforces edit-scope, builds it and reports metrics, and
the winning genome is converted back into a standard ``patch_generation``
result dict so downstream validation and reporting are unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .evolve_evaluator import (
    EVOLVE_MARKERS,
    FILE_DELIM_PREFIX,
    FILE_DELIM_SUFFIX,
    build_session_from_ctx,
    genome_to_patch,
)
from .llm_client import _read_kv_like_config_key
from .patch_generation import _compact_agent_obligations, _target_edit_checklist
from .utils import ensure_dir, read_text, write_json, write_text

_PROVIDER_ENDPOINTS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPEN_ROUTER_KEY", ("OPEN_ROUTER_KEY",)),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY", "DEEPSEEK_KEY")),
    "openai": (None, "OPENAI_API_KEY", ("OPENAI_API_KEY",)),
}


class OpenEvolveUnavailable(RuntimeError):
    pass


class OpenEvolveProviderError(RuntimeError):
    """The LLM provider rejected the request (auth/quota); not a patch failure."""


_PROVIDER_ERROR_RE = re.compile(
    r"(?:"
    r"(?:status(?:\s+code)?|error(?:\s+code)?|http)\W{0,6}\b(?:401|403|429)\b"
    r"|\b(?:401|403|429)\b\W{0,3}(?:unauthorized|forbidden|too many requests)"
    r"|\bunauthorized\b"
    r"|authentication\s+(?:error|failed)"
    r"|invalid[\s_]api[\s_]key"
    r"|no auth credentials"
    r"|insufficient[\s_]quota|quota\s+exceeded|exceeded\s+your\s+quota"
    r"|rate[\s_-]?limit"
    r"|billing"
    r")",
    re.IGNORECASE,
)

_ITERATION_ERROR_RE = re.compile(r"Iteration \d+ error: (?P<msg>.*)")


def _looks_like_provider_error(output: str) -> bool:
    return bool(_PROVIDER_ERROR_RE.search(output or ""))


def _all_iterations_provider_errors(output: str) -> bool:
    """True when every iteration errored with a provider rejection and nothing was evaluated."""
    errors = [m.group("msg") for m in _ITERATION_ERROR_RE.finditer(output or "")]
    if not errors:
        return False
    if "Evaluated program" in output:
        return False
    return all(_looks_like_provider_error(msg) for msg in errors)


def _editable_files(impact_graph: dict[str, Any], all_sources: list[str]) -> list[str]:
    """Source files containing edit_required/edit_candidate functions, or all sources."""
    files: list[str] = []
    for tier in ("edit_required_functions", "edit_candidate_functions"):
        for item in impact_graph.get(tier, []) or []:
            rel = item.get("file")
            if rel and rel not in files:
                files.append(rel)
    files = [f for f in files if f in set(all_sources)]
    return files or list(all_sources)


def _build_genome(old_sources: dict[str, str]) -> str:
    """Concatenate in-scope files with delimiters, each body wrapped in EVOLVE-BLOCK markers."""
    start, end = EVOLVE_MARKERS
    parts: list[str] = [
        "// Driver-Agent evolutionary genome. Edit ONLY between EVOLVE-BLOCK markers.",
        "// Do not change the FILE delimiter lines or the marker lines themselves.",
        "",
    ]
    for rel, text in old_sources.items():
        parts.append(f"{FILE_DELIM_PREFIX}{rel}{FILE_DELIM_SUFFIX}")
        parts.append(start)
        parts.append(text.rstrip("\n"))
        parts.append(end)
        parts.append("")
    return "\n".join(parts) + "\n"


def _build_system_message(
    *,
    context_packet: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    impact_graph: dict[str, Any],
) -> str:
    """Domain prompt for OpenEvolve's LLM, built from kernel-side evidence only."""
    checklist = _target_edit_checklist(compatibility_obligations)
    obligations = _compact_agent_obligations(compatibility_obligations)
    lines = [
        "You are migrating a Linux device driver from an old kernel to a newer kernel.",
        "You may ONLY edit code inside the EVOLVE-BLOCK markers. Preserve all behavior",
        "except what the kernel-side changes below require. Do not edit functions that",
        "are not listed as edit targets.",
        "",
        "## Kernel change summary",
        str(context_packet.get("kernel_change_summary", "") or "(none provided)"),
        "",
        "## Patch generation guidance",
        str(context_packet.get("patch_generation_guidance", "") or "(none provided)"),
        "",
        "## Required-edit checklist (kernel-side obligations)",
        json.dumps(checklist, indent=2)[:8000],
        "",
        "## Compatibility obligations (compact)",
        json.dumps(obligations, indent=2)[:8000],
        "",
        "## Edit targets",
        json.dumps(
            {
                "edit_required": [
                    {"file": x.get("file"), "function": x.get("function")}
                    for x in (impact_graph.get("edit_required_functions", []) or [])
                ],
                "edit_candidate": [
                    {"file": x.get("file"), "function": x.get("function")}
                    for x in (impact_graph.get("edit_candidate_functions", []) or [])
                ],
            },
            indent=2,
        )[:6000],
        "",
        "Never invent APIs. Ground every change in the evidence above.",
    ]
    return "\n".join(lines)


def _resolve_llm(provider: str, model: str, config_ini: str | None, api_key: str | None) -> tuple[str | None, str]:
    """Return (api_base, resolved_api_key) for the given provider."""
    provider = (provider or "openai").lower()
    api_base, ini_key, env_names = _PROVIDER_ENDPOINTS.get(provider, _PROVIDER_ENDPOINTS["openai"])
    key = api_key
    if not key:
        for env_name in env_names:
            key = os.environ.get(env_name)
            if key:
                break
    if not key and config_ini:
        key = _read_kv_like_config_key(Path(config_ini), ini_key)
    return api_base, (key or "")


def _write_config_yaml(
    *,
    path: Path,
    iterations: int,
    population_size: int,
    num_islands: int,
    temperature: float,
    api_base: str | None,
    model: str,
    system_message: str,
    eval_timeout_sec: int,
    checkpoint_interval: int,
    max_code_length: int,
    max_tokens: int = 16000,
) -> None:
    """Emit an OpenEvolve YAML config, validated against openevolve 0.2.27."""

    def _yaml_block(text: str, indent: str) -> str:
        return "\n".join(indent + ln for ln in text.splitlines())

    yaml = [
        f"max_iterations: {int(iterations)}",
        "diff_based_evolution: true",
        f"checkpoint_interval: {int(checkpoint_interval)}",
        f"max_code_length: {int(max_code_length)}",
        "llm:",
        *( [f"  api_base: \"{api_base}\""] if api_base else [] ),
        "  models:",
        f"    - name: \"{model}\"",
        f"  temperature: {float(temperature)}",
        f"  max_tokens: {int(max_tokens)}",
        "prompt:",
        "  system_message: |",
        _yaml_block(system_message, "    "),
        "database:",
        f"  population_size: {int(population_size)}",
        f"  num_islands: {int(num_islands)}",
        "  feature_dimensions: [\"build_tier\", \"patch_size\", \"files_touched\"]",
        "evaluator:",
        "  cascade_evaluation: false",
        "  enable_artifacts: true",
        "  parallel_evaluations: 1",
        f"  timeout: {int(eval_timeout_sec)}",
    ]
    write_text(path, "\n".join(yaml) + "\n")


def _run_openevolve(
    *,
    initial_program: Path,
    evaluator_file: Path,
    config_yaml: Path,
    output_dir: Path,
    iterations: int,
    env: dict[str, str],
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Invoke the OpenEvolve CLI and return the parsed best program from output_dir."""
    ensure_dir(output_dir)
    base_args = [
        str(initial_program),
        str(evaluator_file),
        "--config",
        str(config_yaml),
        "--iterations",
        str(int(iterations)),
        "--output",
        str(output_dir),
    ]
    if shutil.which("openevolve-run") is not None:
        cmd = ["openevolve-run"] + base_args
    elif importlib.util.find_spec("openevolve") is not None:
        cmd = [sys.executable, "-m", "openevolve.cli"] + base_args
    else:
        raise OpenEvolveUnavailable(
            "OpenEvolve is not installed in the active environment. Run "
            "`pip install openevolve` (or use the `da-evolve` devshell command)."
        )

    full_env = {**os.environ, **env}
    try:
        proc = subprocess.run(
            cmd,
            env=full_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise OpenEvolveUnavailable(f"Could not launch OpenEvolve runner {cmd[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        write_text(output_dir / "openevolve_stdout.log", partial or "")
        raise RuntimeError(f"openevolve run timed out after {timeout_sec}s:\n{(partial or '')[-4000:]}") from exc
    stdout_text = proc.stdout or ""
    write_text(output_dir / "openevolve_stdout.log", stdout_text)
    if proc.returncode != 0:
        tail = stdout_text[-4000:]
        if _looks_like_provider_error(tail):
            raise OpenEvolveProviderError(
                f"openevolve LLM provider rejected the request (auth/quota); "
                f"exit {proc.returncode}:\n{tail}"
            )
        raise RuntimeError(f"openevolve run failed (exit {proc.returncode}):\n{tail}")
    if _all_iterations_provider_errors(stdout_text):
        raise OpenEvolveProviderError(
            "openevolve exited 0 but every iteration failed with a provider "
            "auth/quota error and no candidate was evaluated:\n" + stdout_text[-4000:]
        )
    return _read_best_program(output_dir)


def _read_best_program(output_dir: Path) -> dict[str, Any]:
    """Locate the best evolved program and its metrics in OpenEvolve's output tree."""
    best_dir = output_dir / "best"
    code = ""
    metrics: dict[str, Any] = {}
    info_path = best_dir / "best_program_info.json"
    if info_path.exists():
        info = json.loads(read_text(info_path))
        metrics = info.get("metrics", {}) or {}
    if best_dir.exists():
        prog = next((p for p in sorted(best_dir.iterdir()) if p.name.startswith("best_program") and p.suffix != ".json"), None)
        if prog is not None:
            code = read_text(prog)
    if not code:
        raise RuntimeError(f"OpenEvolve produced no best program under {best_dir}")
    return {"code": code, "metrics": metrics}


def run_evolutionary_patch_search(
    *,
    run_dir: Path,
    extracted_dir: Path,
    case_id: str,
    module_path: str,
    context_packet: dict[str, Any],
    impact_graph: dict[str, Any],
    compatibility_obligations: dict[str, Any],
    module_result: Any,
    args: Any,
) -> dict[str, Any]:
    """Run OpenEvolve and return a ``patch_generation``-shaped result dict."""
    evolve_dir = run_dir / "evolve"
    ensure_dir(evolve_dir)

    source_files = list(module_result.source_files_old)
    editable = _editable_files(impact_graph, source_files)
    old_sources: dict[str, str] = {}
    for rel in editable:
        src = extracted_dir / "old_module_sources" / rel
        if src.exists():
            old_sources[rel] = read_text(src).rstrip("\n") + "\n"
    if not old_sources:
        return _empty_result(case_id, "no_in_scope_old_sources_for_evolution")

    initial_program = evolve_dir / "initial_program.c"
    genome = _build_genome(old_sources)
    write_text(initial_program, genome)
    max_code_length = max(20000, len(genome) * 2)

    package_parent = str(Path(__file__).resolve().parents[1])
    eval_context = {
        "package_parent": package_parent,
        "repo": str(args.repo) if getattr(args, "repo", None) else None,
        "old_ref": args.old_ref,
        "new_ref": args.new_ref,
        "run_dir": str(run_dir),
        "extracted_dir": str(extracted_dir),
        "module_path": module_path,
        "kbuild_target": getattr(args, "kbuild_target", None),
        "target_source_files": source_files if getattr(args, "kbuild_target", None) else None,
        "jobs": int(getattr(args, "jobs", 8)),
        "kernel_config_target": getattr(args, "kernel_config_target", "defconfig"),
        "impact_graph": impact_graph,
        "old_sources": old_sources,
    }
    eval_context_path = evolve_dir / "eval_context.json"
    write_json(eval_context_path, eval_context)

    system_message = _build_system_message(
        context_packet=context_packet,
        compatibility_obligations=compatibility_obligations,
        impact_graph=impact_graph,
    )
    write_text(evolve_dir / "system_message.txt", system_message)

    api_base, api_key = _resolve_llm(
        provider=args.llm_provider,
        model=args.llm_model,
        config_ini=getattr(args, "config_ini", None),
        api_key=getattr(args, "llm_api_key", None),
    )
    config_yaml = evolve_dir / "openevolve_config.yaml"
    _write_config_yaml(
        path=config_yaml,
        iterations=int(getattr(args, "evolve_iterations", 20)),
        population_size=int(getattr(args, "evolve_population_size", 30)),
        num_islands=int(getattr(args, "evolve_num_islands", 3)),
        temperature=float(getattr(args, "evolve_temperature", 0.8)),
        api_base=api_base,
        model=args.llm_model,
        system_message=system_message,
        eval_timeout_sec=int(getattr(args, "qemu_timeout_sec", 1800)),
        checkpoint_interval=int(getattr(args, "evolve_checkpoint_interval", 10)),
        max_code_length=max_code_length,
    )

    if args.dry_run or not args.enable_llm:
        return _empty_result(case_id, "evolution_skipped_dry_run_or_llm_disabled")

    prewarm = build_session_from_ctx(eval_context).prepare()
    if not prewarm.get("ok"):
        return _empty_result(case_id, f"build_session_prepare_failed: {prewarm.get('error')}")

    evaluator_file = Path(__file__).resolve().parent / "evolve_evaluator.py"
    output_dir = evolve_dir / "openevolve_output"
    env = {"DRIVER_AGENT_EVAL_CONTEXT": str(eval_context_path)}
    if api_key:
        env["OPENAI_API_KEY"] = api_key

    iterations = int(getattr(args, "evolve_iterations", 20))
    eval_timeout_sec = int(getattr(args, "qemu_timeout_sec", 1800))
    try:
        best = _run_openevolve(
            initial_program=initial_program,
            evaluator_file=evaluator_file,
            config_yaml=config_yaml,
            output_dir=output_dir,
            iterations=iterations,
            env=env,
            timeout_sec=iterations * (eval_timeout_sec + 300) + 600,
        )
    except OpenEvolveProviderError as exc:
        return _empty_result(case_id, f"provider_failure: {exc}")
    except OpenEvolveUnavailable as exc:
        return _empty_result(case_id, f"openevolve_unavailable: {exc}")
    except RuntimeError as exc:
        return _empty_result(case_id, f"evolution_failed: {exc}")

    return _genome_to_result(
        case_id=case_id,
        run_dir=run_dir,
        genome_code=best["code"],
        old_sources=old_sources,
        metrics=best.get("metrics", {}),
    )


def _genome_to_result(
    *,
    case_id: str,
    run_dir: Path,
    genome_code: str,
    old_sources: dict[str, str],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    bridge = genome_to_patch(old_sources, genome_code)
    raw_patch = bridge["raw_patch"]
    fixed_patch = bridge["fixed_patch"]
    touched = bridge["touched_files"]
    _ok = bridge["line_fix_ok"]
    _msgs = bridge["line_fix_messages"]

    candidate_id = "EVOLVE-BEST"
    cand_dir = run_dir / "patch_runs" / candidate_id
    ensure_dir(cand_dir)
    raw_rel = f"patch_runs/{candidate_id}/01_raw_patch.diff"
    fixed_rel = f"patch_runs/{candidate_id}/02_fixed_patch.diff"
    write_text(run_dir / raw_rel, raw_patch)
    write_text(run_dir / fixed_rel, fixed_patch)

    no_op = not bool(raw_patch.strip())
    line_fix_failed = not no_op and not (_ok and fixed_patch.strip())
    candidate = {
        "candidate_id": candidate_id,
        "strategy": "openevolve_map_elites",
        "rationale": "Best genome from evolutionary patch search.",
        "evidence": [],
        "open_questions": [],
        "touched_files": touched,
        "raw_patch_path": raw_rel,
        "fixed_patch_path": fixed_rel,
        "line_fix_ok": bool(_ok),
        "line_fix_messages": list(_msgs or []),
        "no_op_candidate": no_op,
        "evolve_metrics": metrics,
    }
    if no_op:
        parse_status = "skipped_auto_noop"
    elif line_fix_failed:
        parse_status = "line_fix_failed"
    else:
        parse_status = "ok"
    result = {
        "case_id": case_id,
        "stage": "patch_generation",
        "patch_generation_mode": "evolutionary",
        "parse_status": parse_status,
        "module_level_notes": ["patch produced by OpenEvolve evolutionary search"],
        "patch_candidates": [candidate],
        "selected_candidate_id": candidate_id,
        "evolve_metrics": metrics,
    }
    write_json(run_dir / "patch_generation.json", result)
    return result


def _empty_result(case_id: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "stage": "patch_generation",
        "patch_generation_mode": "evolutionary",
        "parse_status": reason,
        "module_level_notes": [reason],
        "patch_candidates": [],
        "selected_candidate_id": None,
    }
