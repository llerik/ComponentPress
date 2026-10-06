import hashlib
import os
from pathlib import Path
import tempfile

from componentpress.domain.diagnostics import Diagnostic, ProjectError


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, data: bytes, expected_hash: str | None = None) -> str:
    if expected_hash is not None:
        if not path.exists() or file_hash(path) != expected_hash:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменён после чтения", path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".componentpress-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), path)) from exc
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
    return hashlib.sha256(data).hexdigest()


def atomic_create(path: Path, data: bytes) -> str:
    """Publish a new file without replacing a file created by another process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".componentpress-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ProjectError(Diagnostic("FILE_EXISTS", "файл уже существует", path)) from exc
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), path)) from exc
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
    return hashlib.sha256(data).hexdigest()
