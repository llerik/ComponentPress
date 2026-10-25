"""Recoverable two-file component registration; project.cpress is committed last."""

import hashlib
import json
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .files import atomic_create, atomic_move, atomic_write, file_hash
from .paths import PROJECT_MANIFEST, resolve_project_path
from componentpress.diagnostics.logging import log_event


def _journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "add-component.json"


def _membership_journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "component-membership.json"


def recover(root: Path) -> None:
    from .resource_transactions import recover_resource_imports

    recover_resource_imports(root)
    if _membership_journal(root).exists():
        recover_component_membership(root)
    recover_add_component(root)


def recover_add_component(root: Path) -> None:
    """Resolve only component registration before project-manifest writers."""
    journal = _journal(root)
    if not journal.exists():
        return
    _log_registration("started")
    try:
        _recover_add_component(root, journal)
    except ProjectError as exc:
        _log_registration("failed", exc.diagnostic.code)
        raise
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        _log_registration("failed", "TRANSACTION_CONFLICT")
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", f"не удаётся восстановить операцию: {exc}", journal)) from exc
    _log_registration("succeeded")


def _recover_add_component(root: Path, journal: Path) -> None:
    record = json.loads(journal.read_text(encoding="utf-8"))
    if record.get("ownership_conflict"):
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", "файл компонента уже существовал; автоматическое восстановление остановлено", journal))
    project = root / PROJECT_MANIFEST
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
    raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", "файлы изменены во время незавершённой операции; требуется ручное восстановление", journal))


def _rollback_registration(root: Path) -> None:
    try:
        recover_add_component(root)
    except ProjectError as exc:
        _log_registration("failed", exc.diagnostic.code)


def recover_component_membership(root: Path) -> None:
    journal = _membership_journal(root)
    if not journal.exists():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        target, backup, hashes = _membership_state(root, record)
        recovery = {"remove": _recover_removed_membership, "restore": _recover_restored_membership}
        handler = recovery.get(record["operation"])
        if handler is None:
            raise ValueError("unknown membership operation")
        recovered = handler(journal, record, target, backup, hashes)
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", f"не удаётся восстановить операцию с компонентом: {exc}", journal)) from exc
    if not recovered:
        raise ProjectError(Diagnostic("TRANSACTION_CONFLICT", "операция с компонентом требует ручного восстановления", journal))


def _membership_state(root: Path, record: dict) -> tuple[Path, Path, tuple[str | None, ...]]:
    target = resolve_project_path(root, record["component"])
    backup = resolve_project_path(root, record["backup"])
    project = root / PROJECT_MANIFEST
    hashes = tuple(file_hash(path) if path.exists() else None for path in (project, target, backup))
    return target, backup, hashes


def _finish_membership_recovery(journal: Path) -> None:
    try:
        journal.unlink()
    except OSError:
        pass


def _recover_removed_membership(journal: Path, record: dict, target: Path, backup: Path,
                                hashes: tuple[str | None, ...]) -> bool:
    project_hash, target_hash, backup_hash = hashes
    content_hash = record["component_hash"]
    if project_hash == record["new_project_hash"] and target_hash is None and backup_hash == content_hash:
        _finish_membership_recovery(journal)
    elif project_hash == record["old_project_hash"] and target_hash == content_hash and backup_hash is None:
        _finish_membership_recovery(journal)
    elif project_hash == record["old_project_hash"] and target_hash is None and backup_hash == content_hash:
        atomic_move(backup, target)
        _finish_membership_recovery(journal)
    else:
        return False
    return True


def _recover_restored_membership(journal: Path, record: dict, target: Path, backup: Path,
                                 hashes: tuple[str | None, ...]) -> bool:
    project_hash, target_hash, backup_hash = hashes
    content_hash = record["component_hash"]
    if project_hash == record["new_project_hash"] and target_hash == content_hash and backup_hash is None:
        _finish_membership_recovery(journal)
    elif project_hash == record["old_project_hash"] and target_hash is None and backup_hash == content_hash:
        _finish_membership_recovery(journal)
    elif project_hash == record["old_project_hash"] and target_hash == content_hash and backup_hash is None:
        atomic_move(target, backup)
        _finish_membership_recovery(journal)
    else:
        return False
    return True


