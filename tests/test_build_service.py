from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest, BuildResult
from componentpress.bootstrap import project_service
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.execution.cancellation import CancellationToken
from componentpress.execution.job_store import JobStore
from componentpress.execution.thread_runner import ThreadBuildRunner
from componentpress.rendering.exporter import ensure_gui_application


class FakeRuntime:
    def __init__(self):
        self.callbacks = {}
        self.active: set[str] = set()
        self.start_error: Exception | None = None

    def recover(self, _root):
        return (Diagnostic("RECOVERY_WARNING", "warning"),)

    def accept(self, job_id, _project_id, _mode):
        return Path("jobs") / job_id

    def prepare(self, _session, _request, job_id, _directory, _token, done):
        self.callbacks[job_id] = done
        done(object(), None)

    def register_fonts(self, snapshot):
        return snapshot

    def start(self, snapshot, _token, _progress, finished):
        if self.start_error:
            raise self.start_error
        self.active.add(snapshot.job_id)
        finished(BuildResult(snapshot.job_id, "succeeded", mode="prod"))
        self.active.discard(snapshot.job_id)

    def fail(self, job_id, _snapshot, _error, mode):
        return BuildResult(job_id, "failed", mode=mode)

    def cancel(self, job_id, _token):
        return job_id in self.active

    def is_active(self, job_id=None):
        return bool(self.active) if job_id is None else job_id in self.active


def _service(runtime, saved=None):
    return BuildService(
        lambda session: saved.append(session) if saved is not None else None,
        lambda _root: runtime,
        CancellationToken,
        lambda: "Preparing",
    )


def _session(root):
    return SimpleNamespace(snapshot=SimpleNamespace(root=root, model=SimpleNamespace(id="project")))


def test_sync_completion_is_accepted_once_and_keeps_recovery_diagnostics(tmp_path):
    runtime = FakeRuntime()
    service = _service(runtime)
    results = []

    job_id = service.start(_session(tmp_path), BuildRequest(), on_finished=results.append)

    assert len(results) == 1
    assert results[0].job_id == job_id
    assert [item.code for item in results[0].diagnostics] == ["RECOVERY_WARNING"]
    assert not service.is_active()
    runtime.callbacks[job_id](object(), None)
    assert len(results) == 1


def test_preaccept_failure_raises_without_completion_or_leaked_active_state(tmp_path):
    runtime = FakeRuntime()
    saved = []
    service = _service(runtime, saved)
    results = []

    with pytest.raises(ProjectError):
        service.start(_session(tmp_path), BuildRequest(mode="invalid"), on_finished=results.append)
    assert not saved
    with pytest.raises(OSError):
        service = BuildService(lambda _session: (_ for _ in ()).throw(OSError()), lambda _root: runtime, CancellationToken, lambda: "Preparing")
        service.start(_session(tmp_path), BuildRequest(), on_finished=results.append)
    assert results == []
    assert not service.is_active()


def test_submit_failure_finishes_accepted_job_once_and_ignores_callback_failure(tmp_path):
    runtime = FakeRuntime()
    runtime.start_error = RuntimeError("submit failed")
    service = _service(runtime)
    calls = []

    job_id = service.start(_session(tmp_path), BuildRequest(), on_finished=lambda result: (calls.append(result), (_ for _ in ()).throw(RuntimeError())))

    assert len(calls) == 1
    assert calls[0].job_id == job_id
    assert calls[0].status == "failed"
    assert not service.is_active()


def test_build_preparation_ends_when_snapshot_is_accepted(tmp_path):
    runtime = FakeRuntime()
    service = _service(runtime)
    runtime.prepare = lambda _session, _request, job_id, _directory, _token, done: runtime.callbacks.__setitem__(job_id, done)
    job_id = service.start(_session(tmp_path), BuildRequest())
    assert service.is_preparing(job_id)

    runtime.callbacks[job_id](object(), None)

    assert not service.is_preparing(job_id)
    assert not service.is_active(job_id)


def test_application_build_service_imports_without_qt_or_adapters(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "importlib.import_module('componentpress.application.build_service'); "
        "assert not any(name == 'PySide6' or name.startswith(('PySide6.', 'componentpress.execution.', "
        "'componentpress.project_io.', 'componentpress.build.', 'componentpress.i18n')) for name in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_runner_submit_failure_rolls_back_active_state_and_allows_retry(tmp_path, monkeypatch):
    runner = ThreadBuildRunner(JobStore(tmp_path))
    snapshot = SimpleNamespace(job_id="job-1", registered_font_ids=())

    def fail_submit(_task):
        raise RuntimeError("submit failed")

    monkeypatch.setattr(runner.coordinator_pool, "start", fail_submit)
    with pytest.raises(RuntimeError, match="submit failed"):
        runner.start(snapshot)
    assert not runner.is_active()

    monkeypatch.setattr(runner.coordinator_pool, "start", lambda _task: None)
    assert runner.start(snapshot) == "job-1"
    assert runner.is_active("job-1")
    runner._deliver_finished(BuildResult("job-1", "failed"))
    assert not runner.is_active()


def test_main_window_clears_busy_state_after_synchronous_submit_failure(tmp_path, monkeypatch, qtbot):
    ensure_gui_application()
    import shutil
    from componentpress.editor import main_window
    demo = Path(__file__).resolve().parents[1] / "examples" / "demo-game"
    root = tmp_path / "demo"
    shutil.copytree(demo, root)
    projects = project_service()
    session = projects.open_session(root)
    controller = ProjectController(projects)
    controller.set_session(session)
    runtime = FakeRuntime()
    runtime.start_error = RuntimeError("submit failed")
    controller.build = _service(runtime, saved=[])
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.load_session(session)

    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *_args: None)
    window._start_build(False)
    assert window._active_build_job is not None
    qtbot.waitUntil(lambda: window._active_build_job is None)
    assert window._build_progress is None
    assert window.controller.build is not None and not window.controller.build.is_active()

    window._start_build(False)
    qtbot.waitUntil(lambda: window._active_build_job is None)
    window._active_build_job = "new-job"
    window._queue_build_finished(BuildResult("old-job", "succeeded"))
    qtbot.wait(10)
    assert window._active_build_job == "new-job"
    window._active_build_job = None
    window.close()
