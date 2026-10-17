import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from componentpress.application.contracts import ComponentHistoryEntry, ComponentOperation
from componentpress.diagnostics import logging as diagnostics
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


def test_add_journal_cleanup_failure_is_recoverable_but_leaves_session_stale(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    shutil.copytree(DEMO, root)
    repository = FileProjectRepository()
    snapshot = repository.open(root)
    unlink = Path.unlink

    def fail_after_registration(path, *args, **kwargs):
        if path.name == "add-component.json":
            raise PermissionError("injected post-commit cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_after_registration)
    with pytest.raises(PermissionError):
        repository.add_component(snapshot, "history-probe", "History probe")
    assert "history-probe" not in snapshot.documents

    monkeypatch.setattr(Path, "unlink", unlink)
    assert "history-probe" in repository.open(root).documents


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
