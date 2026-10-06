"""Replaceable Qt runner: background coordinator plus bounded render pool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt
from PySide6.QtGui import QFontDatabase

from componentpress.application.contracts import BuildProgress, BuildResult
from componentpress.build.engine import BuildEngine
from componentpress.build.input_snapshot import BuildInputSnapshot
from .cancellation import CancellationToken
from .job_store import JobStore
from .signals import BuildSignals


class _CoordinatorTask(QRunnable):
    def __init__(self, engine: BuildEngine, snapshot: BuildInputSnapshot, token: CancellationToken, signals: BuildSignals):
        super().__init__()
        self.engine = engine
        self.snapshot = snapshot
        self.token = token
        self.signals = signals

    def run(self) -> None:
        result = self.engine.execute(self.snapshot, self.token, self.signals.progress.emit)
        self.signals.finished.emit(result)


@dataclass
class _RunningJob:
    snapshot: BuildInputSnapshot
    token: CancellationToken
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

    def start(self, snapshot: BuildInputSnapshot, on_progress=None, on_finished=None) -> str:
        if self._jobs:
            raise RuntimeError("для проекта уже выполняется сборка")
        token = CancellationToken()
        self._jobs[snapshot.job_id] = _RunningJob(snapshot, token, on_progress, on_finished)
        self.coordinator_pool.start(_CoordinatorTask(BuildEngine(self.store), snapshot, token, self.signals))
        return snapshot.job_id

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.token.cancel()
        try:
            self.store.update(job_id, phase="cancelling")
        except Exception:
            pass
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
        for font_id in reversed(job.snapshot.registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        self.store.release(result.job_id)
        if job.finished is not None:
            job.finished(result)
