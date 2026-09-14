#!/usr/bin/env python3
"""Offline synthetic checks for Kbuild-owned source and local-header scope."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver_agent_pregen.kbuild_target import resolve_kbuild_target, source_includes
from driver_agent_pregen.module_source_loader import run_module_source_loader
from driver_agent_pregen.repo_utils import RepositoryAccessor


class GuardedAccessor:
    old_ref, new_ref = "a" * 40, "b" * 40

    def __init__(self, files, allowed):
        self.files = files
        self.allowed = set(allowed)
        self.reads = []
        self.listings = []

    def module_path_exists(self, side, path):
        return True  # Path existence is allowed on either side; contents are not.

    def path_exists(self, side, path):
        assert side == "old", (side, path)
        return path in self.files

    def read_file(self, side, path):
        assert side == "old" and path in self.allowed, (side, path)
        self.reads.append(path)
        return self.files[path]

    def list_module_files(self, side, path, extensions=None):
        assert side == "old"
        self.listings.append(path)
        return sorted(p for p in self.files if p.startswith(path + "/")
                      and (not extensions or Path(p).suffix in extensions))


def resolve(accessor, **kwargs):
    return resolve_kbuild_target(accessor, side="old", module_path="drivers/demo",
                                kbuild_target="demo.ko", **kwargs)


def check_owned_module():
    files = {
        "drivers/demo/Makefile": "obj-$(CONFIG_DEMO) += demo.o sibling/\ndemo-y := core.o\ndemo-$(CONFIG_OPTIONAL) += event.o\n",
        "drivers/demo/core.c": "#include <linux/demo.h>\nint core;\n",
        "drivers/demo/event.c": "#include <linux/irq.h>\nint event;\n",
        "drivers/demo/sibling/private.h": "#error sibling must never be inspected\n",
    }
    owned = ["drivers/demo/core.c", "drivers/demo/event.c"]
    accessor = GuardedAccessor(files, ["drivers/demo/Makefile", *owned])
    with tempfile.TemporaryDirectory() as directory:
        result = run_module_source_loader(accessor, module_path="drivers/demo",
                                          extracted_dir=Path(directory), kbuild_target="demo.ko")
        payload = json.loads((Path(directory) / "module_source.json").read_text())
        assert result.source_files_old == owned
        assert result.headers_old == []
        assert result.module_loc_old == 4
        assert result.kbuild_target_resolution["warnings"] == []
        assert payload["local_include_scope_risks"] == []
        assert sorted(p.relative_to(result.copied_root).as_posix()
                      for p in result.copied_root.rglob("*") if p.is_file()) == sorted(owned)
        assert all((result.copied_root / path).read_text() == files[path] for path in owned)
    assert accessor.listings == []


def check_header_closure():
    files = {
        "drivers/demo/Makefile": "obj-y += demo.o\ndemo-y := sub/main.o helper.o\n",
        "drivers/demo/sub/main.c": '#include "../shared.h"\n#if CONFIG_OPTIONAL\n#include "optional.h"\n#endif\n',
        "drivers/demo/helper.c": '#include "shared.h"\n',
        "drivers/demo/shared.h": '#include "sub/./detail.h"\n',
        "drivers/demo/sub/optional.h": "int optional;\n",
        "drivers/demo/sub/detail.h": '#include "../shared.h"\n',
        "drivers/demo/sibling/private.h": "#error forbidden\n",
    }
    allowed = set(files) - {"drivers/demo/sibling/private.h"}
    accessor = GuardedAccessor(files, allowed)
    with tempfile.TemporaryDirectory() as directory:
        result = run_module_source_loader(accessor, module_path="drivers/demo",
                                          extracted_dir=Path(directory), kbuild_target="demo.ko")
        payload = json.loads((Path(directory) / "module_source.json").read_text())
        assert result.kbuild_target_resolution["source_files"] == ["drivers/demo/sub/main.c", "drivers/demo/helper.c"]
        assert result.headers_old == ["drivers/demo/shared.h", "drivers/demo/sub/optional.h", "drivers/demo/sub/detail.h"]
        assert set(result.source_files_old) == allowed - {"drivers/demo/Makefile"}
        assert result.kbuild_target_resolution["warnings"] == []
        assert payload["local_include_scope_risks"] == []
    assert accessor.listings == []


def check_unresolved_boundary():
    files = {
        "drivers/demo/Makefile": "obj-y += demo.o\n",
        "drivers/demo/demo.c": '#include "../foreign/secret.h"\n#include "/tmp/secret.h"\n#include "missing.h"\n#include "other.c"\n#include HEADER_MACRO\n',
        "drivers/foreign/secret.h": "#error never read\n",
        "drivers/demo/sibling/missing.h": "#error basename guess forbidden\n",
        "drivers/demo/other.c": "#error unrelated source\n",
    }
    accessor = GuardedAccessor(files, ["drivers/demo/Makefile", "drivers/demo/demo.c"])
    result = resolve(accessor, include_all_headers=False, include_local_headers=True)
    assert len(result.warnings) == 5
    assert result.headers == []
    with tempfile.TemporaryDirectory() as directory:
        run_module_source_loader(accessor, module_path="drivers/demo",
                                 extracted_dir=Path(directory), kbuild_target="demo.ko")
        payload = json.loads((Path(directory) / "module_source.json").read_text())
        assert len(payload["local_include_scope_risks"]) == 5
    assert accessor.listings == []


def check_include_lexing():
    text = ('/*\n#include "blocked.h"\n*/\n'
            '// comment continues \\\n#include "also_blocked.h"\n'
            '#include \\\n "ok.h" // trailing comment\n'
            '# /* comment */ include"other.h"\n'
            'const char *s = "#include \\"fake.h\\"";\n'
            '#include <linux/kernel.h>\n#include LOCAL_HEADER\n')
    assert source_includes(text) == [(6, "quoted", "ok.h"), (8, "quoted", "other.h"),
                                     (10, "angle", "linux/kernel.h"), (11, "unsupported", "LOCAL_HEADER")]
    files = {"drivers/demo/Makefile": "obj-y += demo.o\n",
             "drivers/demo/demo.c": text.replace("#include LOCAL_HEADER", ""),
             "drivers/demo/ok.h": "int ok;\n", "drivers/demo/other.h": "int other;\n",
             "drivers/demo/blocked.h": "#error forbidden\n", "drivers/demo/also_blocked.h": "#error forbidden\n"}
    accessor = GuardedAccessor(files, set(files) - {"drivers/demo/blocked.h", "drivers/demo/also_blocked.h"})
    result = resolve(accessor, include_all_headers=False, include_local_headers=True)
    assert result.headers == ["drivers/demo/ok.h", "drivers/demo/other.h"]
    assert result.warnings == []


def check_legacy_and_restriction():
    files = {"drivers/demo/Makefile": "obj-y += demo.o\n",
             "drivers/demo/demo.c": '#include "own.h"\n', "drivers/demo/own.h": "int own;\n",
             "drivers/demo/sibling/private.h": "int sibling;\n"}
    accessor = GuardedAccessor(files, files)
    assert resolve(accessor, include_all_headers=True).headers == ["drivers/demo/own.h", "drivers/demo/sibling/private.h"]
    assert accessor.reads == ["drivers/demo/Makefile"]
    assert resolve(accessor, include_all_headers=False).headers == []
    try:
        resolve(accessor, include_all_headers=True, include_local_headers=True)
    except ValueError:
        pass
    else:
        raise AssertionError("incompatible header modes admitted")
    with tempfile.TemporaryDirectory() as directory:
        run_module_source_loader(accessor, module_path="drivers/demo", extracted_dir=Path(directory),
                                 kbuild_target="demo.ko", restrict_files_old={"drivers/demo/demo.c"})
        payload = json.loads((Path(directory) / "module_source.json").read_text())
        assert len(payload["local_include_scope_risks"]) == 1


def check_custom_include_flags():
    for flag in ("-I$(src)", "-I $(src)/private", "-iquote $(src)", "-isystem $(src)",
                 "-idirafter $(src)", "-Wp,-I$(src)", "-include private.h", "-imacros private.h",
                 "-iquoteprivate", "-isystemprivate", "-includeprivate.h", "-imacrosprivate.h",
                 '"-I$(src)"', "'-I$(src)'"):
        files = {"drivers/demo/Makefile": "obj-y += demo.o\nccflags-y += " + flag + "\n",
                 "drivers/demo/demo.c": "#include <local.h>\n", "drivers/demo/local.h": "#error do not guess include paths\n"}
        accessor = GuardedAccessor(files, ["drivers/demo/Makefile", "drivers/demo/demo.c"])
        result = resolve(accessor, include_all_headers=False, include_local_headers=True)
        assert result.headers == []
        assert len(result.warnings) == 1 and "unsupported custom include flags" in result.warnings[0]
        assert resolve(accessor, include_all_headers=True).warnings == []


def check_real_git_accessor():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git("init", "-q")
        git("config", "user.name", "Scope fixture")
        git("config", "user.email", "scope@example.invalid")
        for path, text in {"drivers/demo/Makefile": "obj-y += demo.o\n",
                           "drivers/demo/demo.c": '#include "own.h"\nint old_source;\n',
                           "drivers/demo/own.h": "int old_header;\n",
                           "drivers/demo/sibling/private.h": "int unrelated;\n"}.items():
            destination = repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        git("add", ".")
        git("commit", "-qm", "old fixture")
        old_ref = git("rev-parse", "HEAD")
        (repo / "drivers/demo/demo.c").write_text("#error new source is not an input\n")
        (repo / "drivers/demo/own.h").write_text("#error new header is not an input\n")
        git("add", ".")
        git("commit", "-qm", "new fixture")
        class GuardedGit(RepositoryAccessor):
            def read_file(self, side, path):
                assert side == "old" and path in {"drivers/demo/Makefile", "drivers/demo/demo.c", "drivers/demo/own.h"}
                return super().read_file(side, path)
            def list_module_files(self, *args, **kwargs):
                raise AssertionError("strict Kbuild extraction must not enumerate sibling headers")
        accessor = GuardedGit(repo=repo, old_ref=old_ref, new_ref=git("rev-parse", "HEAD"))
        result = run_module_source_loader(accessor, module_path="drivers/demo",
                                          extracted_dir=root / "extracted", kbuild_target="demo.ko")
        assert result.source_files_old == ["drivers/demo/demo.c", "drivers/demo/own.h"]
        assert (result.copied_root / "drivers/demo/demo.c").read_text() == '#include "own.h"\nint old_source;\n'
        assert (result.copied_root / "drivers/demo/own.h").read_text() == "int old_header;\n"


def main():
    groups = [check_owned_module, check_header_closure, check_unresolved_boundary,
              check_include_lexing, check_legacy_and_restriction, check_custom_include_flags,
              check_real_git_accessor]
    for check in groups:
        check()
    print(json.dumps({"status": "PASS", "check_count": len(groups),
                      "checks": [check.__name__ for check in groups]}, indent=2))


if __name__ == "__main__":
    main()
