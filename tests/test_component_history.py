import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from componentpress.application.contracts import ComponentHistoryEntry, ComponentOperation
from componentpress.diagnostics import logging as diagnostics
from componentpress.domain.component import DataBinding
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.editor.project_history import ComponentHistory
from componentpress.project_io.repository import FileProjectRepository


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def entry(index: int) -> ComponentHistoryEntry:
    return ComponentHistoryEntry(
        str(index), ComponentOperation.CREATE, str(index), SimpleNamespace(),
        f"backup/{index}", f"components/{index}.yaml", str(index), index,
    )


def test_history_branch_discards_redo_without_advancing_a_pending_operation() -> None:
    history = ComponentHistory()
    first, second = entry(1), entry(2)
    assert history.append(first) == ()
    assert history.append(second) == ()
    history.accept_undo()

    third = entry(3)
    discarded = history.append(third)

    assert discarded == (second,)
    assert history.entries == [first, third]
    assert history.redo_entry() is None
    assert history.index == 2


def test_history_evicts_oldest_after_sixty_four_entries() -> None:
    history = ComponentHistory()
    discarded = tuple(item for index in range(65) for item in history.append(entry(index)))

    assert len(history.entries) == history.limit == 64
    assert history.entries[0].entry_id == "1"
    assert history.index == 64
    assert [item.entry_id for item in discarded] == ["0"]


def test_history_cursor_changes_only_when_caller_accepts_operation() -> None:
    history = ComponentHistory()
    history.append(entry(1))

    assert history.undo_entry().entry_id == "1"
    assert history.index == 1
    history.accept_undo()
    assert history.index == 0
    assert history.redo_entry().entry_id == "1"
    history.accept_redo()
    assert history.index == 1


def test_pruned_backup_waits_for_membership_recovery(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    snapshot = repository.open(root)
    snapshot = repository.add_component(snapshot, "second", "Second")
    backup_path = ".componentpress/transactions/component-history/deleted.yaml"
    unlink = Path.unlink

    def leave_membership_journal(path, *args, **kwargs):
        if path.name == "component-membership.json":
            raise PermissionError("injected journal cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", leave_membership_journal)
    removed = repository.remove_component(snapshot, "forest-card", backup_path=backup_path)
    backup = root / Path(backup_path)
    journal = root / ".componentpress/transactions/component-membership.json"
    assert journal.is_file() and backup.is_file()
    assert not repository.discard_component_history_backup(root, backup_path)
    assert backup.is_file()

    monkeypatch.setattr(Path, "unlink", unlink)
    repository.open(root)
    assert not journal.exists()
    assert repository.discard_component_history_backup(root, backup_path)
    assert not backup.exists()
    assert "forest-card" not in removed.snapshot.documents


def test_add_journal_cleanup_failure_keeps_session_consistent(
    tmp_path: Path, monkeypatch
) -> None:
    from componentpress.bootstrap import project_service

    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    service = project_service()
    session = service.open_session(root)
    original_snapshot = session.snapshot
    original_documents = dict(session.documents)
    preserved = session.documents["forest-card"]
    preserved.set_draft("invalid draft", ())
    preserved.update(name="Unsaved name")
    original_project = (root / "project.cpress").read_bytes()
    with pytest.raises(ProjectError) as caught:
        service.add_to_session(session, "Unbound data", "unbound-data", data=DataBinding(sheet="Cards"))
    assert caught.value.diagnostic.code == "DATA_SOURCE_UNKNOWN"
    assert not (root / "components/unbound-data.yaml").exists()
    assert (root / "project.cpress").read_bytes() == original_project
    unlink = Path.unlink

    def fail_after_registration(path, *args, **kwargs):
        if path.name == "add-component.json":
            raise PermissionError("injected post-commit cleanup failure")
        return unlink(path, *args, **kwargs)

    archive = tmp_path / "blocked.zip"
    monkeypatch.setattr(Path, "unlink", fail_after_registration)
    assert service.add_to_session(session, "History probe", "history-probe") == "history-probe"
    assert session.snapshot is not original_snapshot
    assert "history-probe" in session.snapshot.documents
    assert all(session.documents[key] is value for key, value in original_documents.items())
    assert preserved.dirty and preserved.draft_text == "invalid draft"
    assert (root / ".componentpress/transactions/add-component.json").is_file()

    with pytest.raises(ProjectError) as caught:
        service.add_to_session(session, "Another component", "another-component")
    assert caught.value.diagnostic.code == "TRANSACTION_CONFLICT"
    assert not (root / "components/another-component.yaml").exists()
    original_project = (root / "project.cpress").read_bytes()
    with pytest.raises(ProjectError):
        service.set_project_details(session, name="Changed", version="0.2.0")
    with pytest.raises(ProjectError):
        service.versions.export(session.snapshot, archive)
    assert (root / "project.cpress").read_bytes() == original_project
    assert not archive.exists()

    monkeypatch.setattr(Path, "unlink", unlink)
    assert "history-probe" in service.projects.open(root).documents
    service.set_project_details(session, name="Changed", version="0.2.0")
    assert service.add_to_session(session, "Another component", "another-component") == "another-component"
    assert "another-component" in session.documents


def test_add_collision_does_not_delete_existing_component_even_when_hash_matches(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    snapshot = repository.open(root)
    target = root / "components/claimed.yaml"
    owned = b""
    unlink = Path.unlink

    def race_with_component_creation(path, data):
        nonlocal owned
        if path == target:
            owned = data
            target.write_bytes(data)
            raise ProjectError(Diagnostic("FILE_EXISTS", "file exists", target))
        from componentpress.project_io.files import atomic_create as create
        return create(path, data)

    def fail_add_marker_cleanup(path, *args, **kwargs):
        if path.name == "add-component.json":
            raise PermissionError("injected cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr("componentpress.project_io.transactions.atomic_create", race_with_component_creation)
    monkeypatch.setattr(Path, "unlink", fail_add_marker_cleanup)
    with pytest.raises(ProjectError):
        repository.add_component(snapshot, "claimed", "Claimed")
    assert target.read_bytes() == owned
    journal = root / ".componentpress/transactions/add-component.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["ownership_conflict"]
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(ProjectError) as caught:
        repository.open(root)
    assert caught.value.diagnostic.code == "TRANSACTION_CONFLICT"
    assert target.read_bytes() == owned


def test_add_rejects_a_changed_existing_document_before_commit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    snapshot = repository.open(root)
    original_project = (root / "project.cpress").read_bytes()
    document = root / "components/forest-card.yaml"
    document.write_text(document.read_text(encoding="utf-8") + "# external edit\n", encoding="utf-8")

    with pytest.raises(ProjectError) as caught:
        repository.add_component(snapshot, "new-card", "New card")

    assert caught.value.diagnostic.code == "FILE_CHANGED_EXTERNALLY"
    assert (root / "project.cpress").read_bytes() == original_project
    assert not (root / "components/new-card.yaml").exists()


def test_component_history_operations_keep_safe_log_context() -> None:
    diagnostics.shutdown()
    diagnostics._BUFFER.clear()
    for operation in ("add_component", "remove_component", "restore_component", "discard_component_history_backup"):
        diagnostics.log_event("project.operation", operation=operation, status="started")

    records = [json.loads(line) for line in diagnostics._BUFFER]
    operations = [record["operation"] for record in records if record.get("event") == "project.operation"]
    assert operations == [
        "add_component", "remove_component", "restore_component", "discard_component_history_backup",
    ]
    diagnostics._BUFFER.clear()
