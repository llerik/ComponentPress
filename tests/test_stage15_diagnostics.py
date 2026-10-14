"""Privacy, rotation and recovery behavior for technical diagnostics."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import zipfile

from PySide6.QtCore import QLockFile
from types import SimpleNamespace

from componentpress.diagnostics import logging as diagnostics
from componentpress.build import engine as build_engine_module
from componentpress.execution.cancellation import CancellationToken


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
    assert payload["runtime"]["version"] == "0.16"
    assert payload["incident"]["incident_id"] == incident_id
    assert b"SECRET_FROM_EXCEPTION" not in contents
    assert b"SECRET_PROJECT_PATH" not in contents
    assert b"SECRET_PROJECT_NAME" not in contents
    assert 1 <= len(list(directory.glob("componentpress-*.log*"))) <= 4
    assert all(path.stat().st_size <= 700 for path in directory.glob("componentpress-*.log*"))
    diagnostics.shutdown()


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

    monkeypatch.setattr(build_engine_module, "QThreadPool", ImmediatePool)
    monkeypatch.setattr(build_engine_module, "RenderInstanceTask", CompleteImmediately)
    snapshot = SimpleNamespace(
        max_render_workers=1,
        instances=(SimpleNamespace(index=0), SimpleNamespace(index=1)),
        job_id="0" * 32,
        mode="prod",
    )

    def broken_progress(_value) -> None:
        raise RuntimeError("consumer failure")

    build_engine_module.BuildEngine._render(snapshot, CancellationToken(), broken_progress)
