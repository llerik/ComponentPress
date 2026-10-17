"""Coordinate the synchronous PNG archive use case through an adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from uuid import uuid4

from componentpress.application.contracts import PngArchiveProgress, PngArchiveResult, ProjectSnapshot
from componentpress.application.ports import Cancellation, PngArchiveRuntime
from componentpress.domain.copy_mode import validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic


def export_png_archive(
    source: ProjectSnapshot,
    target: Path,
    *,
    runtime: PngArchiveRuntime,
    cancellation: Cancellation,
    mode: str = "prod",
    component_ids: tuple[str, ...] | None = None,
    on_progress: Callable[[PngArchiveProgress], None] | None = None,
) -> PngArchiveResult:
    selected_mode = validate_copy_mode(mode)
    job_id = uuid4().hex
    try:
        result = _export(runtime, source, target, job_id, selected_mode, component_ids, cancellation, on_progress)
    except Exception as error:
        _finish(runtime, job_id, False, error)
        raise
    return _with_cleanup_warnings(result, _finish(runtime, job_id, True, None))


def _export(runtime, source, target, job_id, mode, component_ids, cancellation, progress):
    directory = runtime.begin(job_id, source, target, mode)
    snapshot = runtime.prepare(source, directory, component_ids, mode, cancellation)
    runtime.render(snapshot, cancellation, progress)
    cancellation.check()
    return runtime.publish(snapshot, target, cancellation, progress)


def _finish(runtime, job_id, published, error):
    try:
        return runtime.finish(job_id, published, error)
    except Exception:
        return () if error is not None else (Diagnostic("CLEANUP_PENDING", "PNG ZIP cleanup failed", severity="warning"),)


def _with_cleanup_warnings(result, diagnostics):
    if not diagnostics:
        return result
    return PngArchiveResult(result.path, result.mode, result.png_count, result.copies, diagnostics)
