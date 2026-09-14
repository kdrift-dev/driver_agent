#!/usr/bin/env python3
"""Contracts for content-pinned samples and immutable run identities (offline)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from ctxlab.provenance import case_fingerprint, write_run_metadata
from draw_ppl_sample import build_manifest, eligible_cases, legacy_dataset_digest
from run_ppl_signal_experiment import select_cases


def main():
    with tempfile.TemporaryDirectory(prefix="driver-agent-provenance-") as directory:
        root = Path(directory)
        dataset = root / "dataset"
        for cid in ("alpha", "beta"):
            pack = dataset / "cases" / cid
            (pack / "old_module_sources").mkdir(parents=True)
            (pack / "case.json").write_text(json.dumps({"case_id": cid, "kernel_symbols": ["new_api"]}))
            (pack / "reference.diff").write_text("-old_api();\n+new_api();\n")
            (pack / "kernel_evidence.json").write_text(json.dumps({"gold": [{"symbol": "new_api"}], "distractors": []}))
            (pack / "old_module_sources" / "driver.c").write_text("int probe(void) { old_api(); }\n")
        manifest = build_manifest(dataset, 2, 0)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        args = argparse.Namespace(dataset_dir=str(dataset), manifest=str(manifest_path), max_cases=0)
        cases = select_cases(args)
        assert {c["case_id"] for c in cases} == {"alpha", "beta"}
        assert args.manifest_validation == "content_v2"
        out = root / "run"
        args.api_key = "must-not-appear"
        first = write_run_metadata(out, args, cases, extra={"phase": "started", "secret": "must-not-appear"})
        original_record = (out / "run_metadata.started.json").read_bytes()
        assert b"must-not-appear" not in original_record
        assert first["selected_cases"]["alpha"]["files"]["reference.diff"]
        print("PASS v2 selection and content provenance without credentials")

        source = dataset / "cases" / "alpha" / "old_module_sources" / "driver.c"
        source.write_text(source.read_text() + "// same IDs, different content\n")
        with patch.dict(os.environ, {"PPL_ALLOW_DIGEST_MISMATCH": "0"}):
            try:
                select_cases(args)
            except SystemExit as exc:
                assert "digest" in str(exc)
            else:
                raise AssertionError("changed source accepted by frozen v2 manifest")
        with patch.dict(os.environ, {"PPL_ALLOW_DIGEST_MISMATCH": "1"}), contextlib.redirect_stdout(io.StringIO()):
            try:
                select_cases(args)
            except SystemExit as exc:
                assert "case contents differ" in str(exc)
            else:
                raise AssertionError("pool bypass bypassed selected pack content hashes")
        print("PASS same-ID content change rejected, including pool-bypass mode")

        final = write_run_metadata(out, args, cases, extra={"phase": "complete"})
        assert (out / "run_metadata.started.json").read_bytes() == original_record
        assert not final["integrity"]["selected_content_unchanged"]
        print("PASS run retains initial identity and detects mid-run input changes")

        legacy = {"tool": "draw_ppl_sample_v1", "case_ids": ["alpha"], "n_selected": 1,
                  "dataset_digest": legacy_dataset_digest(eligible_cases(dataset))}
        manifest_path.write_text(json.dumps(legacy))
        with contextlib.redirect_stdout(io.StringIO()):
            assert select_cases(args)[0]["case_id"] == "alpha"
        assert args.manifest_validation == "legacy_ids_only"
        legacy["case_ids"].append("alpha")
        manifest_path.write_text(json.dumps(legacy))
        try:
            select_cases(args)
        except SystemExit as exc:
            assert "duplicate" in str(exc)
        else:
            raise AssertionError("duplicate selected IDs accepted")
        print("PASS legacy manifest compatibility is explicit; duplicate IDs rejected")

        alias = source.parent / "alias.c"
        alias.symlink_to(source)
        try:
            case_fingerprint(source.parents[1])
        except ValueError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("symlink accepted in frozen pack")
        print("PASS pack symlinks rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
