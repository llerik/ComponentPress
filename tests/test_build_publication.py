from __future__ import annotations

import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from componentpress.build import engine
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cancellation import CancellationToken
from componentpress.execution import render_pool
from componentpress.execution.job_store import JobStore


class ManualPool:
    def __init__(self):
        self.tasks = []
        self.attempts = 0
        self.failed = threading.Event()
        self.ready = threading.Event()

    def setMaxThreadCount(self, count):
        self.max_workers = count

    def start(self, task):
        self.attempts += 1
        if self.attempts == 2 and getattr(self, "reject_second", False):
            self.failed.set()
            raise OSError("pool rejected task")
        self.tasks.append(task)
        if len(self.tasks) == self.max_workers:
            self.ready.set()

    def waitForDone(self):
        pass


class ImmediatePool:
    def __init__(self):
        self.attempts = 0
        self.waited = False

    def setMaxThreadCount(self, count):
        self.max_workers = count

    def start(self, task):
        self.attempts += 1
        task.run()

    def waitForDone(self):
        self.waited = True


class ManualTask:
    def __init__(self, _snapshot, instance, cancellation, done):
        self.instance, self.cancellation, self.done = instance, cancellation, done

    def run(self):
        error = None
        try:
            self.cancellation.check()
        except ProjectError as caught:
            error = caught
        self.done(self.instance.index, error)


def _snapshot(tmp_path, count=4, workers=2):
    return SimpleNamespace(
        job_id="render-test", mode="prod", max_render_workers=workers,
        instances=tuple(SimpleNamespace(index=index) for index in range(count)),
        root=tmp_path, job_directory=tmp_path / ".componentpress" / "jobs" / "render-test",
    )


def test_renderer_keeps_only_worker_window_submitted_and_refills(monkeypatch):
    pool = ManualPool()
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", ManualTask)
    snapshot = _snapshot(Path("."))
    token = CancellationToken()
    finished = threading.Thread(target=render_pool.render_instances, args=(snapshot, token, lambda _item: None), daemon=True)
    finished.start()
    assert len(pool.tasks) == 2
    pool.tasks.pop(0).run()
    assert len(pool.tasks) == 2
    pool.tasks.pop(0).run()
    pool.tasks.pop(0).run()
    pool.tasks.pop(0).run()
    finished.join(timeout=2)
    assert not finished.is_alive()
    assert pool.attempts == 4


def test_rejected_png_submit_preserves_failure_and_waits_for_accepted_task(monkeypatch):
    pool = ManualPool()
    pool.reject_second = True
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", ManualTask)
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, render_pool.render_instances, _snapshot(Path(".")), CancellationToken(), lambda _item: None), daemon=True)
    thread.start()
    assert pool.failed.wait(timeout=2)
    assert len(pool.tasks) == 1
    pool.tasks[0].run()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert isinstance(errors[0], ProjectError)
    assert errors[0].diagnostic.code == "RENDER_FAILED"
    assert "pool rejected task" in errors[0].diagnostic.message


def test_first_render_error_survives_cancellation_errors_from_other_tasks(monkeypatch):
    pool = ManualPool()

    class CompetingTask(ManualTask):
        def run(self):
            if self.instance.index == 0:
                self.done(self.instance.index, ProjectError(Diagnostic("PRIMARY_RENDER", "render failed")))
            else:
                super().run()

    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", CompetingTask)
    token = CancellationToken()
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, render_pool.render_instances, _snapshot(Path(".")), token, lambda _item: None), daemon=True)
    thread.start()
    assert len(pool.tasks) == 2
    pool.tasks.pop(0).run()
    pool.tasks.pop(0).run()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors[0].diagnostic.code == "PRIMARY_RENDER"


