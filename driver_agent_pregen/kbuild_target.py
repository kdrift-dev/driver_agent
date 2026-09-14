from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import posixpath
import re

from .repo_utils import RepositoryAccessor


SOURCE_SUFFIXES = (".c", ".rs", ".S", ".s")
HEADER_SUFFIXES = {".h", ".hpp", ".hh"}


def source_includes(text: str) -> list[tuple[int, str, str]]:
    """Return literal include directives without mistaking comments for inputs.

    The kind is quoted, angle, or unsupported (for example a macro operand).
    Line numbers refer to the first original line of a continued directive.
    """
    logical_lines: list[str] = []
    original_lines: list[int] = []
    logical, first_line = "", 1
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not logical:
            first_line = lineno
        if line.endswith("\\"):
            logical += line[:-1]
            continue
        logical_lines.append(logical + line)
        original_lines.append(first_line)
        logical = ""
    if logical:
        logical_lines.append(logical)
        original_lines.append(first_line)
    tokens = re.compile(r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    uncommented = tokens.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group())
        if match.group().startswith(("/*", "//")) else match.group(),
        "\n".join(logical_lines),
    )
    result: list[tuple[int, str, str]] = []
    for first_line, line in zip(original_lines, uncommented.splitlines()):
        match = re.match(r"\s*#\s*include\b\s*(.*?)\s*$", line)
        if not match:
            continue
        operand = match.group(1)
        if re.fullmatch(r'"[^"\n]+"', operand):
            result.append((first_line, "quoted", operand[1:-1]))
        elif re.fullmatch(r"<[^>\n]+>", operand):
            result.append((first_line, "angle", operand[1:-1]))
        else:
            result.append((first_line, "unsupported", operand))
    return result


def relative_include_path(including_file: str, include: str) -> str | None:
    """Resolve only the compiler's direct relative quoted-include candidate."""
    if not include or include.startswith("/") or "\\" in include or "\x00" in include:
        return None
    return posixpath.normpath(posixpath.join(posixpath.dirname(including_file), include))


def _local_header_closure(
    accessor: RepositoryAccessor, side: str, module_path: str, source_files: list[str]
) -> tuple[list[str], list[str]]:
    """Read only headers explicitly reachable from owned source includes.

    This deliberately does not infer ccflags include directories or inspect
    unrelated driver headers. Unresolvable local scope is reported as a warning
    so strict consumers cannot silently call it complete.
    """
    prefix = module_path.rstrip("/") + "/"
    pending = list(dict.fromkeys(source_files))
    seen = set(pending)
    headers: list[str] = []
    warnings: list[str] = []
    for rel in pending:
        for line, kind, include in source_includes(accessor.read_file(side, rel)):
            if kind == "angle":
                continue  # Kernel/public includes stay on the kernel-evidence side.
            candidate = relative_include_path(rel, include) if kind == "quoted" else None
            reason = None
            if kind == "unsupported":
                reason = "unsupported include operand"
            elif candidate is None or not candidate.startswith(prefix):
                reason = "quoted include escapes module scope"
            elif Path(candidate).suffix.lower() not in HEADER_SUFFIXES:
                reason = "quoted include is not a supported header"
            elif not accessor.path_exists(side, candidate):
                reason = "quoted header is not found relative to its including file"
            if reason:
                warnings.append(f"{rel}:{line}: {reason}: {include}")
                continue
            if candidate not in seen:
                seen.add(candidate)
                headers.append(candidate)
                pending.append(candidate)
    return headers, warnings


@dataclass
class KbuildTargetResolution:
    module_path: str
    kbuild_target: str
    normalized_target: str
    makefile_path: str
    object_files: list[str]
    source_files: list[str]
    headers: list[str]
    files: list[str]
    warnings: list[str]

    def to_json(self) -> dict:
        return asdict(self)


def _normalize_target(target: str) -> str:
    t = target.strip()
    if not t:
        raise ValueError("empty kbuild target")
    if t.endswith(".ko"):
        return t[:-3] + ".o"
    if not t.endswith(".o"):
        return t + ".o"
    return t


def _logical_makefile_lines(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1].strip() + " "
            continue
        if buf:
            stripped = buf + stripped
            buf = ""
        out.append(stripped)
    if buf:
        out.append(buf.strip())
    return out


def _tokenize_rhs(rhs: str) -> list[str]:
    rhs = rhs.split("#", 1)[0]
    rhs = rhs.replace("\\", " ")
    out: list[str] = []
    for tok in rhs.split():
        tok = tok.strip()
        if not tok or tok.startswith("$("):
            continue
        if tok.endswith(".o") or tok.endswith("/"):
            out.append(tok)
    return out


