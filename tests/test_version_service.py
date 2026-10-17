"""Application wiring and failure behavior for project archive operations."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from componentpress.application.version_service import VersionService
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.versioning import repository as archive_module
from componentpress.versioning.repository import ZipArchiveRepository


def test_application_service_uses_fake_ports_without_adapters():
    calls = []

    class Archives:
        def export(self, snapshot, target, **kwargs):
            calls.append(("export", snapshot, target, kwargs))
            return target, "0.1.1", True, None

        def restore_path(self, archive, destination, **kwargs):
            calls.append(("restore", archive, destination, kwargs))
            return destination

    class Projects:
        def open(self, root):
            calls.append(("open", root))
            return "snapshot"

    service = VersionService(Archives(), Projects())
    assert service.export("snapshot", Path("game.zip")) == (Path("game.zip"), "0.1.1", True, None)
    assert service.restore("game.zip", Path("restored")) == "snapshot"
    assert [call[0] for call in calls] == ["export", "restore", "open"]


def test_zip_adapter_locks_canonical_project_and_logs_safe_start_and_finish(monkeypatch, tmp_path):
    events = []
    held = []
    snapshot = SimpleNamespace(root=tmp_path / ".", model=SimpleNamespace(id="project"))

    class Lock:
        def __init__(self, root):
            held.append(("enter", root, events[-1][1]["status"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            held.append(("exit",))

    def export(*_args, **_kwargs):
        assert held[-1][0] == "enter"
        return Path("archive.zip"), "0.1.1", True, None

    monkeypatch.setattr(archive_module, "log_event", lambda event, **context: events.append((event, context)))
    monkeypatch.setattr(archive_module, "export_project_version", export)
    adapter = ZipArchiveRepository(object(), Lock, diagnostic_formatter=str)
    result = adapter.export(snapshot, Path("archive.zip"))

    assert result[2] is True
    assert held[0] == ("enter", tmp_path.resolve(), "started")
    assert held[-1] == ("exit",)
    assert [event[1]["status"] for event in events] == ["started", "succeeded"]
    operation_id = events[0][1]["operation_id"]
    assert len(operation_id) == 32 and set(operation_id) <= set("0123456789abcdef")
    assert all("path" not in event[1] and "target" not in event[1] for event in events)


def test_lock_failure_is_logged_and_does_not_start_export(monkeypatch, tmp_path):
    events = []
    snapshot = SimpleNamespace(root=tmp_path, model=SimpleNamespace(id="project"))

    class Locked:
        def __init__(self, _root):
            pass

        def __enter__(self):
            raise ProjectError(Diagnostic("PROJECT_LOCKED", "locked"))

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(archive_module, "log_event", lambda event, **context: events.append((event, context)))
    monkeypatch.setattr(archive_module, "export_project_version", lambda *_args, **_kwargs: pytest.fail("export ran while locked"))
    adapter = ZipArchiveRepository(object(), Locked, diagnostic_formatter=str)
    with pytest.raises(ProjectError, match="PROJECT_LOCKED"):
        adapter.export(snapshot, tmp_path / "archive.zip")
    assert [event[1]["status"] for event in events] == ["started", "failed"]
    assert events[-1][1]["code"] == "PROJECT_LOCKED"


def test_logging_sink_failure_does_not_change_archive_result(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(root=tmp_path, model=SimpleNamespace(id="project"))

    class Lock:
        def __init__(self, _root): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass

    monkeypatch.setattr(archive_module, "log_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log sink")))
    monkeypatch.setattr(archive_module, "export_project_version", lambda *_args, **_kwargs: (Path("archive.zip"), "0.1.1", True, None))
    adapter = ZipArchiveRepository(object(), Lock, diagnostic_formatter=str)
    assert adapter.export(snapshot, Path("archive.zip"))[2] is True


def test_published_archive_recovers_when_journal_removal_fails(monkeypatch, tmp_path):
    from test_stage13_versioning import _project, _versions

    root = tmp_path / "game"
    projects = _project(root)
    archive_path = root / "archive" / "game-0.1.0.zip"
    real_unlink = Path.unlink

    def fail_journal_unlink(path, *args, **kwargs):
        if path.name == "publish.json" and path.parent.parent == root / ".componentpress" / "versioning":
            raise PermissionError("journal cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    result = _versions(projects).export(projects.open(root), archive_path)
    assert result[2] is False
    assert archive_path.is_file()
    journal = next((root / ".componentpress" / "versioning").glob("*/publish.json"))
    assert journal.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert projects.open(root).model.version == "0.1.1"
    assert projects.open(root).model.version == "0.1.1"
    assert not list((root / ".componentpress" / "versioning").glob("*/publish.json"))
