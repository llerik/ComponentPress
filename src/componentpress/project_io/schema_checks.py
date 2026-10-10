"""Read-only format checks performed before project recovery can write files."""

from pathlib import Path

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.schema import SCHEMA_VERSION

from .paths import resolve_project_path
from .yaml_codec import _load


def check_schema(path: Path) -> dict:
    try:
        tree = _load(path.read_text(encoding="utf-8"), path)
    except UnicodeDecodeError as exc:
        raise ProjectError(Diagnostic("TEXT_ENCODING", "файл должен быть UTF-8", path)) from exc
    version = tree.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version"))
    return tree


def preflight_project_schemas(root: Path) -> None:
    """Reject every unsupported document before locks or journals can alter the tree."""
    root = root.resolve()
    project_path = root / "project.yaml"
    project = check_schema(project_path)
    references = project.get("components", [])
    if not isinstance(references, list):
        return  # The normal parser reports the structural error without recovering files.
    for entry in references:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        relative = entry["path"]
        if not relative.startswith("components/") or not relative.endswith(".yaml"):
            continue
        candidate = resolve_project_path(root, relative)
        if candidate.is_file():
            check_schema(candidate)

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
                    check_schema(backup)
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
            for relative in files:
                if relative == "project.yaml" or (
                    isinstance(relative, str)
                    and relative.startswith("components/")
                    and relative.endswith(".yaml")
                ):
                    check_schema(resolve_project_path(backup, relative))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
            return  # Let the established recovery diagnostic handle malformed journals.
