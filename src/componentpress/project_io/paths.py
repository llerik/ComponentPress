"""Version-one project paths are portable POSIX-style relative paths."""

from pathlib import Path, PurePosixPath
import os
import re

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.schema import PROJECT_MANIFEST

LEGACY_PROJECT_MANIFEST = "project.yaml"


def project_manifest_path(root: Path) -> Path:
    """Resolve the only supported project manifest without changing the tree."""
    manifest = root / PROJECT_MANIFEST
    legacy = root / LEGACY_PROJECT_MANIFEST
    if manifest.is_symlink() or legacy.is_symlink():
        raise ProjectError(Diagnostic(
            "PROJECT_MANIFEST_LINK", "символические ссылки на файл проекта не поддерживаются",
            root, message_key="error.project_manifest_link",
        ))
    if manifest.exists() and legacy.exists():
        raise ProjectError(Diagnostic(
            "PROJECT_MANIFEST_AMBIGUOUS", "проект содержит два файла описания; оставьте только project.cpress",
            root, message_key="error.project_manifest_ambiguous",
        ))
    if legacy.exists():
        raise ProjectError(Diagnostic(
            "PROJECT_MANIFEST_LEGACY", "переименуйте project.yaml в project.cpress вручную",
            legacy, message_key="error.project_manifest_legacy",
        ))
    if not manifest.is_file():
        raise ProjectError(Diagnostic(
            "PROJECT_MANIFEST_MISSING", "файл project.cpress не найден",
            manifest, message_key="error.project_manifest_missing",
        ))
    return manifest


_DEVICE = re.compile(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$")
_INVALID = set('<>:"\\|?*')


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ProjectError(Diagnostic("PATH_INVALID", "путь должен быть относительным"))
    if "\\" in value or "//" in value or re.match(r"^[A-Za-z]:", value):
        raise ProjectError(Diagnostic("PATH_INVALID", "используйте относительный путь с разделителем /"))
    parts = value.split("/")
    if any(part in (".", "..") or not part or part.endswith((" ", ".")) or _DEVICE.match(part) or any(c in _INVALID or ord(c) < 32 for c in part) for part in parts):
        raise ProjectError(Diagnostic("PATH_INVALID", f"непереносимый путь {value!r}"))
    if PurePosixPath(value).as_posix() != value:
        raise ProjectError(Diagnostic("PATH_INVALID", f"непереносимый путь {value!r}"))
    return value


def resolve_project_path(root: Path, value: str) -> Path:
    validate_relative_path(value)
    base = root.resolve()
    candidate = base
    for part in PurePosixPath(value).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки в путях проекта не поддерживаются", candidate))
    target = candidate.resolve()
    if not target.is_relative_to(base):
        raise ProjectError(Diagnostic("PATH_ESCAPE", "путь выходит за пределы проекта", target))
    return target


def check_exact_case(root: Path, value: str) -> None:
    """Reject references relying on case-insensitive lookup before a Linux move."""
    validate_relative_path(value)
    current = root
    for part in value.split("/"):
        if current.is_dir():
            for sibling in current.iterdir():
                if sibling.name.casefold() == part.casefold() and sibling.name != part:
                    raise ProjectError(Diagnostic("PATH_CASE_MISMATCH", f"используйте точный регистр имени {sibling.name!r}", current / part))
        current /= part


def check_case_collisions(root: Path) -> None:
    """Check existing names, including directories, before a project is edited."""
    seen: dict[str, Path] = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        current = Path(folder)
        if current == root:
            dirs[:] = [name for name in dirs if name not in ("output", "archive", ".componentpress", ".git")]
        for name in (*dirs, *files):
            path = current / name
            relative = path.relative_to(root)
            key = relative.as_posix().casefold()
            previous = seen.get(key)
            if previous and previous != relative:
                raise ProjectError(Diagnostic("PATH_CASE_COLLISION", f"конфликт имён {previous} и {relative}", path))
            seen[key] = relative
