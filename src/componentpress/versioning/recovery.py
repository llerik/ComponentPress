"""Recovery of interrupted archive publication journals."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid
import zipfile

from componentpress.diagnostics.logging import log_event
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.platforms.services import ProjectWriteLock
from componentpress.project_io.files import atomic_write, file_hash
from componentpress.project_io.yaml_codec import dump_yaml, parse_project, update_tree
from componentpress.versioning.repository import _verify_archive


def _log(event: str, **context) -> None:
    try:
        log_event(event, **context)
    except Exception:
        pass


def _log_phase(event: str, phase: str, status: str, operation_id: str, code=None) -> None:
    context = {"operation": "archive", "phase": phase, "status": status, "operation_id": operation_id}
    if code is not None:
        context["code"] = code
    _log(event, **context)


def recover_project_versions(root: Path) -> tuple[Diagnostic, ...]:
    """Finish interrupted archive publication only when its owner is verified."""
    operation_id = uuid.uuid4().hex
    _log("project.archive.recovery", operation="archive", phase="import", status="started", operation_id=operation_id)
    operation_root = root / ".componentpress" / "versioning"
    try:
        diagnostics = _recover_if_needed(root, operation_root)
    except ProjectError as exc:
        _log("project.archive.recovery", operation="archive", phase="finished", status="failed", code=exc.diagnostic.code, operation_id=operation_id)
        raise
    status = "failed" if diagnostics else "succeeded"
    context = {"code": diagnostics[0].code} if diagnostics else {}
    _log("project.archive.recovery", operation="archive", phase="finished", status=status, operation_id=operation_id, **context)
    return diagnostics


def _recover_if_needed(root: Path, operation_root: Path) -> tuple[Diagnostic, ...]:
    if not operation_root.is_dir():
        return ()
    state_root = root / ".componentpress"
    if operation_root.is_symlink() or operation_root.resolve().parent != state_root.resolve():
        return (_warning("VERSION_OWNER_UNKNOWN", "каталог архивирования находится вне разрешённого служебного пути и оставлен без изменений", operation_root),)
    with ProjectWriteLock(root):
        return _recover_project_versions_locked(root, operation_root)


def _warning(code: str, message: str, path: Path) -> Diagnostic:
    return Diagnostic(code, message, path, severity="warning")


def _recover_project_versions_locked(root: Path, operation_root: Path) -> tuple[Diagnostic, ...]:
    project_path = root / "project.yaml"
    try:
        current_project, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    except (OSError, UnicodeError, ProjectError) as exc:
        return (_warning("VERSION_RECOVERY", f"невозможно проверить журнал архивирования: {exc}", project_path),)
    diagnostics = []
    for operation in sorted(operation_root.iterdir()):
        diagnostic = _recover_operation(root, current_project.id, operation)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return tuple(diagnostics)


def _recover_operation(root: Path, project_id: str, operation: Path) -> Diagnostic | None:
    if not operation.is_dir() or operation.is_symlink():
        return None
    _log("project.archive.recovery.operation", operation="archive", phase="preparation", status="started", operation_id=operation.name)
    try:
        result = _recover_owned_operation(root, project_id, operation)
    except ProjectError as exc:
        result = exc.diagnostic
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        result = _warning("VERSION_RECOVERY", f"не удалось завершить архивирование; данные сохранены: {exc}", operation / "publish.json")
    status = "failed" if result is not None else "succeeded"
    context = {"code": result.code} if result else {}
    _log("project.archive.recovery.operation", operation="archive", phase="finished", status=status, operation_id=operation.name, **context)
    return result


def _recover_owned_operation(root: Path, project_id: str, operation: Path) -> Diagnostic | None:
    marker = operation / "operation.json"
    if not marker.is_file():
        return None
    owner = _read_json(marker)
    if not _owner_matches(owner, operation.name, project_id):
        return _warning("VERSION_OWNER_UNKNOWN", "владелец временного архива не подтверждён; данные сохранены", operation)
    journal = operation / "publish.json"
    if not journal.is_file():
        shutil.rmtree(operation)
        return None
    if journal.is_symlink():
        raise ValueError("журнал является символической ссылкой")
    record = _read_json(journal)
    return _recover_publication(root, project_id, operation, record)


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("служебный маркер является символической ссылкой")
    return json.loads(path.read_text(encoding="utf-8"))


def _owner_matches(owner: dict, operation_id: str, project_id: str) -> bool:
    return (
        owner.get("format_version") == 1
        and owner.get("operation_id") == operation_id
        and owner.get("project_id") == project_id
    )


def _recover_publication(root: Path, project_id: str, operation: Path, record: dict) -> Diagnostic | None:
    if record.get("operation_id") != operation.name or record.get("project_id") != project_id:
        raise ValueError("идентификатор операции не совпадает с журналом")
    target = _archive_target(record)
    project_path = root / "project.yaml"
    if not target.is_file() or file_hash(target) != record["archive_hash"]:
        _discard_unpublished(operation)
        return None
    _verify_recovery_archive(target, project_id, record)
    result = _apply_version_patch(project_path, record)
    if result is not None:
        return result
    _remove_recovery_journal(operation)
    shutil.rmtree(operation, ignore_errors=True)
    return None


def _verify_recovery_archive(target: Path, project_id: str, record: dict) -> None:
    operation_id = record["operation_id"]
    _log_phase("project.archive.recovery.verify", "preparation", "started", operation_id)
    with zipfile.ZipFile(target, "r") as archive:
        metadata = _verify_archive(archive)
    if metadata.get("project_id") != project_id or metadata.get("project_version") != record["project_version"]:
        raise ValueError("архив не совпадает с журналом операции")
    _log_phase("project.archive.recovery.verify", "preparation", "succeeded", operation_id)


def _remove_recovery_journal(operation: Path) -> None:
    operation_id = operation.name
    _log_phase("project.archive.recovery.journal", "publishing", "started", operation_id)
    (operation / "publish.json").unlink()
    _log_phase("project.archive.recovery.journal", "publishing", "succeeded", operation_id)


def _archive_target(record: dict) -> Path:
    target = Path(record["target"])
    if target.is_symlink() or target.suffix.casefold() != ".zip":
        raise ValueError("путь архивного файла изменён или недопустим")
    return target.resolve()


def _discard_unpublished(operation: Path) -> None:
    (operation / "publish.json").unlink(missing_ok=True)
    shutil.rmtree(operation, ignore_errors=True)


def _apply_version_patch(project_path: Path, record: dict) -> Diagnostic | None:
    project_hash = file_hash(project_path)
    if project_hash == record["project_hash"]:
        _patch_project_version(project_path, record)
        return None
    model, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    if model.id != record["project_id"] or model.version != record["next_version"]:
        return Diagnostic(
            "VERSION_RECOVERY_CONFLICT", "архив опубликован, но описание проекта изменено; версия не повышена", project_path,
        )
    return None


def _patch_project_version(project_path: Path, record: dict) -> None:
    from componentpress.versioning.repository import _recover_pending_registration

    _recover_pending_registration(project_path.parent)
    _model, tree = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    model, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    if model.id != record["project_id"] or model.version != record["project_version"]:
        raise ValueError("версия проекта отличается от журнала")
    updated = model.model_copy(update={"version": record["next_version"]})
    update_tree(tree, updated)
    operation_id = record["operation_id"]
    _log_phase("project.archive.recovery.patch", "publishing", "started", operation_id)
    atomic_write(project_path, dump_yaml(tree).encode("utf-8"), record["project_hash"])
    _log_phase("project.archive.recovery.patch", "publishing", "succeeded", operation_id)
