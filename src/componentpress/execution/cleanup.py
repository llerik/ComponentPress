from __future__ import annotations

import os
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError


def cleanup_job_directory(store, job_id: str) -> tuple[Diagnostic, ...]:
    directory: Path = store.job_directory(job_id)
    jobs_root = store.jobs_root.resolve()
    try:
        resolved = directory.resolve(strict=False)
    except OSError as exc:
        return (Diagnostic("CLEANUP_PENDING", str(exc), directory, severity="warning"),)
    if directory.is_symlink() or resolved.parent != jobs_root:
        return (Diagnostic("CLEANUP_REFUSED", "отказано в очистке пути вне каталога заданий", directory, severity="warning"),)
    if not directory.exists():
        return ()
    metadata = directory / "job.json"
    errors: list[Diagnostic] = []
    # Delete metadata last so a partial Windows cleanup remains recoverable.
    for root, folders, files in os.walk(directory, topdown=False, followlinks=False):
        current = Path(root)
        for name in files:
            path = current / name
            if path == metadata:
                continue
            try:
                path.unlink()
            except OSError as exc:
                errors.append(Diagnostic("CLEANUP_PENDING", str(exc), path, severity="warning"))
        for name in folders:
            path = current / name
            try:
                path.rmdir()
            except OSError as exc:
                if path.exists():
                    errors.append(Diagnostic("CLEANUP_PENDING", str(exc), path, severity="warning"))
    if errors:
        try:
            store.update(job_id, cleanup_state="cleanup_pending", cleanup_errors=[str(item) for item in errors])
        except ProjectError:
            pass
        return tuple(errors)
    try:
        metadata.unlink(missing_ok=True)
        directory.rmdir()
    except OSError as exc:
        return (Diagnostic("CLEANUP_PENDING", str(exc), directory, severity="warning"),)
    return ()
