"""Recoverable two-file component registration; project.yaml is committed last."""

import hashlib
import json
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .files import atomic_create, atomic_write, file_hash
from .paths import resolve_project_path


def _journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "add-component.json"


def recover(root: Path) -> None:
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
