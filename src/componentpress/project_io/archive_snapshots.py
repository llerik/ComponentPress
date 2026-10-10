"""Consistent, full project snapshots used by project version archives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable
import zipfile

from componentpress.domain.diagnostics import Diagnostic, ProjectError


EXCLUDED_ROOTS = {"archive", "output", ".componentpress", ".git"}


def is_project_version_archive(path: Path) -> bool:
    """Recognize our prior ZIPs without treating every user ZIP as disposable."""
    if path.suffix.casefold() != ".zip":
        return False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            with archive.open("manifest.json", "r") as stream:
                manifest = json.loads(stream.read(1024 * 1024).decode("utf-8"))
        return (
            isinstance(manifest, dict)
            and type(manifest.get("format_version")) is int
            and manifest.get("format_version") == 1
            and isinstance(manifest.get("files"), dict)
        )
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile, json.JSONDecodeError):
        return False


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def snapshot_sources(root: Path, destination: Path, *, cancellation=None, on_progress: Callable | None = None) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Copy source bytes to a private snapshot and record a portable inventory."""
    root = root.resolve()
    files: dict[str, dict[str, object]] = {}
    directories: list[str] = []
    root_names = {item.name.casefold() for item in root.iterdir()}
    if "project.yaml" not in root_names:
        raise ProjectError(Diagnostic("ARCHIVE_PROJECT", "в папке отсутствует project.yaml", root))

    original_files: dict[str, tuple[str, int]] = {}
    original_directories: set[str] = set()
    source_count = sum(len(files) for _folder, _dirs, files in os.walk(root, followlinks=False))
    checked_count = 0
    for folder, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(folder)
        if current == root:
            dir_names[:] = sorted(name for name in dir_names if name.casefold() not in EXCLUDED_ROOTS)
        else:
            original_directories.add(current.relative_to(root).as_posix())
        for name in dir_names:
            path = current / name
            if _is_reparse(path):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", path))
            if current == root and name.casefold() in EXCLUDED_ROOTS:
                continue
            original_directories.add(path.relative_to(root).as_posix())
        for name in file_names:
            path = current / name
            if _is_reparse(path):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", path))
            if is_project_version_archive(path):
                continue
            try:
                with path.open("rb") as stream:
                    original_files[path.relative_to(root).as_posix()] = _digest_stream(stream, cancellation)
                checked_count += 1
                if on_progress is not None:
                    on_progress(checked_count, source_count, "Проверка исходников")
            except OSError as exc:
                raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось проверить исходный файл: {exc}", path)) from exc

    copied_count = 0
    for folder, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(folder)
        relative_folder = current.relative_to(root).as_posix()
        if current == root:
            dir_names[:] = sorted(name for name in dir_names if name.casefold() not in EXCLUDED_ROOTS)
        else:
            directories.append(relative_folder)
        kept: list[str] = []
        for name in sorted(dir_names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if _is_reparse(path):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", path))
            if current == root and name.casefold() in EXCLUDED_ROOTS:
                continue
            directories.append(relative)
            kept.append(name)
        dir_names[:] = kept
        for name in sorted(file_names):
            source = current / name
            if _is_reparse(source):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", source))
            if is_project_version_archive(source):
                continue
            relative = source.relative_to(root).as_posix()
            target = destination.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            try:
                with source.open("rb") as incoming, target.open("xb") as outgoing:
                    while True:
                        if cancellation is not None:
                            cancellation.check()
                        block = incoming.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if _is_reparse(source) or source.stat().st_size != size or (digest.hexdigest(), size) != original_files.get(relative):
                    raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "файл изменился во время создания архива", source))
                copied_count += 1
                if on_progress is not None:
                    on_progress(copied_count, source_count, "Создание снимка")
            except OSError as exc:
                raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось зафиксировать файл: {exc}", source)) from exc
            files[relative] = {"size": size, "sha256": digest.hexdigest()}
    if "project.yaml" not in files:
        raise ProjectError(Diagnostic("ARCHIVE_PROJECT", "в папке отсутствует project.yaml", root))
    if set(files) != set(original_files) or set(directories) != original_directories:
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "состав файлов или папок проекта изменился во время создания архива", root))
    return files, sorted(set(directories))


def _digest_stream(stream, cancellation=None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        if cancellation is not None:
            cancellation.check()
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def verify_source_inventory(root: Path, files: dict[str, dict[str, object]], directories: list[str], *, cancellation=None, on_progress: Callable | None = None) -> None:
    """Detect file edits or additions made after a snapshot was prepared."""
    root = root.resolve()
    current_files: dict[str, dict[str, object]] = {}
    current_directories: set[str] = set()
    checked_count = 0
    for folder, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(folder)
        if current == root:
            dir_names[:] = sorted(name for name in dir_names if name.casefold() not in EXCLUDED_ROOTS)
        else:
            current_directories.add(current.relative_to(root).as_posix())
        for name in dir_names:
            path = current / name
            if _is_reparse(path):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", path))
            if current != root or name.casefold() not in EXCLUDED_ROOTS:
                current_directories.add(path.relative_to(root).as_posix())
        for name in file_names:
            path = current / name
            if _is_reparse(path):
                raise ProjectError(Diagnostic("ARCHIVE_REPARSE_POINT", "архив не поддерживает ссылки и точки повторной обработки", path))
            if is_project_version_archive(path):
                continue
            try:
                with path.open("rb") as stream:
                    digest, size = _digest_stream(stream, cancellation)
            except OSError as exc:
                raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось проверить исходный файл: {exc}", path)) from exc
            current_files[path.relative_to(root).as_posix()] = {"size": size, "sha256": digest}
            checked_count += 1
            if on_progress is not None:
                on_progress(checked_count, len(files), "Повторная проверка исходников")
    if current_files != files or current_directories != set(directories):
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "файлы или папки проекта изменились после создания снимка", root))

