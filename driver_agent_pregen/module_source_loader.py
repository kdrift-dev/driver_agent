from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repo_utils import RepositoryAccessor
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



def run_module_source_loader(
    accessor: RepositoryAccessor,
    *,
    module_path: str,
    extracted_dir: Path,
) -> ModuleSourceResult:
    ensure_dir(extracted_dir)
    old_exists = accessor.module_path_exists("old", module_path)
    new_exists = accessor.module_path_exists("new", module_path)

    files = accessor.list_module_files("old", module_path, extensions=SOURCE_EXTENSIONS) if old_exists else []
    headers = [f for f in files if Path(f).suffix.lower() in {".h", ".hpp", ".hh"}]

    copied_root = extracted_dir / "old_module_sources"
    ensure_dir(copied_root)

    module_loc = 0
    for rel in files:
        text = accessor.read_file("old", rel)
        module_loc += len(text.splitlines())
        dst = copied_root / rel
        ensure_dir(dst.parent)
        write_text(dst, text)

    file_list_txt = extracted_dir / "old_module_file_list.txt"
    write_text(file_list_txt, "\n".join(files) + ("\n" if files else ""))

    payload = {
        "module_path": module_path,
        "old_ref": accessor.old_ref,
        "new_ref": accessor.new_ref,
        "old_exists": old_exists,
        "new_path_exists": new_exists,
        "source_files_old": files,
        "headers_old": headers,
        "module_loc_old": module_loc,
        "note": "module path is user-provided; no buildable unit inference performed",
    }
    write_json(extracted_dir / "module_source.json", payload)

    return ModuleSourceResult(
        module_path=module_path,
        old_ref=accessor.old_ref,
        new_ref=accessor.new_ref,
        old_exists=old_exists,
        new_path_exists=new_exists,
        source_files_old=files,
        headers_old=headers,
        module_loc_old=module_loc,
        copied_root=copied_root,
    )
