"""Privacy, rotation and recovery behavior for technical diagnostics."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import zipfile

from PySide6.QtCore import QLockFile
from types import SimpleNamespace

from componentpress.diagnostics import logging as diagnostics
from componentpress.build import engine as build_engine_module
from componentpress.execution import render_pool
from componentpress.execution.cancellation import CancellationToken
from componentpress.version import APPLICATION_VERSION


def isolated_log_dir(monkeypatch, tmp_path: Path) -> Path:
    diagnostics.shutdown()
    diagnostics._BUFFER.clear()
    directory = tmp_path / "app-data" / "logs"
    monkeypatch.setattr(diagnostics, "_directory", lambda: directory)
    monkeypatch.setattr(diagnostics, "_MAX_BYTES", 700)
    return directory


def test_rotates_and_reports_without_exception_or_project_text(monkeypatch, tmp_path: Path) -> None:
    directory = isolated_log_dir(monkeypatch, tmp_path)
    active = diagnostics.initialize()
    assert active is not None and active.parent == directory

    for _ in range(40):
        diagnostics.log_event("project.operation", operation="open", status="succeeded", mode="prod")
    try:
        raise RuntimeError("SECRET_FROM_EXCEPTION")
    except RuntimeError as error:
        incident_id = diagnostics.log_exception(
            "project.operation", error, operation="open", status="failed",
            code="PROJECT_OPEN_FAILED", path="SECRET_PROJECT_PATH",
        )
    diagnostics.log_event("project.operation", operation="SECRET_PROJECT_NAME", status="succeeded")

    report = diagnostics.create_report(tmp_path / "support.zip", incident_id=incident_id, code="PROJECT_OPEN_FAILED")
    with zipfile.ZipFile(report) as archive:
        payload = json.loads(archive.read("report.json"))
        contents = b"\n".join(archive.read(name) for name in archive.namelist() if name.endswith(".log"))
    assert payload["runtime"]["version"] == APPLICATION_VERSION
    assert payload["incident"]["incident_id"] == incident_id
    assert b"SECRET_FROM_EXCEPTION" not in contents
    assert b"SECRET_PROJECT_PATH" not in contents
    assert b"SECRET_PROJECT_NAME" not in contents
    assert 1 <= len(list(directory.glob("componentpress-*.log*"))) <= 4
    assert all(path.stat().st_size <= 700 for path in directory.glob("componentpress-*.log*"))
    diagnostics.shutdown()


def test_report_snapshots_rotating_log_while_writer_rolls_over(monkeypatch, tmp_path: Path) -> None:
    directory = isolated_log_dir(monkeypatch, tmp_path)
    active = diagnostics.initialize()
    assert active is not None
    diagnostics.log_event("report.snapshot", operation="open")
    handler = diagnostics._LOGGER.handlers[0]
    handler.maxBytes = 1
    entered_open, continue_open = threading.Event(), threading.Event()
    reporter_requested_lock = threading.Event()
    real_open = handler._open
    real_acquire = handler.acquire

    def pause_after_rollover():
        entered_open.set()
        assert continue_open.wait(2)
        return real_open()

    def track_report_lock():
        if threading.current_thread().name == "support-report":
            reporter_requested_lock.set()
        return real_acquire()

    handler._open = pause_after_rollover
    handler.acquire = track_report_lock
    errors = []
    writer = threading.Thread(target=lambda: _capture_log(errors), daemon=True)
    writer.start()
    assert entered_open.wait(2)
    reports = []
    reporter = threading.Thread(
        target=lambda: reports.append(diagnostics.create_report(tmp_path / "rotating.zip")),
        name="support-report", daemon=True,
    )
    reporter.start()
    try:
        assert reporter_requested_lock.wait(2)
    finally:
        continue_open.set()
    writer.join(timeout=2)
    reporter.join(timeout=2)
    assert not writer.is_alive() and not reporter.is_alive()
    assert not errors
    with zipfile.ZipFile(reports[0]) as archive:
        contents = b"\n".join(archive.read(name) for name in archive.namelist() if name.startswith("session.log"))
        assert contents.count(b"report.snapshot") == 1
        assert contents.count(b"report.rollover") == 1
    diagnostics.shutdown()


def _capture_log(errors: list[Exception]) -> None:
    try:
        diagnostics.log_event("report.rollover", operation="open")
    except Exception as error:
        errors.append(error)


def test_logging_continues_while_report_compresses_snapshot(monkeypatch, tmp_path: Path) -> None:
    isolated_log_dir(monkeypatch, tmp_path)
    assert diagnostics.initialize() is not None
    diagnostics.log_event("report.before_snapshot", operation="open")
    writing, continue_archive, logged = threading.Event(), threading.Event(), threading.Event()
    original_writestr = zipfile.ZipFile.writestr

    def pause_log_entry(archive, name, data, *args, **kwargs):
        if str(name).startswith("session.log"):
            writing.set()
            assert continue_archive.wait(2)
        return original_writestr(archive, name, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", pause_log_entry)
    errors = []
    reporter = threading.Thread(target=lambda: _capture_report(errors, tmp_path / "snapshot.zip"), daemon=True)
    reporter.start()
    assert writing.wait(2)
    writer = threading.Thread(target=lambda: _capture_log_event(logged, errors), daemon=True)
    writer.start()
    try:
        assert logged.wait(2)
    finally:
        continue_archive.set()
    reporter.join(timeout=2)
    writer.join(timeout=2)
    assert not reporter.is_alive() and not writer.is_alive()
    assert not errors
    diagnostics.shutdown()


def _capture_report(errors: list[Exception], target: Path) -> None:
    try:
        diagnostics.create_report(target)
    except Exception as error:
        errors.append(error)


def _capture_log_event(completed: threading.Event, errors: list[Exception]) -> None:
    _capture_log(errors)
    completed.set()


def test_prune_keeps_active_logs_and_unknown_files(monkeypatch, tmp_path: Path) -> None:
    directory = isolated_log_dir(monkeypatch, tmp_path)
    directory.mkdir(parents=True)
    current = directory / "componentpress-current.log"
    active = directory / "componentpress-active.log"
    expired = directory / "componentpress-expired.log"
    unknown = directory / "keep-me.txt"
    active.write_text("active", encoding="utf-8")
    expired.write_text("expired", encoding="utf-8")
    unknown.write_text("user file", encoding="utf-8")
    old_time = time.time() - (diagnostics._MAX_AGE_DAYS + 2) * 86400
    os.utime(active, (old_time, old_time))
    os.utime(expired, (old_time, old_time))
    lock = QLockFile(str(active.with_suffix(".active")))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    try:
        diagnostics._prune(directory, current)
        assert active.exists()
        assert not expired.exists()
        assert unknown.read_text(encoding="utf-8") == "user file"
    finally:
        lock.unlock()


def test_log_sink_failure_is_nonfatal(monkeypatch, tmp_path: Path) -> None:
    isolated_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostics._LOGGER, "log", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(diagnostics, "_LOG_PATH", tmp_path / "active.log")
    diagnostics.log_event("build.failed", operation="build", status="failed", code="BUILD_FAILED")


def test_progress_callback_failure_cannot_stall_render_barrier(monkeypatch) -> None:
    class ImmediatePool:
        def setMaxThreadCount(self, _count: int) -> None:
            pass

        def start(self, task) -> None:
            task.run()

        def waitForDone(self) -> None:
            pass

    class CompleteImmediately:
        def __init__(self, _snapshot, instance, _token, done):
            self.instance = instance
            self.done = done

        def run(self) -> None:
            self.done(self.instance.index, None)

    monkeypatch.setattr(render_pool, "QThreadPool", ImmediatePool)
    monkeypatch.setattr(render_pool, "RenderInstanceTask", CompleteImmediately)
    snapshot = SimpleNamespace(
        max_render_workers=1,
        instances=(SimpleNamespace(index=0), SimpleNamespace(index=1)),
        job_id="0" * 32,
        mode="prod",
    )

    def broken_progress(_value) -> None:
        raise RuntimeError("consumer failure")

    render_pool.render_instances(snapshot, CancellationToken(), broken_progress)
