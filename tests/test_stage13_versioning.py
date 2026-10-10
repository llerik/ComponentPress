"""Stage-13 behaviour: verified project archives and safe restoration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import uuid
import zipfile

import pytest

from componentpress.application.version_service import (
    VersionService,
    import_project_archive,
    recover_project_versions,
)
from componentpress.bootstrap import project_controller, project_service
from componentpress.domain.diagnostics import ProjectError
from componentpress.editor.main_window import MainWindow
from componentpress.project_io.repository import FileProjectRepository


ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> FileProjectRepository:
    shutil.copytree(ROOT / "examples" / "demo-game", path)
    (path / "assets" / "fonts" / "unused-empty").mkdir(parents=True)
    (path / "assets" / "images" / "unused.png").write_bytes(b"unused-original-source")
    return FileProjectRepository()


def _source_files(root: Path) -> dict[str, bytes]:
    excluded = {"archive", "output", ".componentpress", ".git"}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0].casefold() not in excluded
    }


def test_export_then_restore_preserves_all_project_bytes_and_version(tmp_path: Path) -> None:
    root = tmp_path / "game"
    repository = _project(root)
    before = _source_files(root)
    snapshot = repository.open(root)
    archive_path = root / "archive" / "game-0.1.0.zip"

    path, next_version, patched, error = VersionService(repository).export(snapshot, archive_path)

    assert path == archive_path
    assert (next_version, patched, error) == ("0.1.1", True, None)
    assert repository.open(root).model.version == "0.1.1"
    assert _source_files(root)["project.yaml"] != before["project.yaml"]
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project_version"] == "0.1.0"
        assert manifest["project_id"] == "demo-game"
        assert "assets/images/unused.png" in manifest["files"]
        assert "assets/fonts/unused-empty" in manifest["directories"]
        assert "archive/" not in "\n".join(archive.namelist())
        expected = {f"project/{name}": value for name, value in before.items()}
        assert {name: archive.read(name) for name in expected} == expected

    restored = tmp_path / "recovered-game"
    assert import_project_archive(archive_path, restored) == restored
    assert _source_files(restored) == before
    assert repository.open(restored).model.version == "0.1.0"


def test_source_file_cannot_be_export_target_and_existing_zip_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "game"
    repository = _project(root)
    target = root / "assets" / "images" / "unused.zip"
    original = b"source stays byte-for-byte"
    target.write_bytes(original)

    with pytest.raises(ProjectError) as error:
        VersionService(repository).export(repository.open(root), target, replace=True)
    assert error.value.diagnostic.code == "ARCHIVE_DESTINATION"
    assert target.read_bytes() == original


def test_version_collision_requires_an_explicit_target_choice(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first_repo = _project(first)
    source_archive = first / "archive" / "original.zip"
    VersionService(first_repo).export(first_repo.open(first), source_archive)

    second = tmp_path / "second"
    second_repo = _project(second)
    second_archive_dir = second / "archive"
    second_archive_dir.mkdir()
    competing_archive = second_archive_dir / "existing.zip"
    shutil.copy2(source_archive, competing_archive)

    with pytest.raises(ProjectError) as error:
        VersionService(second_repo).export(second_repo.open(second), second_archive_dir / "renamed.zip")
    assert error.value.diagnostic.code == "ARCHIVE_VERSION_EXISTS"
    assert error.value.diagnostic.path == competing_archive
    assert competing_archive.is_file()


def test_invalid_path_in_archive_manifest_is_rejected_before_destination_write(tmp_path: Path) -> None:
    archive_path = tmp_path / "malicious.zip"
    manifest = {
        "format_version": 1,
        "project_id": "game",
        "project_version": "0.1.0",
        "schema_version": 5,
        "files": {"project.yaml": {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}, "../outside": {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}},
        "directories": [],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("project/project.yaml", b"")
        archive.writestr("project/../outside", b"")
    destination = tmp_path / "restored"

    with pytest.raises(ProjectError):
        import_project_archive(archive_path, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".componentpress-import-*"))


def test_recovery_finishes_only_the_patch_after_published_archive(tmp_path: Path, monkeypatch) -> None:
    import componentpress.application.version_service as module

    root = tmp_path / "game"
    repository = _project(root)
    source_snapshot = repository.open(root)
    actual_atomic_write = module.atomic_write

    def fail_project_patch(path: Path, data: bytes, expected_hash: str | None = None) -> str:
        if path == root / "project.yaml":
            raise ProjectError(__import__("componentpress.domain.diagnostics", fromlist=["Diagnostic"]).Diagnostic(
                "FILE_WRITE", "simulated project YAML write failure", path
            ))
        return actual_atomic_write(path, data, expected_hash)

    monkeypatch.setattr(module, "atomic_write", fail_project_patch)
    archive_path = root / "archive" / "game-0.1.0.zip"
    result = VersionService(repository).export(source_snapshot, archive_path)
    assert result[2] is False
    assert archive_path.is_file()
    assert repository.open(root).model.version == "0.1.0"
    monkeypatch.setattr(module, "atomic_write", actual_atomic_write)

    diagnostics = recover_project_versions(root)
    assert diagnostics == ()
    assert repository.open(root).model.version == "0.1.1"
    assert not list((root / ".componentpress" / "versioning").glob("*/publish.json"))


def test_recovery_cleans_only_marked_orphan_snapshots(tmp_path: Path) -> None:
    root = tmp_path / "game"
    repository = _project(root)
    repository.open(root)
    versioning = root / ".componentpress" / "versioning"
    owned = versioning / uuid.uuid4().hex
    owned.mkdir(parents=True)
    (owned / "operation.json").write_text(json.dumps({
        "format_version": 1,
        "operation_id": owned.name,
        "project_id": "demo-game",
        "owner_pid": -1,
        "owner_host": "old-host",
        "phase": "preparing",
    }), encoding="utf-8")
    (owned / "snapshot").mkdir()
    (owned / "snapshot" / "partial.bin").write_bytes(b"temporary")
    unknown = versioning / uuid.uuid4().hex
    unknown.mkdir()
    (unknown / "user-data.txt").write_text("preserve", encoding="utf-8")

    assert recover_project_versions(root) == ()
    assert not owned.exists()
    assert (unknown / "user-data.txt").read_text(encoding="utf-8") == "preserve"


def test_gui_archive_export_and_import_run_without_blocking_the_window(tmp_path: Path, qtbot, monkeypatch) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFileDialog, QInputDialog

    root = tmp_path / "game"
    _project(root)
    archive_path = tmp_path / "game-0.1.0.zip"
    parent = tmp_path / "restored-parent"
    parent.mkdir()
    window = MainWindow(project_controller())
    qtbot.addWidget(window)
    window.load_session(project_service().open_session(root))
    assert window.export_project_archive_action.isEnabled()
    assert window.import_project_archive_action.isEnabled()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(archive_path), "ZIP (*.zip)"))

    heartbeat: list[bool] = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    window._export_project_archive()
    qtbot.waitUntil(lambda: window._version_task is None, timeout=15000)
    assert heartbeat
    assert archive_path.is_file()
    assert window.session.snapshot.model.version == "0.1.1"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(archive_path), "ZIP (*.zip)"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: str(parent))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("restored", True))
    window._import_project_archive()
    qtbot.waitUntil(lambda: window._version_task is None, timeout=15000)
    assert window.session.snapshot.root == parent / "restored"
    assert window.session.snapshot.model.version == "0.1.0"
    assert root.joinpath("project.yaml").is_file()