def test_primary_render_error_survives_progress_and_logging_failures(monkeypatch):
    pool = ManualPool()
    primary = ProjectError(Diagnostic("PRIMARY_RENDER", "render failed"))

    class PrimaryThenCancelledTask(ManualTask):
        def run(self):
            error = primary if self.instance.index == 0 else ProjectError(Diagnostic("BUILD_CANCELLED", "cancelled"))
            self.done(self.instance.index, error)

    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", PrimaryThenCancelledTask)
    monkeypatch.setattr(render_pool, "log_exception", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log unavailable")))
    errors = []

    def fail_first(_value):
        raise RuntimeError("consumer failure")

    thread = threading.Thread(
        target=lambda: _capture(errors, render_pool.render_instances, _snapshot(Path(".")), CancellationToken(), fail_first),
        daemon=True,
    )
    thread.start()
    assert pool.ready.wait(timeout=2)
    assert len(pool.tasks) == 2
    pool.tasks.pop(0).run()
    pool.tasks.pop(0).run()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == [primary]


def _capture(errors, function, *args):
    try:
        function(*args)
    except Exception as error:
        errors.append(error)


def test_empty_snapshot_finishes_without_waiting(monkeypatch):
    pool = ManualPool()
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    render_pool.render_instances(_snapshot(Path("."), count=0), CancellationToken(), lambda _item: None)
    assert pool.attempts == 0


def test_cancel_before_initial_reservation_finishes_render_queue(monkeypatch):
    pool = ManualPool()
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", ManualTask)
    token, errors = CancellationToken(), []
    token.cancel()
    thread = threading.Thread(
        target=lambda: _capture(errors, render_pool.render_instances, _snapshot(Path(".")), token, lambda _item: None),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors[0].diagnostic.code == "BUILD_CANCELLED"
    assert pool.attempts == 0


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("failure", ["translation", "construction", "callback"])
def test_notification_failures_do_not_interrupt_render_queue(monkeypatch, workers, failure):
    pool = ImmediatePool()
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", ManualTask)
    log_attempts = []

    def broken_log(*_args, **_kwargs):
        log_attempts.append(True)
        raise OSError("log unavailable")

    monkeypatch.setattr(render_pool, "log_exception", broken_log)
    if failure == "translation":
        monkeypatch.setattr(render_pool, "tr", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("translation failed")))
    elif failure == "construction":
        monkeypatch.setattr(render_pool, "BuildProgress", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("construction failed")))

    snapshot = _snapshot(Path("."), count=workers + 2, workers=workers)

    def notify(_value):
        if failure == "callback":
            raise RuntimeError("consumer failure")

    render_pool.render_instances(snapshot, CancellationToken(), notify)

    assert pool.attempts == workers + 2
    assert len(log_attempts) == workers + 2
    assert pool.waited
    assert pool.max_workers == workers


