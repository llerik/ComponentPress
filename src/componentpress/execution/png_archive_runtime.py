"""Qt and file adapter for the synchronous PNG ZIP use case."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import BinaryIO
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

_CREATE_NEW = 1
_DELETE_ON_CLOSE = 0x04000000
_READ_WRITE_DELETE = 0xC0010000
_CREATE_COLLISIONS = (80, 183)


class QtPngArchiveRuntime:
    def __init__(self, root: Path, builder: InputSnapshotBuilder):
        self.store = JobStore(root)
        self.builder = builder
        self._job_id: str | None = None
        self._temporary: Path | None = None
        self._temporary_file: BinaryIO | None = None
        self._copy_temporary: Path | None = None
        self._copy_temporary_file: BinaryIO | None = None
        self._target: Path | None = None
        self._font_ids: tuple[int, ...] = ()
        self._started = False

    def begin(self, job_id: str, source: ProjectSnapshot, target: Path, mode: str) -> Path:
        self._job_id = job_id
        log_event("project.png_export", operation="png_export", status="started", mode=mode, job_id=job_id)
        try:
            self._prepare_target(target, mode)
            directory = self._initialize(job_id, source, mode)
            temporary_directory = None if os.name == "nt" else target.parent
            self._temporary, self._temporary_file = _create_temp_file(temporary_directory)
            return directory
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
            self._log_cleanup(self._close_temp_files())
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
            self._link_target(target, cancellation, progress, len(snapshot.instances))
        except ProjectError:
            raise
        except (OSError, zipfile.BadZipFile) as error:
            raise self._archive_write_error(error, target, snapshot.mode) from error
        self._log_phase("publish", "succeeded", "publishing")
        return PngArchiveResult(target, snapshot.mode, len(snapshot.instances), snapshot.total_copies)

    def _link_target(self, target: Path, cancellation, progress, total: int) -> None:
        self._temporary_file.flush()
        os.fsync(self._temporary_file.fileno())
        cancellation.check()
        try:
            os.link(self._temporary, target)
        except OSError as error:
            if error.errno == errno.EXDEV and os.name == "nt":
                self._copy_to_target_volume(target, cancellation, progress, total)
                return
            if isinstance(error, FileExistsError):
                raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target)) from error
            raise ProjectError(Diagnostic("ARCHIVE_PUBLISH", f"не удалось атомарно опубликовать ZIP: {error}", target)) from error

    def _copy_to_target_volume(self, target: Path, cancellation, progress, total: int) -> None:
        self._copy_temporary, self._copy_temporary_file = _create_temp_file(target.parent)
        self._temporary_file.seek(0)
        while chunk := self._temporary_file.read(1024 * 1024):
            cancellation.check()
            self._copy_temporary_file.write(chunk)
            self._progress(progress, PngArchiveProgress("pack", total, total))
            cancellation.check()
        self._copy_temporary_file.flush()
        os.fsync(self._copy_temporary_file.fileno())
        cancellation.check()
        try:
            os.link(self._copy_temporary, target)
        except FileExistsError as error:
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target)) from error
        except OSError as error:
            raise ProjectError(Diagnostic("ARCHIVE_PUBLISH", f"не удалось атомарно опубликовать ZIP: {error}", target)) from error

    def _pack(self, snapshot, cancellation, progress):
        records = []
        with zipfile.ZipFile(self._temporary_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            self._write_images(archive, snapshot, cancellation, progress, records)
            self._write_manifest(archive, snapshot, records)
        self._temporary_file.flush()

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
        diagnostics = self._close_temp_files()
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
        try:
            log_event(f"project.png_export.{name}", operation="png_export", status=status, job_id=self._job_id, phase=phase)
        except Exception:
            pass

    def _close_temp_files(self) -> list[Diagnostic]:
        diagnostics = []
        for name in ("_copy_temporary_file", "_temporary_file"):
            file = getattr(self, name)
            setattr(self, name, None)
            if file is not None:
                try:
                    file.close()
                except OSError as error:
                    diagnostics.append(self._cleanup_warning("PNG_TEMP_CLEANUP", error))
        if os.name != "nt":
            for name in ("_copy_temporary", "_temporary"):
                path = getattr(self, name)
                setattr(self, name, None)
                try:
                    if path is not None:
                        path.unlink(missing_ok=True)
                except OSError as error:
                    diagnostics.append(self._cleanup_warning("PNG_TEMP_CLEANUP", error))
        return diagnostics

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


def _create_temp_file(directory: Path | None) -> tuple[Path, BinaryIO]:
    if os.name == "nt":
        return _create_windows_temp_file(directory or Path(tempfile.gettempdir()))
    descriptor, name = tempfile.mkstemp(dir=directory)
    return Path(name), os.fdopen(descriptor, "w+b")


def _create_windows_temp_file(directory: Path) -> tuple[Path, BinaryIO]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    for _ in range(5):
        path = directory / f".componentpress-{uuid4().hex}.tmp"
        handle = create_file(str(path), _READ_WRITE_DELETE, 0, None, _CREATE_NEW, _DELETE_ON_CLOSE, None)
        if handle != ctypes.c_void_p(-1).value:
            return _wrap_windows_handle(path, handle, kernel, msvcrt)
        error = ctypes.get_last_error()
        if error not in _CREATE_COLLISIONS:
            raise ctypes.WinError(error)
    raise FileExistsError("could not create a unique temporary file")


def _wrap_windows_handle(path, handle, kernel, msvcrt) -> tuple[Path, BinaryIO]:
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    try:
        return path, os.fdopen(descriptor, "w+b")
    except Exception:
        os.close(descriptor)
        raise