def change_component_membership(root: Path, *, operation: str, relative_path: str, backup_path: str,
                                project_bytes: bytes, old_project_hash: str, component_hash: str) -> None:
    """Commit registry removal/restoration while retaining original YAML for undo."""
    journal = _membership_journal(root)
    if journal.exists():
        recover_component_membership(root)
    target = resolve_project_path(root, relative_path)
    backup = resolve_project_path(root, backup_path)
    record = _membership_record(operation, relative_path, backup_path, project_bytes, old_project_hash, component_hash)
    journal.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    try:
        _move_membership_component(operation, target, backup, component_hash)
        atomic_write(root / PROJECT_MANIFEST, project_bytes, old_project_hash)
    except Exception:
        recover_component_membership(root)
        raise
    # A cleanup failure occurs after commit. Keep the journal for open-time recovery.
    _finish_membership_recovery(journal)


def _membership_record(operation: str, relative_path: str, backup_path: str,
                       project_bytes: bytes, old_project_hash: str, component_hash: str) -> dict:
    return {
        "operation": operation, "component": relative_path, "backup": backup_path,
        "component_hash": component_hash, "old_project_hash": old_project_hash,
        "new_project_hash": hashlib.sha256(project_bytes).hexdigest(),
    }


def _move_membership_component(operation: str, target: Path, backup: Path, component_hash: str) -> None:
    source, destination, error = _membership_paths(operation, target, backup)
    if not source.is_file() or file_hash(source) != component_hash:
        raise ProjectError(Diagnostic(error[0], error[1], source))
    atomic_move(source, destination)


def _membership_paths(operation: str, target: Path, backup: Path) -> tuple[Path, Path, tuple[str, str]]:
    if operation == "remove":
        return target, backup, ("FILE_CHANGED_EXTERNALLY", "компонент изменён перед удалением")
    if operation == "restore":
        return backup, target, ("COMPONENT_HISTORY", "снимок компонента изменён перед восстановлением")
    raise ValueError(f"unknown membership operation: {operation}")


def register_component(root: Path, component_path: str, component_bytes: bytes, project_bytes: bytes, old_project_hash: str) -> None:
    journal = _journal(root)
    if journal.exists():
        recover_add_component(root)
    target = resolve_project_path(root, component_path)
    if target.exists():
        raise ProjectError(Diagnostic("FILE_EXISTS", "компонент уже существует", target))
    record = {
        "component": component_path,
        "component_hash": hashlib.sha256(component_bytes).hexdigest(),
        "old_project_hash": old_project_hash,
        "new_project_hash": hashlib.sha256(project_bytes).hexdigest(),
    }
    _log_registration("started")
    _write_registration_journal(journal, record)
    _register_files(root, journal, target, record, component_bytes, project_bytes, old_project_hash)


def _write_registration_journal(journal: Path, record: dict) -> None:
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    except ProjectError as exc:
        _log_registration("failed", exc.diagnostic.code)
        raise


def _register_files(root: Path, journal: Path, target: Path, record: dict, component_bytes: bytes, project_bytes: bytes, old_project_hash: str) -> None:
    created = False
    try:
        atomic_create(target, component_bytes)
        created = True
        atomic_write(root / PROJECT_MANIFEST, project_bytes, old_project_hash)
    except ProjectError as exc:
        _handle_registration_failure(root, journal, record, created, exc)
        raise
    except Exception:
        _rollback_registration(root)
        _log_registration("failed", "FILE_WRITE")
        raise
    _finish_registration(journal)


def _handle_registration_failure(root: Path, journal: Path, record: dict, created: bool, error: ProjectError) -> None:
    if not created and error.diagnostic.code == "FILE_EXISTS":
        try:
            journal.unlink()
        except OSError:
            _mark_registration_ownership_unknown(journal, record)
    else:
        _rollback_registration(root)
    _log_registration("failed", error.diagnostic.code)


def _mark_registration_ownership_unknown(journal: Path, record: dict) -> None:
    record["ownership_conflict"] = True
    try:
        atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    except ProjectError:
        _log_registration("failed", "TRANSACTION_CONFLICT")


def _finish_registration(journal: Path) -> None:
    try:
        journal.unlink()
    except OSError:
        _log_registration("cleanup_pending")
    else:
        _log_registration("succeeded")


def _log_registration(status: str, code: str | None = None) -> None:
    try:
        log_event("project.component.registration", operation="add_component", phase="publishing", status=status, code=code)
    except Exception:
        pass