def test_progress_callback_cancellation_prevents_starting_next_png(monkeypatch):
    pool = ImmediatePool()
    monkeypatch.setattr(render_pool, "QThreadPool", lambda: pool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", ManualTask)
    token = CancellationToken()
    snapshot = _snapshot(Path("."), count=3, workers=1)

    def cancel(_value):
        token.cancel()

    with pytest.raises(ProjectError) as caught:
        render_pool.render_instances(snapshot, token, cancel)

    assert caught.value.diagnostic.code == "BUILD_CANCELLED"
    assert pool.attempts == 1


def test_build_runner_cancel_only_sets_thread_safe_token(tmp_path):
    from componentpress.execution.thread_runner import ThreadBuildRunner, _RunningJob
    from componentpress.rendering.exporter import ensure_gui_application

    ensure_gui_application()
    store = JobStore(tmp_path)
    job_id = "cancel-owner-test"
    store.initialize(job_id, "project")
    store.update(job_id, phase="publishing", output_directory="output/result")
    token = CancellationToken()
    runner = ThreadBuildRunner(store)
    runner._jobs[job_id] = _RunningJob(None, token, None, None)
    assert runner.cancel(job_id)
    assert token.cancelled
    assert store.read(job_id)["phase"] == "publishing"
    assert store.read(job_id)["output_directory"] == "output/result"
    store.release(job_id)


class BookkeepingStore:
    def __init__(self, cancel_on_intent=None):
        self.cancel_on_intent = cancel_on_intent
        self.history_error = None

    def update(self, _job_id, **changes):
        if "output_directory" in changes and self.cancel_on_intent:
            self.cancel_on_intent.cancel()

    def save_history(self, *_args, **_kwargs):
        if self.history_error:
            raise self.history_error


def _build_snapshot(root, job_id="publication-test"):
    directory = root / ".componentpress" / "jobs" / job_id
    staging = directory / "staging"
    staging.mkdir(parents=True)
    (staging / "marker.txt").write_text("complete", encoding="utf-8")
    return SimpleNamespace(job_id=job_id, mode="prod", root=root, job_directory=directory,
                           staging_directory=staging, instances=(), total_copies=0)


def test_cancel_after_intent_does_not_publish_and_keeps_old_output(tmp_path, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    old = tmp_path / "output" / "build-old"
    old.mkdir(parents=True)
    (old / "marker.txt").write_text("old", encoding="utf-8")
    token = CancellationToken()
    store = BookkeepingStore(cancel_on_intent=token)
    engine_instance = engine.BuildEngine(store, lambda *_args: None)
    monkeypatch.setattr(engine_instance, "_prepare_artifacts", lambda *_args: SimpleNamespace(pages=()))
    monkeypatch.setattr(engine, "cleanup_job_directory", lambda *_args: ())
    monkeypatch.setattr(engine.os, "replace", lambda *_args: pytest.fail("cancelled output was published"))

    result = engine_instance.execute(snapshot, token, lambda _item: None)

    assert result.status == "cancelled"
    assert result.output_directory is None
    assert (old / "marker.txt").read_text(encoding="utf-8") == "old"
    assert list((tmp_path / "output").iterdir()) == [old]


def test_cancel_after_rename_keeps_published_success(tmp_path, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    token = CancellationToken()
    store = BookkeepingStore()
    engine_instance = engine.BuildEngine(store, lambda *_args: None)
    monkeypatch.setattr(engine_instance, "_prepare_artifacts", lambda *_args: SimpleNamespace(pages=()))
    monkeypatch.setattr(engine, "cleanup_job_directory", lambda *_args: ())
    real_replace = os.replace

    def publish_then_cancel(source, target):
        real_replace(source, target)
        token.cancel()

    monkeypatch.setattr(engine.os, "replace", publish_then_cancel)

    result = engine_instance.execute(snapshot, token, lambda _item: None)

    assert result.status == "succeeded"
    assert result.output_directory is not None and (result.output_directory / "marker.txt").is_file()


def test_atomic_rename_failure_keeps_previous_output(tmp_path, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    old = tmp_path / "output" / "build-old"
    old.mkdir(parents=True)
    (old / "marker.txt").write_text("old", encoding="utf-8")
    engine_instance = engine.BuildEngine(BookkeepingStore(), lambda *_args: None)
    monkeypatch.setattr(engine_instance, "_prepare_artifacts", lambda *_args: SimpleNamespace(pages=()))
    monkeypatch.setattr(engine, "cleanup_job_directory", lambda *_args: ())
    monkeypatch.setattr(engine.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("rename denied")))

    result = engine_instance.execute(snapshot, CancellationToken(), lambda _item: None)

    assert result.status == "failed"
    assert result.output_directory is None
    assert (old / "marker.txt").read_text(encoding="utf-8") == "old"
    assert list((tmp_path / "output").iterdir()) == [old]


def test_primary_build_diagnostic_survives_history_and_cleanup_failures(tmp_path, monkeypatch):
    snapshot = _build_snapshot(tmp_path)
    store = BookkeepingStore()
    store.history_error = OSError("history busy")
    engine_instance = engine.BuildEngine(store, lambda *_args: None)
    primary = ProjectError(Diagnostic("PRIMARY_BUILD_FAILURE", "primary"))
    monkeypatch.setattr(engine_instance, "_prepare_artifacts", lambda *_args: (_ for _ in ()).throw(primary))
    monkeypatch.setattr(engine, "cleanup_job_directory", lambda *_args: (_ for _ in ()).throw(OSError("cleanup busy")))

    result = engine_instance.execute(snapshot, CancellationToken(), lambda _item: None)

    assert result.status == "failed"
    assert result.diagnostics[0] == primary.diagnostic
    assert [item.code for item in result.diagnostics[1:]] == ["BUILD_BOOKKEEPING_FAILED", "CLEANUP_PENDING"]
