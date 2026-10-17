"""Qt and file adapter for the synchronous PNG ZIP use case."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import zipfile
from uuid import uuid4

from PySide6.QtGui import QFontDatabase

from componentpress.application.contracts import BuildInputSnapshot, PngArchiveProgress, PngArchiveResult, ProjectSnapshot
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.tasks import RenderInstanceTask
from componentpress.rendering.snapshot_fonts import register_snapshot_fonts


class QtPngArchiveRuntime:
    def __init__(self, root: Path, builder: InputSnapshotBuilder):
        self.store = JobStore(root)
        self.builder = builder
        self._job_id: str | None = None
        self._temporary: Path | None = None
        self._target: Path | None = None
        self._font_ids: tuple[int, ...] = ()
        self._started = False

    def begin(self, job_id: str, source: ProjectSnapshot, target: Path, mode: str) -> Path:
        self._job_id = job_id
        log_event("project.png_export", operation="png_export", status="started", mode=mode, job_id=job_id)
        try:
            self._prepare_target(target, mode)
            self._temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            return self._initialize(job_id, source, mode)
        except OSError as error:
            self._abort_begin(job_id)
            raise self._archive_write_error(error, target, mode) from error
        except Exception:
            self._abort_begin(job_id)
            raise

    def _prepare_target(self, target: Path, mode: str) -> None:
        self._validate_target(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._target = target

    def _initialize(self, job_id: str, source: ProjectSnapshot, mode: str) -> Path:
        directory = self.store.initialize(job_id, source.model.id)
        self._started = True
        self.store.update(job_id, operation="png_export", mode=mode)
        return directory

    @staticmethod
    def _archive_write_error(error: Exception, target: Path, mode: str) -> ProjectError:
        incident = log_exception("project.png_export", error, operation="png_export", status="failed", mode=mode, code="ARCHIVE_WRITE")
        return ProjectError(Diagnostic("ARCHIVE_WRITE", "Не удалось создать ZIP-архив", target, incident_id=incident, message_key="error.png_export_failed"))

    @staticmethod
    def _validate_target(target: Path) -> None:
        if target.exists():
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target))
        if target.suffix.casefold() != ".zip":
            raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "для PNG-архива выберите файл .zip", target))

    def _abort_begin(self, job_id: str) -> None:
        if job_id in self.store._locks:
            try:
                warnings = cleanup_job_directory(self.store, job_id)
                self._log_cleanup(warnings)
            except Exception as error:
                log_exception("project.png_export.cleanup", error, operation="png_export", status="cleanup_pending", job_id=job_id, code="CLEANUP_PENDING")
            finally:
                self.store.release(job_id)
                self._started = False

    @staticmethod
    def _log_cleanup(diagnostics) -> None:
        for diagnostic in diagnostics:
            log_event("project.png_export.cleanup", operation="png_export", status="cleanup_pending", code=diagnostic.code)

    def prepare(self, source: ProjectSnapshot, directory: Path, component_ids, mode: str, cancellation) -> BuildInputSnapshot:
        self._log_phase("prepare", "started", "preparation")
        snapshot = self.builder.create(source, job_id=self._job_id, job_directory=directory,
                                       component_ids=component_ids, mode=mode, cancellation=cancellation,
                                       defer_font_registration=True)
        try:
            snapshot = register_snapshot_fonts(snapshot)
        except OSError as error:
            raise self._archive_write_error(error, self._target, mode) from error
        self._font_ids = snapshot.registered_font_ids
        self._log_phase("prepare", "succeeded", "preparation")
        return snapshot

    def render(self, snapshot: BuildInputSnapshot, cancellation, progress) -> None:
        self._log_phase("render", "started", "rendering")
        total = len(snapshot.instances)
        self._progress(progress, PngArchiveProgress("render", 0, total))
        for index, instance in enumerate(snapshot.instances, 1):
            self._render_one(snapshot, instance, cancellation)
            self._progress(progress, PngArchiveProgress("render", index, total))
        self._log_phase("render", "succeeded", "rendering")

    @staticmethod
    def _render_one(snapshot, instance, cancellation) -> None:
        errors = []
        RenderInstanceTask(snapshot, instance, cancellation, lambda _index, error: errors.append(error) if error else None).run()
        if errors:
            error = errors[0]
            if isinstance(error, ProjectError):
                raise error
            raise ProjectError(Diagnostic("PNG_EXPORT_FAILED", str(error), instance.document_path)) from error

    @staticmethod
    def _progress(progress, value: PngArchiveProgress) -> None:
        if progress is not None:
            progress(value)

    def publish(self, snapshot, target: Path, cancellation, progress) -> PngArchiveResult:
        try:
            self._log_phase("pack", "started", "export")
            self._pack(snapshot, cancellation, progress)
            self._log_phase("pack", "succeeded", "export")
            cancellation.check()
            self._log_phase("publish", "started", "publishing")
            self._link_target(target)
            self._log_phase("publish", "succeeded", "publishing")
        except ProjectError:
            raise
        except (OSError, zipfile.BadZipFile) as error:
            raise self._archive_write_error(error, target, snapshot.mode) from error
        return PngArchiveResult(target, snapshot.mode, len(snapshot.instances), snapshot.total_copies)

    def _link_target(self, target: Path) -> None:
        try:
            os.link(self._temporary, target)
        except FileExistsError as error:
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target)) from error
        except OSError as error:
            raise ProjectError(Diagnostic("ARCHIVE_PUBLISH", f"не удалось атомарно опубликовать ZIP: {error}", target)) from error

    def _pack(self, snapshot, cancellation, progress):
        records = []
        with zipfile.ZipFile(self._temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            self._write_images(archive, snapshot, cancellation, progress, records)
            self._write_manifest(archive, snapshot, records)

    def _write_images(self, archive, snapshot, cancellation, progress, records) -> None:
        for index, instance in enumerate(snapshot.instances, 1):
            cancellation.check()
            source = snapshot.staging_directory / "images" / instance.png_name
            data = source.read_bytes()
            relative = f"images/{instance.png_name}"
            self._write_png(archive, relative, data)
            records.append(self._record(instance, relative, data))
            self._progress(progress, PngArchiveProgress("pack", index, len(snapshot.instances)))

    @staticmethod
    def _write_png(archive, relative: str, data: bytes) -> None:
        info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)

    @staticmethod
    def _record(instance, relative: str, data: bytes) -> dict:
        return {"component_id": instance.component_id, "instance_id": instance.instance_id,
                "row_number": instance.row_number, "copies": instance.copies,
                "path": relative, "sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _write_manifest(archive, snapshot, records) -> None:
        manifest = {"manifest_version": 1, "project_id": snapshot.project.id, "mode": snapshot.mode,
                    "input_hashes": dict(sorted(snapshot.input_hashes.items())),
                    "counts": {"png": len(snapshot.instances), "copies": snapshot.total_copies},
                    "instances": records}
        info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def finish(self, job_id: str, published: bool, error: Exception | None) -> tuple[Diagnostic, ...]:
        self._log_phase("cleanup", "started", "cleanup")
        diagnostics = self._cleanup_temp()
        diagnostics.extend(self._release_fonts())
        diagnostics.extend(self._cleanup_job(job_id))
        try:
            self.store.release(job_id)
        except Exception as release_error:
            diagnostics.append(self._cleanup_warning("PNG_LOCK_RELEASE", release_error))
        try:
            self._log_finish(published, error, diagnostics)
        except Exception:
            pass
        self._log_phase("cleanup", "cleanup_pending" if diagnostics else "succeeded", "cleanup")
        return tuple(diagnostics)

    def _log_phase(self, name: str, status: str, phase: str) -> None:
        log_event(f"project.png_export.{name}", operation="png_export", status=status, job_id=self._job_id, phase=phase)

    def _cleanup_temp(self) -> list[Diagnostic]:
        try:
            if self._temporary is not None:
                self._temporary.unlink(missing_ok=True)
        except OSError as error:
            return [self._cleanup_warning("PNG_TEMP_CLEANUP", error)]
        return []

    def _release_fonts(self) -> list[Diagnostic]:
        diagnostics = []
        for font_id in reversed(self._font_ids):
            try:
                QFontDatabase.removeApplicationFont(font_id)
            except Exception as error:
                diagnostics.append(self._cleanup_warning("PNG_FONT_CLEANUP", error))
        return diagnostics

    def _cleanup_job(self, job_id: str) -> list[Diagnostic]:
        if not self._started:
            return []
        try:
            return list(cleanup_job_directory(self.store, job_id))
        except Exception as error:
            return [self._cleanup_warning("CLEANUP_PENDING", error)]

    @staticmethod
    def _cleanup_warning(code: str, error: Exception) -> Diagnostic:
        return Diagnostic(code, str(error), severity="warning")

    def _log_finish(self, published: bool, error: Exception | None, diagnostics: list[Diagnostic]) -> None:
        if published:
            log_event("project.png_export", operation="png_export", status="succeeded", job_id=self._job_id)
        elif error is not None:
            status = "cancelled" if isinstance(error, ProjectError) and error.diagnostic.code == "BUILD_CANCELLED" else "failed"
            code = error.diagnostic.code if isinstance(error, ProjectError) else None
            log_exception("project.png_export", error, operation="png_export", status=status, job_id=self._job_id, code=code)
        for diagnostic in diagnostics:
            log_event("project.png_export.cleanup", operation="png_export", status="cleanup_pending", job_id=self._job_id, code=diagnostic.code)
