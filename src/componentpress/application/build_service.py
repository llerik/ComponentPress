"""Build use case; runtime, persistence and presentation stay at the edges."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from componentpress.application.contracts import BuildInputSnapshot, BuildProgress, BuildRequest, BuildResult
from componentpress.application.ports import BuildRuntime, Cancellation
from componentpress.application.sessions import ProjectSession
from componentpress.domain.copy_mode import validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic, ProjectError


@dataclass
class _Preparing:
    runtime: BuildRuntime
    token: Cancellation
    recovery: tuple[Diagnostic, ...]
    finished: Callable | None
    progress: Callable | None
    mode: str
    snapshot_accepted: bool = False


class BuildService:
    def __init__(self, save_all: Callable[[ProjectSession], None], runtime_factory: Callable[[Path], BuildRuntime], cancellation_factory: Callable[[], Cancellation], prepare_message: Callable[[], str], save_selected=None):
        self.save_all = save_all
        self.save_selected = save_selected
        self.runtime_factory = runtime_factory
        self.cancellation_factory = cancellation_factory
        self.prepare_message = prepare_message
        self._runtimes: dict[Path, BuildRuntime] = {}
        self._preparing: dict[str, _Preparing] = {}

    def _runtime(self, root: Path) -> BuildRuntime:
        key = root.resolve()
        if key not in self._runtimes:
            self._runtimes[key] = self.runtime_factory(key)
        return self._runtimes[key]

    def recover(self, root: Path) -> tuple[Diagnostic, ...]:
        return self._runtime(root).recover(root)

    def start(self, session: ProjectSession, request: BuildRequest, *, on_progress=None, on_finished=None) -> str:
        mode = validate_copy_mode(request.mode)
        root = session.snapshot.root.resolve()
        if self._preparing or any(runtime.is_active() for runtime in self._runtimes.values()):
            raise ProjectError(Diagnostic("BUILD_ACTIVE", "для проекта уже выполняется сборка", root))
        self._save_request(session, request)
        runtime = self._runtime(root)
        recovery = runtime.recover(root)
        job_id = uuid4().hex
        token = self.cancellation_factory()
        directory = runtime.accept(job_id, session.snapshot.model.id, mode)
        pending = _Preparing(runtime, token, recovery, on_finished, on_progress, mode)
        self._preparing[job_id] = pending
        try:
            self._progress(pending, BuildProgress(job_id, "preparing", 0, 0, self.prepare_message()))
            runtime.prepare(session.snapshot, request, job_id, directory, token, lambda snapshot, error: self._prepared(job_id, snapshot, error))
        except Exception as error:
            if job_id in self._preparing:
                self._finish(job_id, runtime.fail(job_id, None, error, mode))
        return job_id

    def _save_request(self, session: ProjectSession, request: BuildRequest) -> None:
        if request.component_ids is not None and self.save_selected is not None:
            self.save_selected(session, request.component_ids)
        else:
            self.save_all(session)

    def _prepared(self, job_id: str, snapshot: BuildInputSnapshot | None, error: Exception | None) -> None:
        pending = self._preparing.get(job_id)
        if pending is None:
            return
        if error is None and pending.token.cancelled:
            error = ProjectError(Diagnostic("BUILD_CANCELLED", "сборка отменена пользователем"))
        if error is not None:
            self._finish(job_id, pending.runtime.fail(job_id, snapshot, error, pending.mode))
            return
        pending.snapshot_accepted = True
        try:
            snapshot = pending.runtime.register_fonts(snapshot)
            self._progress(pending, BuildProgress(job_id, "preparing", 1, 1, self.prepare_message()))
            pending.runtime.start(snapshot, pending.token, lambda value: self._progress(pending, value), lambda result: self._running_finished(job_id, result))
        except Exception as start_error:
            if job_id in self._preparing:
                self._finish(job_id, pending.runtime.fail(job_id, snapshot, start_error, pending.mode))

    def _running_finished(self, job_id: str, result: BuildResult) -> None:
        pending = self._preparing.get(job_id)
        if pending is not None:
            self._finish(job_id, result)

    def _finish(self, job_id: str, result: BuildResult) -> None:
        pending = self._preparing.pop(job_id, None)
        if pending is None:
            return
        cleanup_pending = any(item.code in {"CLEANUP_PENDING", "CLEANUP_REFUSED"} for item in pending.recovery)
        result = replace(
            result,
            diagnostics=(*pending.recovery, *result.diagnostics),
            cleanup_state="cleanup_pending" if cleanup_pending else result.cleanup_state,
        )
        if pending.finished is not None:
            try:
                pending.finished(result)
            except Exception:
                pass

    def cancel(self, job_id: str) -> bool:
        pending = self._preparing.get(job_id)
        if pending is not None:
            pending.token.cancel()
            return pending.runtime.cancel(job_id, pending.token)
        return False

    def is_active(self, job_id: str | None = None) -> bool:
        return (bool(self._preparing) if job_id is None else job_id in self._preparing) or any(runtime.is_active(job_id) for runtime in self._runtimes.values())

    def is_preparing(self, job_id: str) -> bool:
        pending = self._preparing.get(job_id)
        return pending is not None and not pending.snapshot_accepted

    @staticmethod
    def _progress(pending: _Preparing, value: BuildProgress) -> None:
        if pending.progress is not None:
            try:
                pending.progress(value)
            except Exception:
                pass
