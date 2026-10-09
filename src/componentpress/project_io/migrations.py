"""Recoverable, comment-preserving project format migrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.schema import SCHEMA_VERSION
from .files import atomic_write
from .paths import resolve_project_path
from .yaml_codec import _load


ACTIVE_JOURNAL = Path(".componentpress/migrations/v1-to-v2-active.json")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(root: Path) -> tuple[Path, dict] | None:
    journal = root / ACTIVE_JOURNAL
    if not journal.exists():
        return None
    try:
        return journal, json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(Diagnostic("MIGRATION_RECOVERY", f"повреждён журнал миграции: {exc}", journal)) from exc


def migration_required(root: Path) -> bool:
    # Pre-0.08 formats are intentionally rejected without rewriting user files.
    return False


def recovery_required(root: Path) -> bool:
    return (root / ACTIVE_JOURNAL).exists()


def recover_migration(root: Path) -> None:
    """Restore all originals after an interrupted migration."""
    loaded = _read_record(root)
    if loaded is None:
        return
    journal, record = loaded
    if record.get("status") == "committed":
        journal.unlink()
        return
    try:
        backup = resolve_project_path(root, record["backup"])
        files = record["files"]
        if not isinstance(files, dict):
            raise ValueError("неверный список файлов")
        restore: list[tuple[Path, bytes, str]] = []
        for relative, hashes in files.items():
            target = resolve_project_path(root, relative)
            source = backup / Path(relative)
            data = source.read_bytes()
            if _sha(data) != hashes["old"]:
                raise ValueError(f"повреждена резервная копия {relative}")
            current = _sha(target.read_bytes())
            if current not in (hashes["old"], hashes["new"]):
                raise ValueError(f"файл изменён посторонней программой: {relative}")
            restore.append((target, data, current))
        for target, data, current in restore:
            if current != _sha(data):
                atomic_write(target, data, current)
        journal.unlink()
    except (KeyError, OSError, ValueError, ProjectError) as exc:
        raise ProjectError(Diagnostic("MIGRATION_RECOVERY", f"не удалось восстановить формат 1: {exc}", journal)) from exc


def migrate_v1_to_v2(root: Path) -> Path | None:
    """Retained compatibility entry point; published legacy formats are read-only."""
    root = root.resolve()
    from .schema_checks import preflight_project_schemas

    preflight_project_schemas(root)
    recover_migration(root)
    project_path = root / "project.yaml"
    project_bytes = project_path.read_bytes()
    project_tree = _load(project_bytes.decode("utf-8"), project_path)
    version = project_tree.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"версия схемы {version!r} не поддерживается; автоматическая миграция отключена", project_path, "schema_version"))
    return None
