"""Deterministic synthetic git repos for exercising every miner code path.

One repo, one scenario per commit on top of a common base:

  api_rename    header fn rename + new flags arg + new macro; 2 callers adapt
  struct_field  header struct field rename; 1 caller adapts
  treewide_sed  header macro rename; 7 callers adapt (tests per-commit cap)
  comment_only  header+driver comment-only touch (must be filtered)
  pure_rename   driver file rename + unrelated header decl (must be filtered)

Commit dates/author are pinned, so SHAs — and therefore case ids and the
seeded per-commit sampling — are stable across runs and machines.
`build_fixture_repo` returns the expected mining outcome next to the repo
path; tools/run_ctxlab_checks.py asserts the miner against it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@ctxlab",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@ctxlab",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}

_ALPHA_H = """\
/* alpha helper API */
struct alpha_dev { int legacy_len; };
extern int alpha_init(struct alpha_dev *ad);
extern void alpha_exit(struct alpha_dev *ad);
"""

_BETA_H = "#define BETA_TIMEOUT_MS 100\n"

_ALPHA_CALLER = """\
#include <linux/alpha.h>

static struct alpha_dev {name}_dev;

static int {name}_probe(void)
{{
	int ret;

	ret = alpha_init(&{name}_dev);
	if (ret)
		return ret;
	{name}_dev.legacy_len = 8;
	return 0;
}}
"""

_BETA_CALLER = """\
#include <linux/beta.h>

static int {name}_wait(void)
{{
	return BETA_TIMEOUT_MS * 2;
}}
"""

_JUNK_C = "static int noop(void) { return 0; }\n"

SED_DRIVERS = [f"drivers/net/s{i}.c" for i in range(1, 8)]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   env={**os.environ, **_GIT_ENV},
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _edit(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    text = path.read_text()
    assert old in text, f"fixture edit target missing in {rel}: {old!r}"
    path.write_text(text.replace(old, new))


def build_fixture_repo(dest: Path) -> dict[str, Any]:
    """Build the repo at dest (must not exist) and return the expected outcome."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", "main")

    _write(dest, "include/linux/alpha.h", _ALPHA_H)
    _write(dest, "include/linux/beta.h", _BETA_H)
    for name, rel in (("a1", "drivers/misc/a1.c"), ("a2", "drivers/misc/a2.c")):
        _write(dest, rel, _ALPHA_CALLER.format(name=name))
    for i, rel in enumerate(SED_DRIVERS, start=1):
        _write(dest, rel, _BETA_CALLER.format(name=f"s{i}"))
    _write(dest, "drivers/misc/junk.c", _JUNK_C)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "base: alpha/beta APIs and callers")
    _git(dest, "tag", "base")

    # api_rename
    _edit(dest, "include/linux/alpha.h",
          "extern int alpha_init(struct alpha_dev *ad);",
          "extern int alpha_setup(struct alpha_dev *ad, unsigned long flags);\n"
          "#define ALPHA_SETUP_SYNC 0x2UL")
    for name, rel in (("a1", "drivers/misc/a1.c"), ("a2", "drivers/misc/a2.c")):
        _edit(dest, rel, f"alpha_init(&{name}_dev);",
              f"alpha_setup(&{name}_dev, ALPHA_SETUP_SYNC);")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "treewide: convert alpha_init to alpha_setup")

    # struct_field
    _edit(dest, "include/linux/alpha.h", "int legacy_len;", "int payload_len;")
    _edit(dest, "drivers/misc/a1.c", "a1_dev.legacy_len = 8;", "a1_dev.payload_len = 8;")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "alpha: rename legacy_len to payload_len")

    # treewide_sed
    _edit(dest, "include/linux/beta.h", "BETA_TIMEOUT_MS", "BETA_TIMEOUT_JIFFIES")
    for rel in SED_DRIVERS:
        _edit(dest, rel, "BETA_TIMEOUT_MS", "BETA_TIMEOUT_JIFFIES")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "treewide: BETA_TIMEOUT_MS becomes jiffies-based")

    # comment_only
    with (dest / "include/linux/beta.h").open("a") as fh:
        fh.write("/* beta_doc_note: timing rationale */\n")
    _edit(dest, "drivers/net/s1.c", "#include <linux/beta.h>",
          "/* beta_doc_note applies here too */\n#include <linux/beta.h>")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "docs: comment-only notes")

    # pure_rename
    _git(dest, "mv", "drivers/misc/junk.c", "drivers/misc/junk_renamed.c")
    with (dest / "include/linux/alpha.h").open("a") as fh:
        fh.write("extern void alpha_reset(struct alpha_dev *ad);\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "misc: rename junk.c; add unrelated alpha_reset decl")
    _git(dest, "tag", "tip")

    sed_gold = ["BETA_TIMEOUT_JIFFIES", "BETA_TIMEOUT_MS"]
    return {
        "repo": str(dest),
        "old_ref": "base",
        "new_ref": "tip",
        # candidate commits touch both prefix groups; all five scenarios qualify
        "expected_candidates": 5,
        # driver file -> exact gold set for every case that must be mined
        "expected_cases": {
            "drivers/misc/a1.c#api_rename": ["ALPHA_SETUP_SYNC", "alpha_init", "alpha_setup"],
            "drivers/misc/a2.c#api_rename": ["ALPHA_SETUP_SYNC", "alpha_init", "alpha_setup"],
            "drivers/misc/a1.c#struct_field": ["legacy_len", "payload_len"],
        },
        # treewide commit: 7 candidates, default cap samples 5, all same gold
        "treewide": {"scale": len(SED_DRIVERS), "sampled": 5, "gold": sed_gold},
        # subjects of commits that must yield zero cases
        "expected_filtered_subjects": [
            "docs: comment-only notes",
            "misc: rename junk.c; add unrelated alpha_reset decl",
        ],
        "expected_total_cases": 3 + 5,
    }
