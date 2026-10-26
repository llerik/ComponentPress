"""Concurrent project membership must never produce a mixed open snapshot."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil

import pytest

from componentpress.domain.diagnostics import ProjectError
from componentpress.bootstrap import project_service
from componentpress.editor.controllers import ProjectController
from componentpress.platforms.services import ProjectWriteLock
from componentpress.project_io import repository as repository_module
from componentpress.project_io.repository import FileProjectRepository


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def _open_paused(root, method, phase, watched, reached, release, result) -> None:
    from PySide6.QtCore import QCoreApplication
    from componentpress.project_io import repository

    read_text = repository._read_text

    if phase == "preflight":
        from componentpress.project_io import schema_checks

        check_component = schema_checks._check_component

        def pause_preflight(path, variables, expected_id=None):
            if path.resolve() == watched and not reached.is_set():
                reached.set()
                if not release.wait(15):
                    raise TimeoutError("parent did not release the preflight barrier")
            return check_component(path, variables, expected_id)

        schema_checks._check_component = pause_preflight

    def pause(path: Path):
        if path.resolve() == watched and not reached.is_set():
            reached.set()
            if not release.wait(15):
                raise TimeoutError("parent did not release the open barrier")
        return read_text(path)

    repository._read_text = pause
    try:
        snapshot = getattr(FileProjectRepository(), method)(root)
        documents = tuple(sorted((key, value.text) for key, value in snapshot.documents.items()))
        result.put(("snapshot", snapshot.text, documents, QCoreApplication.instance() is None))
    except ProjectError as exc:
        result.put(("error", exc.diagnostic.code, QCoreApplication.instance() is None))
    except Exception as exc:
        result.put(("error", f"UNEXPECTED_{type(exc).__name__}", QCoreApplication.instance() is None))


def _signature(snapshot):
    return snapshot.text, tuple(sorted((key, value.text) for key, value in snapshot.documents.items()))


def _mutate(root: Path, change: str, removed=None):
    repository = FileProjectRepository()
    snapshot = repository.open(root)
    backup = ".componentpress/transactions/component-history/rf07-backup.yaml"
    if change == "add":
        repository.add_component(snapshot, "fresh-card", "Fresh card")
        return
    if change == "remove":
        return repository.remove_component(snapshot, "forest-card", backup_path=backup)
    if change == "restore":
        return repository.restore_component(
            snapshot, "second", reference_path=removed.reference_path,
            backup_path=removed.backup_path, component_hash=removed.component_hash,
            position=removed.position,
        )
    removed_forest = repository.remove_component(snapshot, "forest-card", backup_path=backup)
    return repository.restore_component(
        removed_forest.snapshot, "forest-card", reference_path=removed_forest.reference_path,
        backup_path=removed_forest.backup_path, component_hash=removed_forest.component_hash,
        position=removed_forest.position,
    )


@pytest.mark.parametrize("method,phase", (
    ("open", "preflight"), ("open", "documents"), ("open_for_editor", "documents"),
))
@pytest.mark.parametrize("change", ("add", "remove", "restore", "restore_aba"))
def test_membership_change_during_open_returns_a_whole_snapshot_or_diagnostic(
    tmp_path: Path, method: str, phase: str, change: str,
) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    removed = None
    if change == "restore":
        initial = repository.add_component(repository.open(root), "second", "Second")
        initial = repository.add_component(initial, "third", "Third")
        removed = repository.remove_component(
            initial, "second", backup_path=".componentpress/transactions/component-history/restore.yaml",
        )
    elif change == "restore_aba":
        repository.add_component(repository.open(root), "second", "Second")
    elif change == "remove":
        repository.add_component(repository.open(root), "second", "Second")
    before = _signature(repository.open(root))
    watched = (root / "components/forest-card.yaml").resolve()
    context = multiprocessing.get_context("spawn")
    reached, release = context.Event(), context.Event()
    result = context.Queue()
    reader = context.Process(
        target=_open_paused, args=(root, method, phase, watched, reached, release, result),
    )
    reader.start()
    try:
        assert reached.wait(20), "reader did not reach the component read barrier"
        _mutate(root, change, removed)
        final = _signature(repository.open(root))
    finally:
        release.set()
    reader.join(20)
    try:
        assert not reader.is_alive(), "reader exceeded its bounded open time"
        outcome = result.get(timeout=2)
        assert outcome[0] in {"snapshot", "error"}
        assert outcome[-1] is True
        if outcome[0] == "snapshot":
            assert (outcome[1], outcome[2]) in {before, final}
        else:
            assert outcome[1] in {"FILE_READ", "FILE_CHANGED_EXTERNALLY", "PROJECT_LOCKED"}
    finally:
        if reader.is_alive():
            reader.terminate()
            reader.join(5)
        result.close()
        result.join_thread()


def _remove_and_crash_before_manifest(root: Path, backup: str) -> None:
    from componentpress.project_io import transactions

    atomic_write = transactions.atomic_write

    def crash_on_project_manifest(path: Path, data: bytes, expected_hash=None):
        if path == root / "project.cpress":
            os._exit(37)
        return atomic_write(path, data, expected_hash)

    transactions.atomic_write = crash_on_project_manifest
    repository = FileProjectRepository()
    repository.remove_component(repository.open(root), "forest-card", backup_path=backup)


@pytest.mark.parametrize("method", ("open", "open_for_editor"))
def test_open_recovers_verified_membership_backup_after_writer_crash(tmp_path: Path, method: str) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    FileProjectRepository().add_component(FileProjectRepository().open(root), "second", "Second")
    manifest = (root / "project.cpress").read_bytes()
    component = (root / "components/forest-card.yaml").read_bytes()
    backup_path = ".componentpress/transactions/component-history/crash.yaml"
    context = multiprocessing.get_context("spawn")
    writer = context.Process(target=_remove_and_crash_before_manifest, args=(root, backup_path))
    writer.start()
    writer.join(20)
    assert not writer.is_alive()
    assert writer.exitcode == 37
    backup = root / backup_path
    journal = root / ".componentpress/transactions/component-membership.json"
    target = root / "components/forest-card.yaml"
    assert not target.exists() and backup.read_bytes() == component and journal.is_file()
    assert (root / "project.cpress").read_bytes() == manifest

    with ProjectWriteLock(root):
        with pytest.raises(ProjectError) as caught:
            getattr(FileProjectRepository(), method)(root)
        assert caught.value.diagnostic.code == "PROJECT_LOCKED"
        assert not target.exists() and backup.read_bytes() == component and journal.is_file()

    record = json.loads(journal.read_text(encoding="utf-8"))
    invalid = component.replace(b"schema_version: 9", b"schema_version: 7", 1)
    assert invalid != component
    backup.write_bytes(invalid)
    record["component_hash"] = hashlib.sha256(invalid).hexdigest()
    journal.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        getattr(FileProjectRepository(), method)(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert not target.exists() and backup.read_bytes() == invalid and journal.is_file()
    backup.write_bytes(component)
    record["component_hash"] = hashlib.sha256(component).hexdigest()
    journal.write_text(json.dumps(record), encoding="utf-8")

    snapshot = getattr(FileProjectRepository(), method)(root)
    assert target.read_bytes() == component
    assert (root / "project.cpress").read_bytes() == manifest
    assert not backup.exists() and not journal.exists()
    assert "forest-card" in snapshot.documents
    assert snapshot.documents["forest-card"].disk_hash == hashlib.sha256(component).hexdigest()


def test_editor_session_survives_open_interrupted_by_component_removal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    repository.add_component(repository.open(root), "second", "Second")
    controller = ProjectController(project_service())
    session = controller.open(root)
    document = session.documents["forest-card"]
    document.set_draft("unsaved text", ())
    read_text = repository_module._read_text
    triggered = False

    def remove_before_document_read(path: Path):
        nonlocal triggered
        if path == document.source.path and not triggered:
            triggered = True
            current = repository.open(root)
            repository.remove_component(
                current, "forest-card",
                backup_path=".componentpress/transactions/component-history/racing.yaml",
            )
        return read_text(path)

    monkeypatch.setattr("componentpress.project_io.repository._read_text", remove_before_document_read)
    with pytest.raises(ProjectError):
        controller.open(root)

    assert triggered
    assert controller.session is session
    assert controller.session.documents["forest-card"] is document
    assert document.dirty and document.current_text == "unsaved text"


def test_read_only_open_does_not_create_project_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    snapshot = FileProjectRepository().open(root)
    assert snapshot.documents
    assert not (root / ".componentpress").exists()
