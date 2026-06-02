from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil

from .repo_utils import RepositoryAccessor
from .kbuild_target import KbuildTargetResolution, resolve_kbuild_target
from .utils import ensure_dir, write_json, write_text


SOURCE_EXTENSIONS = {
    ".c",
    ".h",
    ".rs",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".hh",
    ".S",
    ".s",
}


@dataclass
class ModuleSourceResult:
    module_path: str
    old_ref: str | None
    new_ref: str | None
    old_exists: bool
    new_path_exists: bool
    source_files_old: list[str]
    headers_old: list[str]
    module_loc_old: int
    copied_root: Path
    case_scope: str = "directory"
    kbuild_target: str | None = None
    kbuild_target_resolution: dict | None = None



def run_module_source_loader(
    accessor: RepositoryAccessor,
    *,
    module_path: str,
    extracted_dir: Path,
    restrict_files_old: set[str] | None = None,
    kbuild_target: str | None = None,
) -> ModuleSourceResult:
    ensure_dir(extracted_dir)
    old_exists = accessor.module_path_exists("old", module_path)
    new_exists = accessor.module_path_exists("new", module_path)

    target_resolution: KbuildTargetResolution | None = None
    if old_exists and kbuild_target:
        target_resolution = resolve_kbuild_target(
            accessor,
            side="old",
            module_path=module_path,
            kbuild_target=kbuild_target,
            include_all_headers=True,
        )
        if not target_resolution.source_files:
            raise ValueError(
                f"--kbuild-target {kbuild_target!r} under {module_path!r} resolved no old source files; "
                f"warnings={target_resolution.warnings}"
            )
        files = target_resolution.files
    else:
        files = accessor.list_module_files("old", module_path, extensions=SOURCE_EXTENSIONS) if old_exists else []
    if restrict_files_old is not None:
        allow = set(restrict_files_old)
        files = [f for f in files if f in allow]
    headers = [f for f in files if Path(f).suffix.lower() in {".h", ".hpp", ".hh"}]

    copied_root = extracted_dir / "old_module_sources"
    ensure_dir(copied_root)

    module_loc = 0
    source_texts: dict[str, str] = {}
    for rel in files:
        text = accessor.read_file("old", rel)
        source_texts[rel] = text
        module_loc += len(text.splitlines())
        dst = copied_root / rel
        ensure_dir(dst.parent)
        write_text(dst, text)

    file_list_txt = extracted_dir / "old_module_file_list.txt"
    write_text(file_list_txt, "\n".join(files) + ("\n" if files else ""))

    payload = {
        "module_path": module_path,
        "case_scope": "kbuild_target" if kbuild_target else "directory",
        "kbuild_target": kbuild_target,
        "kbuild_target_resolution": target_resolution.to_json() if target_resolution else None,
        "old_ref": accessor.old_ref,
        "new_ref": accessor.new_ref,
        "old_exists": old_exists,
        "new_path_exists": new_exists,
        "source_files_old": files,
        "headers_old": headers,
        "module_loc_old": module_loc,
        "local_include_scope_risks": _local_include_scope_risks(files, source_texts),
        "note": (
            "module path is user-provided; kbuild target source scope was resolved from old Makefile/Kbuild"
            if kbuild_target
            else (
                "module path is user-provided; no buildable unit inference performed"
                if restrict_files_old is None
                else "module path is user-provided; old files restricted by caller filter"
            )
        ),
    }
    write_json(extracted_dir / "module_source.json", payload)

    return ModuleSourceResult(
        module_path=module_path,
        old_ref=accessor.old_ref,
        new_ref=accessor.new_ref,
        case_scope="kbuild_target" if kbuild_target else "directory",
        kbuild_target=kbuild_target,
        kbuild_target_resolution=target_resolution.to_json() if target_resolution else None,
        old_exists=old_exists,
        new_path_exists=new_exists,
        source_files_old=files,
        headers_old=headers,
        module_loc_old=module_loc,
        copied_root=copied_root,
    )


def _local_include_scope_risks(files: list[str], source_texts: dict[str, str]) -> list[dict]:
    """Report quoted includes that are not present in the provided module scope.

    This does not expand the module or read other driver files. It only makes an
    existing boundary issue explicit so later stages and dataset selection can
    avoid treating incomplete user-provided paths as fully buildable modules.
    """

    file_set = set(files)
    basenames = {Path(f).name: f for f in files}
    risks: list[dict] = []
    for rel, text in sorted(source_texts.items()):
        cur_dir = Path(rel).parent
        for idx, line in enumerate(text.splitlines(), start=1):
            m = re.match(r'\s*#\s*include\s+"([^"]+)"', line)
            if not m:
                continue
            inc = m.group(1)
            direct = (cur_dir / inc).as_posix()
            if direct in file_set:
                continue
            if inc in basenames:
                continue
            risks.append(
                {
                    "file": rel,
                    "line": idx,
                    "include": inc,
                    "reason": "quoted include is not present under the user-provided module_path source scope",
                }
            )
    return risks


def run_external_module_source_loader(
    *,
    source_root: Path,
    module_path: str,
    extracted_dir: Path,
    old_ref: str | None,
    new_ref: str | None,
    new_path_exists: bool = False,
    note: str | None = None,
) -> ModuleSourceResult:
    """Load an out-of-tree driver source snapshot into the normal artifact layout.

    The rest of Driver-Agent expects files to live under
    extracted/old_module_sources/<module_path>/..., so OOT files get a synthetic
    module_path while preserving their original relative paths below that root.
    """
    ensure_dir(extracted_dir)
    source_root = source_root.resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise FileNotFoundError(f"external source root not found: {source_root}")

    copied_root = extracted_dir / "old_module_sources"
    module_root = copied_root / module_path
    if module_root.exists():
        shutil.rmtree(module_root)
    ensure_dir(module_root)

    files: list[str] = []
    headers: list[str] = []
    module_loc = 0
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".mod.c") or path.name in {"Module.symvers", "modules.order"}:
            continue
        if path.name.startswith(".") or "/." in f"/{path.relative_to(source_root).as_posix()}":
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        rel_under_source = path.relative_to(source_root).as_posix()
        rel = f"{module_path.rstrip('/')}/{rel_under_source}"
        files.append(rel)
        if path.suffix.lower() in {".h", ".hpp", ".hh"}:
            headers.append(rel)
        text = path.read_text(encoding="utf-8", errors="replace")
        module_loc += len(text.splitlines())
        dst = copied_root / rel
        ensure_dir(dst.parent)
        write_text(dst, text)

    write_text(extracted_dir / "old_module_file_list.txt", "\n".join(files) + ("\n" if files else ""))

    payload = {
        "module_path": module_path,
        "case_scope": "directory",
        "kbuild_target": None,
        "kbuild_target_resolution": None,
        "old_ref": old_ref,
        "new_ref": new_ref,
        "old_exists": True,
        "new_path_exists": new_path_exists,
        "source_files_old": files,
        "headers_old": headers,
        "module_loc_old": module_loc,
        "source_origin": "out_of_tree",
        "external_source_root": source_root.as_posix(),
        "note": note
        or "out-of-tree module path is synthetic; no buildable unit inference performed",
    }
    write_json(extracted_dir / "module_source.json", payload)

    return ModuleSourceResult(
        module_path=module_path,
        old_ref=old_ref,
        new_ref=new_ref,
        old_exists=True,
        new_path_exists=new_path_exists,
        source_files_old=files,
        headers_old=headers,
        module_loc_old=module_loc,
        copied_root=copied_root,
    )
