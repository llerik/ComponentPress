"""Read-only format checks performed before project recovery can write files."""

import json
from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.schema import SCHEMA_VERSION

from .files import file_hash
from .paths import PROJECT_MANIFEST, project_manifest_path, resolve_project_path
from .yaml_codec import _load, parse_project
from .yaml_codec import parse_component


def check_schema(path: Path) -> dict:
    try:
        tree = _load(path.read_text(encoding="utf-8"), path)
    except UnicodeDecodeError as exc:
        raise ProjectError(Diagnostic("TEXT_ENCODING", "файл должен быть UTF-8", path)) from exc
    version = tree.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version"))
    return tree


def _check_component(path: Path, variables, expected_id: str | None = None) -> None:
    try:
        check_schema(path)
        document, _ = parse_component(path.read_text(encoding="utf-8"), path, variables)
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
    if expected_id is not None and document.id != expected_id:
        raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", path, "id"))


def _preflight_migration_backup(backup: Path, files: dict, current_model) -> None:
    project_file = backup / PROJECT_MANIFEST
    backup_model = current_model
    if project_file.is_file() and PROJECT_MANIFEST in files:
        check_schema(project_file)
        backup_model, _ = parse_project(project_file.read_text(encoding="utf-8"), project_file)
    refs = {entry.path: entry.id for entry in backup_model.components}
    for relative in files:
        if isinstance(relative, str) and relative.startswith("components/") and relative.endswith(".yaml"):
            path = resolve_project_path(backup, relative)
            if path.is_file():
                _check_component(path, backup_model.variables, refs.get(relative))


def _membership_recovery_backup(root: Path, relative: str, manifest_hash: str,
                                expected_id: str, variables) -> Path | None:
    journal = root / ".componentpress" / "transactions" / "component-membership.json"
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        if (record.get("operation") != "remove" or record.get("component") != relative
                or record.get("old_project_hash") != manifest_hash):
            return None
        backup = resolve_project_path(root, record["backup"])
        history = (root / ".componentpress" / "transactions" / "component-history").resolve()
        if not backup.is_relative_to(history) or not backup.is_file():
            return None
        if file_hash(backup) != record.get("component_hash"):
            return None
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return None
    _check_component(backup, variables, expected_id)
    return backup


def _preflight_components(root: Path, model, manifest_hash: str) -> None:
    for entry in model.components:
        candidate = resolve_project_path(root, entry.path)
        if not candidate.is_file():
            candidate = _membership_recovery_backup(
                root, entry.path, manifest_hash, entry.id, model.variables,
            )
        if candidate is None or not candidate.is_file():
            raise ProjectError(Diagnostic(
                "COMPONENT_FILE_MISSING", "файл зарегистрированного компонента не найден",
                resolve_project_path(root, entry.path),
            ))
        _check_component(candidate, model.variables, entry.id)


def _preflight_add_component_journal(root: Path) -> None:
    journal = root / ".componentpress" / "transactions" / "add-component.json"
    if not journal.is_file():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        relative = record.get("component")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return
    if isinstance(relative, str) and relative.startswith("components/") and relative.endswith(".yaml"):
        candidate = resolve_project_path(root, relative)
        if candidate.is_file():
            _preflight_unregistered_component(candidate)


def _preflight_unregistered_component(path: Path) -> None:
    try:
        check_schema(path)
    except ProjectError as exc:
        allowed = {"YAML_STRUCTURE", "YAML_SYNTAX", "YAML_UNSUPPORTED", "TEXT_ENCODING"}
        if exc.diagnostic.code not in allowed:
            raise


def _preflight_membership_journal(root: Path, model) -> None:
    journal = root / ".componentpress" / "transactions" / "component-membership.json"
    if not journal.is_file():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        backup_path = record.get("backup")
        if not isinstance(backup_path, str):
            return
        backup = resolve_project_path(root, backup_path)
        if backup.is_file():
            _check_component(backup, model.variables)
        if file_hash(project_manifest_path(root)) != record["old_project_hash"]:
            return
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return
    entry = next((item for item in model.components if item.path == record.get("component")), None)
    if backup.is_file():
        _check_membership_backup_id(backup, model.variables, entry.id if entry else None)


def _check_membership_backup_id(backup: Path, variables, expected_id: str | None) -> None:
    document, _ = parse_component(backup.read_text(encoding="utf-8"), backup, variables)
    if expected_id is None or document.id != expected_id:
        raise ProjectError(Diagnostic(
            "COMPONENT_ID_MISMATCH", "ID резервной копии не совпадает с реестром",
            backup, "id",
        ))


def _preflight_migration_journal(root: Path, model) -> None:
    journal = root / ".componentpress" / "migrations" / "v1-to-v2-active.json"
    if not journal.is_file():
        return
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        if record.get("status") == "committed":
            return
        backup = resolve_project_path(root, record["backup"])
        files = record["files"]
        if isinstance(files, dict):
            _preflight_migration_backup(backup, files, model)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return


def preflight_project_schemas(root: Path) -> None:
    """Reject every unsupported document before project recovery can write."""
    root = root.resolve()
    project = project_manifest_path(root)
    check_schema(project)
    model, _ = parse_project(project.read_text(encoding="utf-8"), project)
    _preflight_components(root, model, file_hash(project))
    _preflight_add_component_journal(root)
    _preflight_membership_journal(root, model)
    _preflight_migration_journal(root, model)
