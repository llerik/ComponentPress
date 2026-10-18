"""Bounded PNG rendering for a prepared build snapshot."""

from __future__ import annotations

from threading import Event, Lock

from PySide6.QtCore import QThreadPool

from componentpress.application.contracts import BuildInputSnapshot, BuildProgress
from componentpress.application.ports import Cancellation
from componentpress.diagnostics.logging import log_exception
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.tasks import RenderInstanceTask
from componentpress.i18n import tr


def render_instances(snapshot: BuildInputSnapshot, cancellation: Cancellation, progress) -> None:
    pool = QThreadPool()
    pool.setMaxThreadCount(snapshot.max_render_workers)
    _RenderQueue(pool, snapshot, cancellation, progress).run()


class _RenderQueue:
    def __init__(self, pool, snapshot, cancellation, progress):
        self.pool, self.snapshot = pool, snapshot
        self.cancellation, self.progress = cancellation, progress
        self.lock, self.finished = Lock(), Event()
        self.next_index = self.active = self.completed = 0
        self.error = None

    def run(self):
        initial = [self._reserve() for _ in range(min(self.snapshot.max_render_workers, len(self.snapshot.instances)))]
        if self.active == 0:
            self.finished.set()
        for instance in initial:
            self._start(instance)
        self.finished.wait()
        self.pool.waitForDone()
        self._raise_error()

    def _reserve(self):
        with self.lock:
            if self.cancellation.cancelled or self.next_index == len(self.snapshot.instances):
                return None
            instance = self.snapshot.instances[self.next_index]
            self.next_index += 1
            self.active += 1
            return instance

    def _start(self, instance):
        if instance is None:
            return
        if self.cancellation.cancelled:
            return self._skip_reserved()
        try:
            task = RenderInstanceTask(self.snapshot, instance, self.cancellation, self._complete)
            self.pool.start(task)
        except Exception as error:
            self._complete(instance.index, error)

    def _skip_reserved(self):
        with self.lock:
            self.active -= 1
            if self.active == 0:
                self.finished.set()

    def _complete(self, _index, error):
        with self.lock:
            self.completed += 1
            self.active -= 1
            self._remember_error(error)
            done = self.completed
            should_start = error is None and not self.cancellation.cancelled
            if not should_start and self.active == 0:
                self.finished.set()
        self._report_progress(done)
        if should_start:
            self._start_next()

    def _remember_error(self, error):
        if error is not None and self.error is None:
            self.error = error
            self.cancellation.cancel()

    def _report_progress(self, done):
        try:
            value = BuildProgress(self.snapshot.job_id, "rendering", done, len(self.snapshot.instances),
                                  tr("ui.png_build_progress", completed=done, total=len(self.snapshot.instances)))
            self.progress(value)
        except Exception as error:
            try:
                log_exception("build.progress", error, operation="build", job_id=self.snapshot.job_id,
                              phase="rendering", mode=self.snapshot.mode)
            except Exception:
                pass

    def _start_next(self):
        instance = self._reserve()
        if instance is None:
            with self.lock:
                if self.active == 0:
                    self.finished.set()
        else:
            self._start(instance)

    def _raise_error(self):
        if isinstance(self.error, ProjectError):
            raise self.error
        if self.error is not None:
            raise ProjectError(Diagnostic("RENDER_FAILED", str(self.error), self.snapshot.job_directory)) from self.error
        self.cancellation.check()
