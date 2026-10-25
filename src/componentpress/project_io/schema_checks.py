"""Read-only format checks performed before project recovery can write files."""

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
    check_schema(path)
    document, _ = parse_component(path.read_text(encoding="utf-8"), path, variables)
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


def preflight_project_schemas(root: Path) -> None:
    """Reject every unsupported document before locks or journals can alter the tree."""
    root = root.resolve()
    project_path = project_manifest_path(root)
    check_schema(project_path)
    project_model, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    for entry in project_model.components:
        candidate = resolve_project_path(root, entry.path)
        if not candidate.is_file():
            raise ProjectError(Diagnostic("COMPONENT_FILE_MISSING", "файл зарегистрированного компонента не найден", candidate))
        _check_component(candidate, project_model.variables, entry.id)

    # add-component recovery can remove the unregistered component named here.
    transaction_journal = root / ".componentpress" / "transactions" / "add-component.json"
    if transaction_journal.is_file():
        import json

        try:
            record = json.loads(transaction_journal.read_text(encoding="utf-8"))
            relative = record.get("component")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return  # The recovery routine diagnoses malformed records.
        if isinstance(relative, str) and relative.startswith("components/") and relative.endswith(".yaml"):
            candidate = resolve_project_path(root, relative)
            if candidate.is_file():
                try:
                    check_schema(candidate)
                except ProjectError as exc:
                    # Recovery owns malformed/uncommitted files, but it must
                    # never get the chance to overwrite a readable old schema.
                    if exc.diagnostic.code not in {"YAML_STRUCTURE", "YAML_SYNTAX", "YAML_UNSUPPORTED", "TEXT_ENCODING"}:
                        raise

    membership_journal = root / ".componentpress" / "transactions" / "component-membership.json"
    if membership_journal.is_file():
        import json

        try:
            record = json.loads(membership_journal.read_text(encoding="utf-8"))
            backup_path = record.get("backup")
            if isinstance(backup_path, str):
                backup = resolve_project_path(root, backup_path)
                if backup.is_file():
                    _check_component(backup, project_model.variables)
                    if file_hash(project_manifest_path(root)) == record["old_project_hash"]:
                        entry = next((item for item in project_model.components if item.path == record["component"]), None)
                        document, _ = parse_component(backup.read_text(encoding="utf-8"), backup, project_model.variables)
                        if entry is None or document.id != entry.id:
                            raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID резервной копии не совпадает с реестром", backup, "id"))
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return  # Recovery reports malformed transaction state without writing.

    # An interrupted migration may restore an old YAML document from its backup.
    migration_journal = root / ".componentpress" / "migrations" / "v1-to-v2-active.json"
    if migration_journal.is_file():
        import json

        try:
            record = json.loads(migration_journal.read_text(encoding="utf-8"))
            if record.get("status") == "committed":
                return
            backup = resolve_project_path(root, record["backup"])
            files = record["files"]
            if not isinstance(files, dict):
                return
            _preflight_migration_backup(backup, files, project_model)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
            return  # Let the established recovery diagnostic handle malformed journals.
