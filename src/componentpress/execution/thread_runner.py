"""Replaceable Qt runner: background coordinator plus bounded render pool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt
from PySide6.QtGui import QFontDatabase

from componentpress.application.contracts import BuildInputSnapshot, BuildProgress, BuildResult
from componentpress.application.ports import Cancellation
from componentpress.build.engine import BuildEngine
from componentpress.domain.diagnostics import Diagnostic
from componentpress.diagnostics.logging import log_exception
from .cancellation import CancellationToken
from .job_store import JobStore
from .render_pool import render_instances
from .signals import BuildSignals


class _CoordinatorTask(QRunnable):
    def __init__(self, engine: BuildEngine, snapshot: BuildInputSnapshot, token: Cancellation, signals: BuildSignals):
        super().__init__()
        self.engine = engine
        self.snapshot = snapshot
        self.token = token
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.engine.execute(self.snapshot, self.token, self.signals.progress.emit)
        except Exception as exc:
            incident_id = log_exception("build.coordinator", exc, operation="build", job_id=self.snapshot.job_id, phase="coordinator", mode=self.snapshot.mode)
            try:
                self.engine.store.update(self.snapshot.job_id, phase="failed", cleanup_state="cleanup_pending")
            except Exception as state_error:
                log_exception("build.coordinator_state", state_error, operation="build", job_id=self.snapshot.job_id, phase="cleanup", mode=self.snapshot.mode)
            result = BuildResult(self.snapshot.job_id, "failed", diagnostics=(Diagnostic("BUILD_FAILED", "Unexpected build error", incident_id=incident_id, message_key="error.build_failed"),), cleanup_state="cleanup_pending", mode=self.snapshot.mode)
        self.signals.finished.emit(result)


@dataclass
class _RunningJob:
    snapshot: BuildInputSnapshot
    token: Cancellation
    progress: Callable[[BuildProgress], None] | None
    finished: Callable[[BuildResult], None] | None


class ThreadBuildRunner(QObject):
    def __init__(self, store: JobStore, parent: QObject | None = None):
        super().__init__(parent)
        self.store = store
        self.signals = BuildSignals(self)
        self.coordinator_pool = QThreadPool(self)
        self.coordinator_pool.setMaxThreadCount(1)
        self._jobs: dict[str, _RunningJob] = {}
        self.signals.progress.connect(self._deliver_progress, Qt.ConnectionType.QueuedConnection)
        self.signals.finished.connect(self._deliver_finished, Qt.ConnectionType.QueuedConnection)

    def start(self, snapshot: BuildInputSnapshot, on_progress=None, on_finished=None, *, cancellation: Cancellation | None = None) -> str:
        if self._jobs:
            raise RuntimeError("для проекта уже выполняется сборка")
        token = cancellation or CancellationToken()
        self._jobs[snapshot.job_id] = _RunningJob(snapshot, token, on_progress, on_finished)
        try:
            engine = BuildEngine(self.store, render_instances)
            self.coordinator_pool.start(_CoordinatorTask(engine, snapshot, token, self.signals))
        except Exception:
            self._jobs.pop(snapshot.job_id, None)
            raise
        return snapshot.job_id

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.token.cancel()
        return True

    def is_active(self, job_id: str | None = None) -> bool:
        return bool(self._jobs) if job_id is None else job_id in self._jobs

    def _deliver_progress(self, value: BuildProgress) -> None:
        job = self._jobs.get(value.job_id)
        if job is not None and job.progress is not None:
            job.progress(value)

    def _deliver_finished(self, result: BuildResult) -> None:
        job = self._jobs.pop(result.job_id, None)
        if job is None:
            return
        # Global font registration belongs to the GUI/main thread.
        try:
            for font_id in reversed(job.snapshot.registered_font_ids):
                QFontDatabase.removeApplicationFont(font_id)
        except Exception as exc:
            log_exception("build.font_cleanup", exc, operation="build", job_id=result.job_id, phase="font_cleanup")
        finally:
            try:
                self.store.release(result.job_id)
            except Exception as exc:
                log_exception("build.lock_release", exc, operation="build", job_id=result.job_id, phase="lock_release")
            finally:
                if job.finished is not None:
                    try:
                        job.finished(result)
                    except Exception as exc:
                        log_exception("build.completion_callback", exc, operation="build", job_id=result.job_id, phase="completion")
