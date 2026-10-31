#!/usr/bin/env python3
"""Validation experiment for the evolutionary patch-search stage.

Runs the pipeline N times with the ``evolutionary`` strategy and M times with
the ``linear`` control on the same case, parses each run's artifacts and emits
a per-trial CSV plus an aggregate markdown table. Repeated trials are required
because the mutation operator is a stochastic LLM. Provider auth/quota failures
and infrastructure failures are excluded from success denominators. This is an
experiment driver, not a unit test and not part of the agent input path.

Environment overrides:
    REPO / LINUX_REPO   linux.git checkout (required)
    OLD_REF NEW_REF     git refs                (default v6.19 / v7.0-rc1)
    MODULE_PATH         driver dir              (default drivers/pmdomain/st)
    KBUILD_TARGET       .ko target, optional    (e.g. ste-ux500-pm-domain.ko)
    KERNEL_CONFIG       defconfig|allmodconfig  (default defconfig)
    EVOLVE_PYTHON       interpreter with openevolve installed (default: this python)
    LINEAR_PYTHON       interpreter for linear control        (default: this python)
    CONFIG_INI          provider key file       (default <repo>/config.ini)
    LLM_PROVIDER LLM_MODEL
    N_EVOLVE N_LINEAR   trial counts            (default 5 / 5)
    JOBS                make -j                 (default 8)
    EVOLVE_ITERATIONS EVOLVE_POP EVOLVE_ISLANDS EVOLVE_TEMPERATURE

Example:
    REPO=/linux/linux.git MODULE_PATH=drivers/pmdomain/st \
    EVOLVE_PYTHON=/opt/oe-venv/bin/python N_EVOLVE=5 N_LINEAR=5 \
    python3 tools/run_evolve_validation_experiment.py --out-root runs/evolve_experiment
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PREGEN = REPO_ROOT / "pregen_driver_agent.py"

INFRA_FAILURE_PREFIXES = ("openevolve_unavailable", "evolution_failed", "build_session_prepare_failed")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_config(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-root", default=_env("OUT_ROOT", str(REPO_ROOT / "runs" / "evolve_experiment")))
    p.add_argument("--repo", default=os.environ.get("REPO") or os.environ.get("LINUX_REPO", ""))
    p.add_argument("--old-ref", default=_env("OLD_REF", "v6.19"))
    p.add_argument("--new-ref", default=_env("NEW_REF", "v7.0-rc1"))
    p.add_argument("--module-path", default=_env("MODULE_PATH", "drivers/pmdomain/st"))
    p.add_argument("--kbuild-target", default=os.environ.get("KBUILD_TARGET", ""))
    p.add_argument("--kernel-config-target", default=_env("KERNEL_CONFIG", "defconfig"))
    p.add_argument("--config-ini", default=_env("CONFIG_INI", str(REPO_ROOT / "config.ini")))
    p.add_argument("--llm-provider", default=_env("LLM_PROVIDER", "openrouter"))
    p.add_argument("--llm-model", default=_env("LLM_MODEL", "anthropic/claude-sonnet-4.6"))
    p.add_argument("--evolve-python", default=_env("EVOLVE_PYTHON", sys.executable))
    p.add_argument("--linear-python", default=_env("LINEAR_PYTHON", sys.executable))
    p.add_argument("--n-evolve", type=int, default=int(_env("N_EVOLVE", "5")))
    p.add_argument("--n-linear", type=int, default=int(_env("N_LINEAR", "5")))
    p.add_argument("--jobs", type=int, default=int(_env("JOBS", "8")))
    p.add_argument("--evolve-iterations", type=int, default=int(_env("EVOLVE_ITERATIONS", "20")))
    p.add_argument("--evolve-population-size", type=int, default=int(_env("EVOLVE_POP", "30")))
    p.add_argument("--evolve-num-islands", type=int, default=int(_env("EVOLVE_ISLANDS", "3")))
    p.add_argument("--evolve-temperature", type=float, default=float(_env("EVOLVE_TEMPERATURE", "0.8")))
    p.add_argument("--dry-run", action="store_true", help="print the trial commands and exit (no LLM/build)")
    p.add_argument("--reparse", action="store_true", help="re-parse existing trial dirs under --out-root and rewrite CSV/markdown (no new trials)")
    args = p.parse_args(argv)
    if not args.repo:
        p.error("linux repo not set; pass --repo or REPO=/path/to/linux.git")
    return args


def _preflight_openevolve(args: argparse.Namespace) -> None:
    """Fail fast if the evolutionary interpreter cannot import openevolve."""
    if args.n_evolve <= 0:
        return
    probe = subprocess.run(
        [args.evolve_python, "-c", "import openevolve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if probe.returncode != 0:
        raise SystemExit(
            f"[experiment] openevolve is not importable by {args.evolve_python!r}; "
            "set EVOLVE_PYTHON to an interpreter with openevolve installed "
            "(e.g. .venv-openevolve/bin/python). Import error:\n"
            + (probe.stderr or "").strip()
        )


def _pipeline_cmd(args: argparse.Namespace, *, strategy: str, out_dir: Path) -> tuple[list[str], str]:
    python = args.evolve_python if strategy == "evolutionary" else args.linear_python
    cmd = [
        python,
        str(PREGEN),
        "--repo", args.repo,
        "--old-ref", args.old_ref,
        "--new-ref", args.new_ref,
        "--module-path", args.module_path,
        "--out-dir", str(out_dir),
        "--jobs", str(args.jobs),
        "--kernel-config-target", args.kernel_config_target,
        "--enable-llm",
        "--llm-provider", args.llm_provider,
        "--llm-model", args.llm_model,
        "--config-ini", args.config_ini,
    ]
    if args.kbuild_target:
        cmd += ["--kbuild-target", args.kbuild_target]
    if strategy == "evolutionary":
        cmd += [
            "--patch-search-strategy", "evolutionary",
            "--evolve-iterations", str(args.evolve_iterations),
            "--evolve-population-size", str(args.evolve_population_size),
            "--evolve-num-islands", str(args.evolve_num_islands),
            "--evolve-temperature", str(args.evolve_temperature),
        ]
    return cmd, python


_RUN_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def _find_run_dir(out_dir: Path) -> Optional[Path]:
    """Return the newest <out_dir>/<case_id>/<timestamp>/ run directory.

    Only timestamp-named directories qualify: OpenEvolve checkpoints carry
    their own metadata.json deeper in the tree and must not be selected.
    """
    candidates = [
        c.parent for c in out_dir.rglob("metadata.json") if _RUN_DIR_RE.match(c.parent.name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


def run_trial(args: argparse.Namespace, *, strategy: str, index: int) -> dict[str, Any]:
    out_dir = Path(args.out_root) / strategy / f"trial_{index:02d}"
    cmd, python = _pipeline_cmd(args, strategy=strategy, out_dir=out_dir)

    if args.dry_run:
        print(f"[dry-run] {strategy} trial {index}: {' '.join(cmd)}")
        return {"strategy": strategy, "trial": index, "status": "dry_run"}

    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "trial_stdout.log"
    print(f"[run] {strategy} trial {index} (python={python}) -> {out_dir}")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    run_dir = _find_run_dir(out_dir)
    metrics = parse_run(run_dir) if run_dir else {"status": "no_run_dir"}
    metrics.update(
        {
            "strategy": strategy,
            "trial": index,
            "exit_code": proc.returncode,
            "run_dir": str(run_dir) if run_dir else "",
            "log": str(log_path),
        }
    )
    if "status" not in metrics:
        metrics["status"] = "ok" if proc.returncode == 0 else "pipeline_error"
    return metrics


def _load(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _level_status(summary: dict[str, Any], level: int) -> str:
    for entry in summary.get("levels", []) or []:
        if entry.get("level") == level:
            return str(entry.get("status", "UNKNOWN")).upper()
    return "UNKNOWN"


def _openevolve_stats(run_dir: Path) -> dict[str, Any]:
    """Best-effort MAP-Elites stats from the OpenEvolve stdout log."""
    log = next(run_dir.glob("evolve/openevolve_output/openevolve_stdout.log"), None)
    if log is None:
        return {}
    text = log.read_text(encoding="utf-8", errors="replace")
    cells = text.count("New MAP-Elites cell occupied")
    scores = []
    for line in text.splitlines():
        i = line.find("combined_score=")
        if i != -1:
            try:
                scores.append(float(line[i + len("combined_score=") :].split(",")[0].split()[0]))
            except ValueError:
                pass
    length_rejects = text.count("exceeds maximum length")
    return {
        "oe_cells_occupied": cells or "",
        "oe_max_score": (max(scores) if scores else ""),
        "oe_length_rejects": length_rejects or "",
    }


def parse_run(run_dir: Path) -> dict[str, Any]:
    pg = _load(run_dir / "patch_generation.json") or {}
    summary = _load(run_dir / "validation_summary_v2.json") or {}

    parse_status = pg.get("parse_status", "")
    if isinstance(parse_status, str) and parse_status.startswith("provider_failure"):
        status = "provider_failure"
    elif isinstance(parse_status, str) and parse_status.startswith(INFRA_FAILURE_PREFIXES):
        status = "infra_failure"
    else:
        status = ""

    cand = (pg.get("patch_candidates") or [{}])
    cand0 = cand[0] if cand else {}
    inner = pg.get("evolve_metrics") or cand0.get("evolve_metrics") or {}

    out: dict[str, Any] = {
        "parse_status": parse_status,
        "no_op": bool(cand0.get("no_op_candidate")),
        "files_touched": len(cand0.get("touched_files") or []),
        "inner_combined_score": inner.get("combined_score", ""),
        "inner_build_tier": inner.get("build_tier", ""),
        "inner_build_pass": inner.get("build_pass", ""),
        "inner_patch_size": inner.get("patch_size", ""),
        "overall": summary.get("overall_result", ""),
        "L1_build": _level_status(summary, 1),
        "L2_runtime": _level_status(summary, 2),
        "L3_obligation": _level_status(summary, 3),
    }
    if status:
        out["status"] = status
    if (run_dir / "evolve").exists():
        out.update(_openevolve_stats(run_dir))
    return out


FIELDS = [
    "strategy", "trial", "status", "exit_code", "parse_status", "no_op",
    "overall", "L1_build", "L2_runtime", "L3_obligation",
    "files_touched", "inner_combined_score", "inner_build_tier", "inner_build_pass",
    "inner_patch_size", "oe_cells_occupied", "oe_max_score", "oe_length_rejects",
    "run_dir", "log",
]


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _summarize(rows: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    trials = [r for r in rows if r.get("strategy") == strategy and r.get("status") != "dry_run"]
    scored = [r for r in trials if r.get("status") not in ("provider_failure", "infra_failure")]
    n = len(scored)
    builds = sum(1 for r in scored if str(r.get("L1_build")) == "PASS")
    nonnoop = sum(1 for r in scored if not r.get("no_op"))
    provider = sum(1 for r in trials if r.get("status") == "provider_failure")
    infra = sum(1 for r in trials if r.get("status") == "infra_failure")
    return {
        "strategy": strategy,
        "trials": len(trials),
        "scored": n,
        "provider_failures": provider,
        "infra_failures": infra,
        "non_noop": nonnoop,
        "build_pass": builds,
        "build_pass_rate": (f"{builds}/{n}" if n else "0/0"),
    }


def write_markdown(rows: list[dict[str, Any]], summaries: list[dict[str, Any]], path: Path, args: argparse.Namespace) -> None:
    lines = [
        "# Evolutionary patch-search validation experiment",
        "",
        f"- Case: `{args.module_path}` {args.old_ref} -> {args.new_ref}"
        + (f" (kbuild `{args.kbuild_target}`)" if args.kbuild_target else ""),
        f"- Kernel config: `{args.kernel_config_target}`; model: `{args.llm_model}`",
        f"- Evolve: iterations={args.evolve_iterations} pop={args.evolve_population_size} "
        f"islands={args.evolve_num_islands} temp={args.evolve_temperature}",
        "",
        "> `build_pass_rate` denominator excludes provider failures (auth/quota) and"
        " infrastructure failures (openevolve unavailable / search crashed), which are"
        " not model/pipeline failures. `build_tier=3` is unreachable in v1 (runtime not"
        " evaluated in the inner loop); see docs/evolve_search_analysis.md.",
        "",
        "## Aggregate",
        "",
        "| strategy | trials | scored | provider_fail | infra_fail | non-no-op | build PASS | rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['strategy']} | {s['trials']} | {s['scored']} | {s['provider_failures']} "
            f"| {s['infra_failures']} | {s['non_noop']} | {s['build_pass']} | {s['build_pass_rate']} |"
        )
    lines += [
        "",
        "## Per-trial",
        "",
        "| strategy | # | status | no_op | L1 build | overall | inner score | tier | files |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("status") == "dry_run":
            continue
        lines.append(
            f"| {r.get('strategy')} | {r.get('trial')} | {r.get('status')} | {r.get('no_op')} "
            f"| {r.get('L1_build')} | {r.get('overall')} | {r.get('inner_combined_score')} "
            f"| {r.get('inner_build_tier')} | {r.get('files_touched')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reparse_rows(out_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy in ("evolutionary", "linear"):
        base = out_root / strategy
        if not base.is_dir():
            continue
        for trial_dir in sorted(base.glob("trial_*")):
            index = int(trial_dir.name.split("_")[-1])
            run_dir = _find_run_dir(trial_dir)
            metrics = parse_run(run_dir) if run_dir else {"status": "no_run_dir"}
            metrics.update(
                {
                    "strategy": strategy,
                    "trial": index,
                    "run_dir": str(run_dir) if run_dir else "",
                    "log": str(trial_dir / "trial_stdout.log"),
                }
            )
            if "status" not in metrics:
                metrics["status"] = "ok"
            rows.append(metrics)
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    args = build_config(argv)
    if not args.dry_run and not args.reparse:
        _preflight_openevolve(args)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    print(
        f"[experiment] case={args.module_path} {args.old_ref}->{args.new_ref} "
        f"n_evolve={args.n_evolve} n_linear={args.n_linear} config={args.kernel_config_target}"
    )
    rows: list[dict[str, Any]] = []
    if args.reparse:
        rows = reparse_rows(out_root)
        if not rows:
            print(f"[experiment] no trial dirs found under {out_root}", file=sys.stderr)
            return 1
    else:
        for i in range(1, args.n_evolve + 1):
            rows.append(run_trial(args, strategy="evolutionary", index=i))
        for i in range(1, args.n_linear + 1):
            rows.append(run_trial(args, strategy="linear", index=i))

    if args.dry_run:
        return 0

    csv_path = out_root / "experiment_results.csv"
    md_path = out_root / "experiment_summary.md"
    summaries = [_summarize(rows, "evolutionary"), _summarize(rows, "linear")]
    write_csv(rows, csv_path)
    write_markdown(rows, summaries, md_path, args)

    print(f"\n[experiment] wrote {csv_path}")
    print(f"[experiment] wrote {md_path}\n")
    for s in summaries:
        print(
            f"  {s['strategy']:12} scored={s['scored']} provider_fail={s['provider_failures']} "
            f"non_noop={s['non_noop']} build_pass={s['build_pass_rate']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
