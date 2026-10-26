"""Shared crash-safe runner for registered project schema migrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.platforms.services import ProjectWriteLock
from .files import atomic_create, atomic_write
from .paths import PROJECT_MANIFEST, project_manifest_path, resolve_project_path
from .yaml_codec import _load
from .schema_migrations import MIGRATIONS_BY_ID, MIGRATIONS_BY_SOURCE


_ROOT = Path(".componentpress/migrations")
_JOURNAL = _ROOT / "active.json"


def migrate_schema_on_open(root: Path) -> None:
    root = root.resolve()
    if not (root / PROJECT_MANIFEST).is_file():
        return
    version = _project_version(root)
    path = _migration_path(version, root)
    if not path:
        return
    with ProjectWriteLock(root):
        version = _project_version(root)
        path = _migration_path(version, root)
        if (root / _JOURNAL).is_file():
            active = _read_journal(root / _JOURNAL)
            if not path or active.get("migration") != path[0].id:
                raise _error("SCHEMA_MIGRATION_JOURNAL", "журнал не совпадает с достижимым путём миграции", root / _JOURNAL)
            _resume(root)
            path = _migration_path(_project_version(root), root)
        for migration in path:
            _start(root, migration)


def _migration_path(version: int, root: Path):
    plan = []
    while version < SCHEMA_VERSION:
        migration = MIGRATIONS_BY_SOURCE.get(version)
        if migration is None or migration.target_version <= version or migration.target_version > SCHEMA_VERSION:
            raise _error("SCHEMA_UNSUPPORTED", f"нет полного пути миграции от schema {version}", root / PROJECT_MANIFEST)
        plan.append(migration)
        version = migration.target_version
    return tuple(plan)


def _project_version(root: Path) -> int:
    path = project_manifest_path(root)
    try:
        tree = _load(path.read_text(encoding="utf-8"), path)
    except OSError as exc:
        raise _error("FILE_READ", str(exc), path) from exc
    value = tree.get("schema_version") if isinstance(tree, dict) else None
    if type(value) is not int or value > SCHEMA_VERSION:
        raise _error("SCHEMA_UNSUPPORTED", f"версия схемы {value!r} не поддерживается", path)
    return value


def _start(root: Path, migration) -> None:
    targets, project_id = migration.prepare(root)
    operation = uuid4().hex
    entries = _stage_targets(root, operation, targets)
    record = {"format": 1, "operation": operation, "migration": migration.id,
              "target_version": migration.target_version, "project_id": project_id, "files": entries}
    path = root / _JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_create(path, json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    _resume(root)


def _stage_targets(root: Path, operation: str, targets: list[tuple[str, bytes]]) -> list[dict]:
    stage_root = root / _ROOT / operation
    stage_root.mkdir(parents=True, exist_ok=False)
    ordered = [item for item in targets if item[0] != PROJECT_MANIFEST]
    ordered.extend(item for item in targets if item[0] == PROJECT_MANIFEST)
    entries = []
    for index, (relative, data) in enumerate(ordered):
        target = resolve_project_path(root, relative)
        staged = stage_root / f"{index:06}.yaml"
        atomic_create(staged, data)
        entries.append({"target": relative, "staged": staged.relative_to(root).as_posix(),
                        "old": _hash(target.read_bytes()), "new": _hash(data)})
    return entries


def _resume(root: Path) -> None:
    journal = root / _JOURNAL
    record = _read_journal(journal)
    migration = MIGRATIONS_BY_ID.get(record.get("migration"))
    if migration is None:
        raise _error("SCHEMA_MIGRATION_JOURNAL", "тип миграции не зарегистрирован", journal)
    _validate_record(root, record, migration)
    _apply_entries(root, record["files"])
    _finish(root, journal, record["operation"])


def _validate_record(root: Path, record: dict, migration) -> None:
    files = record.get("files")
    operation = record.get("operation")
    if (record.get("format") != 1 or not isinstance(operation, str) or not operation.isalnum()
            or not isinstance(files, list) or not files or any(not isinstance(item, dict) for item in files)):
        raise _error("SCHEMA_MIGRATION_JOURNAL", "журнал преобразования повреждён", root / _JOURNAL)
    if files[-1].get("target") != PROJECT_MANIFEST or record.get("target_version") != migration.target_version:
        raise _error("SCHEMA_MIGRATION_JOURNAL", "порядок или версия миграции не совпадают", root / _JOURNAL)
    _validate_stage_files(root, operation, files)
    migration.validate_staged(root, files, record.get("project_id"))


def _validate_stage_files(root: Path, operation: str, files: list[dict]) -> None:
    stage_root = (root / _ROOT / operation).resolve()
    targets = set()
    for entry in files:
        target = resolve_project_path(root, entry.get("target", ""))
        staged = resolve_project_path(root, entry.get("staged", ""))
        if entry["target"] in targets or not staged.is_relative_to(stage_root) or staged.is_symlink():
            raise _error("SCHEMA_MIGRATION_JOURNAL", "недопустимый путь staging", staged)
        targets.add(entry["target"])
        _check_entry_hashes(target, staged, entry)


def _check_entry_hashes(target: Path, staged: Path, entry: dict) -> None:
    try:
        staged_hash = _hash(staged.read_bytes())
        target_hash = _hash(target.read_bytes())
    except OSError as exc:
        raise _error("SCHEMA_MIGRATION_CONFLICT", "файл операции отсутствует", target) from exc
    if staged_hash != entry.get("new") or target_hash not in {entry.get("old"), entry.get("new")}:
        raise _error("SCHEMA_MIGRATION_CONFLICT", "файл изменён во время миграции", target)


def _apply_entries(root: Path, files: list[dict]) -> None:
    for entry in files:
        target = resolve_project_path(root, entry["target"])
        if _hash(target.read_bytes()) == entry["old"]:
            staged = resolve_project_path(root, entry["staged"])
            atomic_write(target, staged.read_bytes(), entry["old"])


def _finish(root: Path, journal: Path, operation: str) -> None:
    journal.unlink()
    shutil.rmtree(resolve_project_path(root, (_ROOT / operation).as_posix()))


def _read_journal(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            return record
    except (OSError, json.JSONDecodeError):
        pass
    raise _error("SCHEMA_MIGRATION_JOURNAL", "журнал преобразования повреждён", path)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error(code: str, message: str, path: Path) -> ProjectError:
    return ProjectError(Diagnostic(code, message, path))
