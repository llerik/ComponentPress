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
        self.next_index = self.active = self.completed = self.settled = self.revision = 0
        self.active_instances = {}
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
            return self._reserve_locked()

    def _reserve_locked(self):
        if self.cancellation.cancelled or self.next_index == len(self.snapshot.instances):
            return None
        instance = self.snapshot.instances[self.next_index]
        self.next_index += 1
        self.active += 1
        self.active_instances[instance.index] = instance
        return instance

    def _start(self, instance):
        if instance is None:
            return
        if self.cancellation.cancelled:
            return self._complete(instance, None)
        try:
            task = RenderInstanceTask(
                self.snapshot, instance, self.cancellation,
                lambda index, error: self._complete(instance, error),
            )
            self.pool.start(task)
        except Exception as error:
            self._complete(instance, error)

    def _complete(self, instance, error):
        with self.lock:
            self.settled += 1
            self.active -= 1
            self.active_instances.pop(instance.index, None)
            self._remember_error(error)
            should_start = error is None and not self.cancellation.cancelled
            next_instance = self._reserve_locked() if should_start else None
            self.completed += error is None and not self.cancellation.cancelled
            if self.active == 0 and next_instance is None:
                self.finished.set()
            self.revision += 1
            state = self._progress_state_locked()
        self._report_progress(state)
        if next_instance is not None:
            self._start(next_instance)

    def _remember_error(self, error):
        if error is not None and self.error is None:
            self.error = error
            self.cancellation.cancel()

    def _progress_state_locked(self):
        return (
            self.completed, len(self.snapshot.instances),
            tuple(sorted({getattr(item, "component_name", "") for item in self.active_instances.values()} - {""})),
            self.revision,
        )

    def _report_progress(self, value=None):
        try:
            if value is None:
                with self.lock:
                    self.revision += 1
                    value = self._progress_state_locked()
            completed, total, component_names, revision = value
            value = BuildProgress(
                self.snapshot.job_id, "rendering", completed, total,
                tr("ui.png_build_progress", completed=completed, total=total),
                component_names, revision,
            )
            self.progress(value)
        except Exception as error:
            try:
                log_exception("build.progress", error, operation="build", job_id=self.snapshot.job_id,
                              phase="rendering", mode=self.snapshot.mode)
            except Exception:
                pass

    def _raise_error(self):
        if isinstance(self.error, ProjectError):
            raise self.error
        if self.error is not None:
            raise ProjectError(Diagnostic("RENDER_FAILED", str(self.error), self.snapshot.job_directory)) from self.error
        self.cancellation.check()
