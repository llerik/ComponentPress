"""Recoverable two-file component registration; project.yaml is committed last."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .files import atomic_create, atomic_move, atomic_write, file_hash
from .paths import resolve_project_path


def _journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "add-component.json"


def _membership_journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "component-membership.json"


def recover(root: Path) -> None:
    if _membership_journal(root).exists():
        recover_component_membership(root)
    journal = _journal(root)
    if not journal.exists():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        project = root / "project.yaml"
        component = resolve_project_path(root, record["component"])
        current_project = file_hash(project) if project.exists() else None
        current_component = file_hash(component) if component.exists() else None
        if current_project == record["new_project_hash"] and current_component == record["component_hash"]:
            journal.unlink()
            return
        if current_project == record["old_project_hash"] and current_component in (None, record["component_hash"]):
            if component.exists():
                component.unlink()
            journal.unlink()
            return
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", f"не удаётся восстановить операцию: {exc}", journal)) from exc
    raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", "файлы изменены во время незавершённой операции; требуется ручное восстановление", journal))


def recover_component_membership(root: Path) -> None:
    journal = _membership_journal(root)
    if not journal.exists():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        target = resolve_project_path(root, record["component"])
        backup = resolve_project_path(root, record["backup"])
        project = root / "project.yaml"
        project_hash = file_hash(project) if project.exists() else None
        target_hash = file_hash(target) if target.exists() else None
        backup_hash = file_hash(backup) if backup.exists() else None
        old_hash, new_hash = record["old_project_hash"], record["new_project_hash"]
        content_hash = record["component_hash"]
        operation = record["operation"]
        if operation == "remove":
            if project_hash == new_hash and target_hash is None and backup_hash == content_hash:
                try:
                    journal.unlink()
                except OSError:
                    pass
                return
            if project_hash == old_hash and target_hash == content_hash and backup_hash is None:
                try:
                    journal.unlink()
                except OSError:
                    pass
                return
            if project_hash == old_hash and target_hash is None and backup_hash == content_hash:
                atomic_move(backup, target)
                journal.unlink()
                return
        elif operation == "restore":
            if project_hash == new_hash and target_hash == content_hash and backup_hash is None:
                try:
                    journal.unlink()
                except OSError:
                    pass
                return
            if project_hash == old_hash and target_hash is None and backup_hash == content_hash:
                try:
                    journal.unlink()
                except OSError:
                    pass
                return
            if project_hash == old_hash and target_hash == content_hash and backup_hash is None:
                atomic_move(target, backup)
                journal.unlink()
                return
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", f"не удаётся восстановить операцию с компонентом: {exc}", journal)) from exc
    raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", "операция с компонентом требует ручного восстановления", journal))


def change_component_membership(root: Path, *, operation: str, relative_path: str, backup_path: str,
                                project_bytes: bytes, old_project_hash: str, component_hash: str) -> None:
    """Commit registry removal/restoration while retaining original YAML for undo."""
    journal = _membership_journal(root)
    if journal.exists():
        recover_component_membership(root)
    target = resolve_project_path(root, relative_path)
    backup = resolve_project_path(root, backup_path)
    record = {
        "operation": operation, "component": relative_path, "backup": backup_path,
        "component_hash": component_hash, "old_project_hash": old_project_hash,
        "new_project_hash": hashlib.sha256(project_bytes).hexdigest(),
    }
    journal.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    try:
        if operation == "remove":
            if not target.is_file() or file_hash(target) != component_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "компонент изменён перед удалением", target))
            atomic_move(target, backup)
        elif operation == "restore":
            if not backup.is_file() or file_hash(backup) != component_hash:
                raise ProjectError(Diagnostic("COMPONENT_HISTORY", "снимок компонента изменён перед восстановлением", backup))
            atomic_move(backup, target)
        else:
            raise ValueError(f"unknown membership operation: {operation}")
        atomic_write(root / "project.yaml", project_bytes, old_project_hash)
    except Exception:
        recover_component_membership(root)
        raise
    # A cleanup failure occurs after commit. Keep the journal for open-time recovery.
    try:
        journal.unlink()
    except OSError:
        pass


def register_component(root: Path, component_path: str, component_bytes: bytes, project_bytes: bytes, old_project_hash: str) -> None:
    journal = _journal(root)
    if journal.exists():
        recover(root)
    target = resolve_project_path(root, component_path)
    if target.exists():
        raise ProjectError(Diagnostic("FILE_EXISTS", "компонент уже существует", target))
    record = {
        "component": component_path,
        "component_hash": hashlib.sha256(component_bytes).hexdigest(),
        "old_project_hash": old_project_hash,
        "new_project_hash": hashlib.sha256(project_bytes).hexdigest(),
    }
    journal.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    try:
        atomic_create(target, component_bytes)
        atomic_write(root / "project.yaml", project_bytes, old_project_hash)
    except Exception:
        recover(root)
        raise
    journal.unlink()
