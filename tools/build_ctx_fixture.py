#!/usr/bin/env python3
"""Build the deterministic ctxlab fixture repo (see ctxlab/fixtures.py).

Example:
    python3 tools/build_ctx_fixture.py --dest runs/ctx_fixture_repo
prints the repo path and the expected mining outcome as JSON.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctxlab.fixtures import build_fixture_repo  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dest", required=True)
    p.add_argument("--force", action="store_true", help="remove dest first if it exists")
    args = p.parse_args(argv)
    dest = Path(args.dest)
    if dest.exists():
        if not args.force:
            p.error(f"{dest} exists; pass --force to rebuild")
        shutil.rmtree(dest)
    expected = build_fixture_repo(dest)
    print(json.dumps(expected, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
