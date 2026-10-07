"""Application use case coordinating save, background snapshot and one job."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEventLoop, QObject, QRunnable, QThreadPool, Qt, Signal

from componentpress.application.contracts import BuildProgress, BuildRequest, BuildResult
from componentpress.application.sessions import ProjectSession
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.copy_mode import validate_copy_mode
from componentpress.execution.cancellation import CancellationToken
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.thread_runner import ThreadBuildRunner
from .project_service import ProjectService


class _PreparationSignals(QObject):
    finished = Signal(object)


class _PreparationTask(QRunnable):
    def __init__(self, builder, source, kwargs, token, signals, job_id):
        super().__init__()
        self.builder = builder
        self.source = source
        self.kwargs = kwargs
        self.token = token
        self.signals = signals
        self.job_id = job_id

    def run(self) -> None:
        try:
            snapshot = self.builder.create(self.source, cancellation=self.token, **self.kwargs)
        except Exception as exc:
            self.signals.finished.emit((self.job_id, None, exc))
        else:
            self.signals.finished.emit((self.job_id, snapshot, None))


@dataclass
class _Preparing:
    token: CancellationToken
    recovery_diagnostics: tuple[Diagnostic, ...]
    on_progress: object
    on_finished: object
    mode: str


class BuildService(QObject):
    def __init__(self, projects: ProjectService, snapshot_builder: InputSnapshotBuilder | None = None):
        super().__init__()
        self.projects = projects
        self.snapshot_builder = snapshot_builder or InputSnapshotBuilder()
        self._store: JobStore | None = None
        self._runner: ThreadBuildRunner | None = None
        self._preparation_pool = QThreadPool(self)
        self._preparation_pool.setMaxThreadCount(1)
        self._preparation_signals = _PreparationSignals(self)
        self._preparation_signals.finished.connect(self._prepared, Qt.ConnectionType.QueuedConnection)
        self._preparing: dict[str, _Preparing] = {}

    def _runtime(self, root: Path) -> tuple[JobStore, ThreadBuildRunner]:
        if self._store is None or self._store.root != root.resolve():
            if self.is_active():
                raise ProjectError(Diagnostic("BUILD_ACTIVE", "нельзя сменить проект во время сборки", root))
            self._store = JobStore(root)
            self._runner = ThreadBuildRunner(self._store)
        assert self._runner is not None
        return self._store, self._runner

    def recover(self, root: Path) -> tuple[Diagnostic, ...]:
        store, _runner = self._runtime(root)
        return store.recover()

    def start(self, session: ProjectSession, request: BuildRequest, *, on_progress=None, on_finished=None) -> str:
        mode = validate_copy_mode(request.mode)
        store, runner = self._runtime(session.snapshot.root)
        if runner.is_active() or self._preparing:
            raise ProjectError(Diagnostic("BUILD_ACTIVE", "для проекта уже выполняется сборка", session.snapshot.root))
        self.projects.save_all(session)
        recovery_diagnostics = store.recover()
        job_id = uuid4().hex
        job_directory = store.initialize(job_id, session.snapshot.model.id)
        store.update(job_id, mode=mode)
        token = CancellationToken()
        self._preparing[job_id] = _Preparing(token, recovery_diagnostics, on_progress, on_finished, mode)
        if on_progress is not None:
            on_progress(BuildProgress(job_id, "preparing", 0, 0, "Подготовка неизменяемого снимка"))
        self._preparation_pool.start(_PreparationTask(
            self.snapshot_builder,
            session.snapshot,
            {
                "job_id": job_id,
                "job_directory": job_directory,
                "component_ids": request.component_ids,
                "mode": mode,
                "print_settings": request.print_settings,
                "memory_budget_bytes": request.memory_budget_bytes,
                "max_render_workers": request.max_render_workers,
                "defer_font_registration": True,
            },
            token,
            self._preparation_signals,
            job_id,
        ))
        return job_id

    def _prepared(self, payload) -> None:
        job_id, snapshot, error = payload
        pending = self._preparing.pop(job_id, None)
        if pending is None or self._store is None or self._runner is None:
            return
        if error is None and pending.token.cancelled:
            error = ProjectError(Diagnostic("BUILD_CANCELLED", "сборка отменена пользователем", self._store.root))
        if error is None:
            try:
                # QFontDatabase is process-global: register the exact used set in
                # this queued main-thread callback before any render task starts.
                snapshot = self.snapshot_builder.register_snapshot_fonts(snapshot)
            except Exception as exc:
                error = exc
        if error is not None:
            cleanup = cleanup_job_directory(self._store, job_id)
            self._store.release(job_id)
            diagnostic = error.diagnostic if isinstance(error, ProjectError) else Diagnostic("BUILD_PREPARATION_FAILED", str(error), self._store.root)
            status = "cancelled" if diagnostic.code == "BUILD_CANCELLED" else "failed"
            result = BuildResult(
                job_id,
                status,
                diagnostics=(*pending.recovery_diagnostics, diagnostic, *cleanup),
                cleanup_state="cleanup_pending" if cleanup else "done",
                mode=pending.mode,
            )
            if pending.on_finished is not None:
                pending.on_finished(result)
            return

        def finished(result: BuildResult) -> None:
            cleanup_pending = any(item.code in {"CLEANUP_PENDING", "CLEANUP_REFUSED"} for item in pending.recovery_diagnostics)
            merged = replace(
                result,
                diagnostics=(*pending.recovery_diagnostics, *result.diagnostics),
                cleanup_state="cleanup_pending" if cleanup_pending else result.cleanup_state,
            )
            if pending.on_finished is not None:
                pending.on_finished(merged)

        try:
            self._runner.start(snapshot, pending.on_progress, finished)
        except Exception as exc:
            from PySide6.QtGui import QFontDatabase

            for font_id in reversed(snapshot.registered_font_ids):
                QFontDatabase.removeApplicationFont(font_id)
            cleanup = cleanup_job_directory(self._store, job_id)
            self._store.release(job_id)
            diagnostic = exc.diagnostic if isinstance(exc, ProjectError) else Diagnostic("BUILD_START_FAILED", str(exc), self._store.root)
            result = BuildResult(
                job_id,
                "failed",
                diagnostics=(*pending.recovery_diagnostics, diagnostic, *cleanup),
                cleanup_state="cleanup_pending" if cleanup else "done",
                mode=pending.mode,
            )
            if pending.on_finished is not None:
                pending.on_finished(result)

    def run(self, session: ProjectSession, request: BuildRequest, *, on_progress=None) -> BuildResult:
        loop = QEventLoop()
        result: BuildResult | None = None

        def finished(value: BuildResult) -> None:
            nonlocal result
            result = value
            loop.quit()

        self.start(session, request, on_progress=on_progress, on_finished=finished)
        loop.exec()
        assert result is not None
        return result

    def cancel(self, job_id: str) -> bool:
        preparing = self._preparing.get(job_id)
        if preparing is not None:
            preparing.token.cancel()
            if self._store is not None:
                try:
                    self._store.update(job_id, phase="cancelling")
                except ProjectError:
                    pass
            return True
        return bool(self._runner and self._runner.cancel(job_id))

    def is_active(self, job_id: str | None = None) -> bool:
        if job_id is None:
            return bool(self._preparing) or bool(self._runner and self._runner.is_active())
        return job_id in self._preparing or bool(self._runner and self._runner.is_active(job_id))
