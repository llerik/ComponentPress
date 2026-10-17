"""Stable byte capture helpers shared by build and rendering adapters."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from componentpress.domain.diagnostics import Diagnostic, ProjectError


def read_stable(path: Path) -> tuple[bytes, str]:
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except FileNotFoundError as exc:
        raise ProjectError(Diagnostic("RESOURCE_MISSING", "используемый файл не найден", path)) from exc
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))
    return data, hashlib.sha256(data).hexdigest()


def _copy_chunks(source, target, digest) -> int:
    size = 0
    for block in iter(lambda: source.read(1024 * 1024), b""):
        target.write(block)
        digest.update(block)
        size += len(block)
    target.flush()
    os.fsync(target.fileno())
    return size


def _copy_to_temporary(path: Path, temporary: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    try:
        before = path.stat()
        with path.open("rb") as source, temporary.open("xb") as target:
            size = _copy_chunks(source, target, digest)
        after = path.stat()
        stable = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        if not stable or size != before.st_size:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))
    except ProjectError:
        raise
    except FileNotFoundError as exc:
        raise ProjectError(Diagnostic("RESOURCE_MISSING", "используемый файл не найден", path)) from exc
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
    return digest.hexdigest(), size


def _publish_capture(path: Path, temporary: Path, directory: Path, digest: str) -> Path:
    copied = directory / f"{digest}{path.suffix.lower()}"
    if copied.exists():
        temporary.unlink()
    else:
        os.replace(temporary, copied)
    return copied


def copy_stable(path: Path, directory: Path) -> tuple[Path, str, int]:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".capture-{uuid4().hex}.tmp"
    try:
        digest, size = _copy_to_temporary(path, temporary)
        copied = _publish_capture(path, temporary, directory, digest)
        return copied, digest, size
    finally:
        temporary.unlink(missing_ok=True)