def _read_makefile(accessor: RepositoryAccessor, side: str, module_path: str) -> tuple[str, str]:
    for name in ("Makefile", "Kbuild"):
        rel = f"{module_path.rstrip('/')}/{name}"
        if accessor.path_exists(side, rel):
            return rel, accessor.read_file(side, rel)
    raise FileNotFoundError(f"no Makefile/Kbuild found under {module_path}")


def _object_to_source(accessor: RepositoryAccessor, side: str, module_path: str, obj: str) -> str | None:
    if not obj.endswith(".o"):
        return None
    stem = obj[:-2]
    for suffix in SOURCE_SUFFIXES:
        rel = f"{module_path.rstrip('/')}/{stem}{suffix}"
        if accessor.path_exists(side, rel):
            return rel
    return None


def resolve_kbuild_target(
    accessor: RepositoryAccessor,
    *,
    side: str,
    module_path: str,
    kbuild_target: str,
    include_all_headers: bool = True,
    include_local_headers: bool = False,
) -> KbuildTargetResolution:
    """Resolve a Kbuild module target to its owning old/new source files.

    This intentionally supports the common Linux module forms only:

    - obj-$(CONFIG_FOO) += foo.o
    - foo-y := a.o b.o
    - foo-objs := a.o b.o
    - foo-$(CONFIG_BAR) += optional.o

    If a target has no composite object list, it resolves to <target>.c when
    that file exists. The caller can still use directory mode by omitting
    --kbuild-target.

    include_all_headers preserves the historical directory-wide header mode.
    Strict production extraction instead explicitly sets it to False and sets
    include_local_headers=True to follow only owned sources' local includes.
    """

    if include_all_headers and include_local_headers:
        raise ValueError("include_all_headers and include_local_headers are mutually exclusive")

    normalized = _normalize_target(kbuild_target)
    stem = normalized[:-2]
    makefile_path, makefile_text = _read_makefile(accessor, side, module_path)
    lines = _logical_makefile_lines(makefile_text)

    obj_targets: list[str] = []
    composite_objs: list[str] = []
    warnings: list[str] = []

    obj_re = re.compile(r"^obj-[^+]*\+=\s*(?P<rhs>.+)$")
    var_re = re.compile(rf"^{re.escape(stem)}-(?:y|m|objs|\$\([^)]+\))\s*(?::=|\+=|=)\s*(?P<rhs>.*)$")

    for line in lines:
        m = obj_re.match(line)
        if m:
            obj_targets.extend(_tokenize_rhs(m.group("rhs")))
        m = var_re.match(line)
        if m:
            composite_objs.extend(_tokenize_rhs(m.group("rhs")))

    if normalized not in obj_targets:
        warnings.append(f"{normalized} was not found in obj-* targets in {makefile_path}")

    object_files = composite_objs or [normalized]
    if include_local_headers:
        include_flag = re.compile(
            r"(?:-I|-iquote|-isystem|-idirafter|"
            r"-iprefix|-iwithprefix(?:before)?|-isysroot|--sysroot|"
            r"-include|-imacros)"
        )
        if any(include_flag.search(line.split("#", 1)[0]) for line in lines):
            warnings.append(
                f"unsupported custom include flags in {makefile_path}; "
                "local header scope does not infer compiler include search paths or forced includes"
            )
    source_files: list[str] = []
    unresolved: list[str] = []
    for obj in object_files:
        if obj.endswith("/"):
            warnings.append(f"ignored subdirectory object token {obj}")
            continue
        src = _object_to_source(accessor, side, module_path, obj)
        if src:
            source_files.append(src)
        else:
            unresolved.append(obj)
    if unresolved:
        warnings.append("could not resolve object source files: " + ", ".join(unresolved))

    headers: list[str] = []
    if include_all_headers:
        headers = [
            f
            for f in accessor.list_module_files(side, module_path, extensions=HEADER_SUFFIXES)
            if f.startswith(module_path.rstrip("/") + "/")
        ]
    elif include_local_headers:
        headers, include_warnings = _local_header_closure(accessor, side, module_path, source_files)
        warnings.extend(include_warnings)

    files: list[str] = []
    seen: set[str] = set()
    for rel in [*source_files, *headers]:
        if rel not in seen:
            seen.add(rel)
            files.append(rel)

    if not source_files:
        warnings.append(f"no source files resolved for kbuild target {kbuild_target}")

    return KbuildTargetResolution(
        module_path=module_path,
        kbuild_target=kbuild_target,
        normalized_target=normalized,
        makefile_path=makefile_path,
        object_files=object_files,
        source_files=source_files,
        headers=headers,
        files=files,
        warnings=warnings,
    )
