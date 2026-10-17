"""Qt and filesystem adapters for the application build scenario."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QFontDatabase

from componentpress.application.contracts import BuildInputSnapshot, BuildRequest, BuildResult, ProjectSnapshot
from componentpress.application.ports import BuildRuntime, Cancellation, WriteLockFactory
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.thread_runner import ThreadBuildRunner
from componentpress.rendering.snapshot_fonts import register_snapshot_fonts


class _PreparationSignals(QObject):
    finished = Signal(object)


class _PreparationTask(QRunnable):
    def __init__(self, builder, source: ProjectSnapshot, request, directory, token, lock, job_id, signals):
        super().__init__()
        self.builder, self.source, self.request = builder, source, request
        self.directory, self.token, self.lock = directory, token, lock
        self.job_id, self.signals = job_id, signals

    def run(self) -> None:
        try:
            with self.lock(self.source.root):
                snapshot = self.builder.create(
                    self.source, job_id=self.job_id, job_directory=self.directory,
                    component_ids=self.request.component_ids, mode=self.request.mode,
                    print_settings=self.request.print_settings,
                    memory_budget_bytes=self.request.memory_budget_bytes,
                    max_render_workers=self.request.max_render_workers,
                    defer_font_registration=True, cancellation=self.token,
                )
        except Exception as error:
            self.signals.finished.emit((self.job_id, None, error))
        else:
            self.signals.finished.emit((self.job_id, snapshot, None))


class QtBuildRuntime:
    def __init__(self, root: Path, builder: InputSnapshotBuilder, write_lock: WriteLockFactory):
        self.root = root.resolve()
        self.builder, self.write_lock = builder, write_lock
        self.store = JobStore(self.root)
        self.runner = ThreadBuildRunner(self.store)
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)
        self.signals = _PreparationSignals()
        self.signals.finished.connect(self._prepared, Qt.ConnectionType.QueuedConnection)
        self._preparing: dict[str, Callable] = {}

    def recover(self, root: Path) -> tuple[Diagnostic, ...]:
        return self.store.recover()

    def accept(self, job_id: str, project_id: str, mode: str) -> Path:
        try:
            directory = self.store.initialize(job_id, project_id)
            self.store.update(job_id, mode=mode)
        except Exception:
            try:
                cleanup_job_directory(self.store, job_id)
            except Exception:
                pass
            self.store.release(job_id)
            raise
        log_event("build.started", operation="build", status="started", job_id=job_id, phase="preparation", mode=mode)
        return directory

    def prepare(self, source: ProjectSnapshot, request: BuildRequest, job_id: str, directory: Path, token: Cancellation, done: Callable) -> None:
        self._preparing[job_id] = done
        try:
            self.pool.start(_PreparationTask(self.builder, source, request, directory, token, self.write_lock, job_id, self.signals))
        except Exception:
            self._preparing.pop(job_id, None)
            raise

    def _prepared(self, payload) -> None:
        job_id, snapshot, error = payload
        callback = self._preparing.pop(job_id, None)
        if callback is not None:
            callback(snapshot, error)

    def register_fonts(self, snapshot: BuildInputSnapshot) -> BuildInputSnapshot:
        return register_snapshot_fonts(snapshot)

    def start(self, snapshot: BuildInputSnapshot, token: Cancellation, progress: Callable, finished: Callable) -> None:
        self.runner.start(snapshot, progress, finished, cancellation=token)

    def fail(self, job_id: str, snapshot: BuildInputSnapshot | None, error: Exception, mode: str) -> BuildResult:
        diagnostic = self._diagnostic(error, job_id, mode)
        cleanup = self._cleanup_unstarted(job_id, snapshot, mode)
        try:
            self.store.release(job_id)
        except Exception as release_error:
            log_exception("build.preparation_release", release_error, operation="build", job_id=job_id, phase="lock_release", mode=mode)
            cleanup = (*cleanup, Diagnostic("CLEANUP_PENDING", "очистку подготовки повторит следующее открытие проекта", severity="warning"))
        return BuildResult(job_id, "cancelled" if diagnostic.code == "BUILD_CANCELLED" else "failed", diagnostics=(diagnostic, *cleanup), cleanup_state="cleanup_pending" if cleanup else "done", mode=mode)

    @staticmethod
    def _diagnostic(error: Exception, job_id: str, mode: str) -> Diagnostic:
        if isinstance(error, ProjectError):
            status = "cancelled" if error.diagnostic.code == "BUILD_CANCELLED" else "failed"
            log_event("build.preparation", operation="build", status=status, job_id=job_id, code=error.diagnostic.code, mode=mode)
            return error.diagnostic
        incident = log_exception("build.preparation", error, operation="build", job_id=job_id, phase="preparation", mode=mode)
        return Diagnostic("BUILD_PREPARATION_FAILED", "Preparation failed", incident_id=incident, message_key="error.preparation_failed")

    def _cleanup_unstarted(self, job_id: str, snapshot: BuildInputSnapshot | None, mode: str) -> tuple[Diagnostic, ...]:
        if snapshot is not None:
            for font_id in reversed(snapshot.registered_font_ids):
                try:
                    QFontDatabase.removeApplicationFont(font_id)
                except Exception as error:
                    log_exception("build.font_cleanup", error, operation="build", job_id=job_id, phase="font_cleanup", mode=mode)
        try:
            return cleanup_job_directory(self.store, job_id)
        except Exception as error:
            incident = log_exception("build.preparation_cleanup", error, operation="build", job_id=job_id, phase="cleanup", mode=mode)
            return (Diagnostic("CLEANUP_PENDING", "очистку подготовки повторит следующее открытие проекта", severity="warning", incident_id=incident),)

    def cancel(self, job_id: str, token: Cancellation) -> bool:
        token.cancel()
        phase = "preparation" if job_id in self._preparing else "rendering"
        log_event("build.cancel_requested", operation="build", status="cancelled", job_id=job_id, phase=phase)
        if job_id in self._preparing:
            try:
                self.store.update(job_id, phase="cancelling")
            except Exception:
                pass
            return True
        return self.runner.cancel(job_id)

    def is_active(self, job_id: str | None = None) -> bool:
        return (bool(self._preparing) or self.runner.is_active()) if job_id is None else job_id in self._preparing or self.runner.is_active(job_id)
