"""Recoverable, comment-preserving project format migrations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .files import atomic_write
from .paths import resolve_project_path
from .yaml_codec import _load, dump_yaml, parse_component, parse_project


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
    path = root / "project.yaml"
    try:
        tree = _load(path.read_text(encoding="utf-8"), path)
    except UnicodeDecodeError as exc:
        raise ProjectError(Diagnostic("TEXT_ENCODING", "файл должен быть UTF-8", path)) from exc
    version = tree.get("schema_version")
    return version == 1 and not isinstance(version, bool)


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


def _add_node_names(nodes: CommentedSeq) -> None:
    for node in nodes:
        if not isinstance(node, CommentedMap):
            continue
        if "name" not in node:
            node.insert(1, "name", node.get("id", "Элемент"))
        children = node.get("children")
        if isinstance(children, CommentedSeq):
            _add_node_names(children)


def migrate_v1_to_v2(root: Path) -> Path | None:
    """Upgrade every YAML document as one recoverable transaction.

    The returned directory is the persistent copy of the pre-migration files.
    """
    root = root.resolve()
    recover_migration(root)
    project_path = root / "project.yaml"
    project_bytes = project_path.read_bytes()
    project_tree = _load(project_bytes.decode("utf-8"), project_path)
    version = project_tree.get("schema_version")
    if version == 2:
        return None
    if version != 1 or isinstance(version, bool):
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", project_path, "schema_version"))
    components = project_tree.get("components")
    if not isinstance(components, CommentedSeq):
        raise ProjectError(Diagnostic("DOCUMENT_INVALID", "components должен быть списком", project_path, "components"))

    originals: dict[str, bytes] = {"project.yaml": project_bytes}
    updated: dict[str, bytes] = {}
    project_tree["schema_version"] = 2
    updated["project.yaml"] = dump_yaml(project_tree).encode("utf-8")
    variables = dict(project_tree.get("variables") or {})
    for ref in components:
        if not isinstance(ref, CommentedMap) or not isinstance(ref.get("path"), str):
            raise ProjectError(Diagnostic("DOCUMENT_INVALID", "неверная запись компонента", project_path, "components"))
        relative = ref["path"]
        path = resolve_project_path(root, relative)
        data = path.read_bytes()
        tree = _load(data.decode("utf-8"), path)
        if tree.get("schema_version") != 1:
            raise ProjectError(Diagnostic("SCHEMA_MIXED", "ожидался документ формата 1", path, "schema_version"))
        tree["schema_version"] = 2
        elements = tree.get("elements")
        if isinstance(elements, CommentedSeq):
            _add_node_names(elements)
        originals[relative] = data
        updated[relative] = dump_yaml(tree).encode("utf-8")

    # Validate the complete future state before a backup or source write.
    project_model, _ = parse_project(updated["project.yaml"].decode("utf-8"), project_path)
    for ref in project_model.components:
        parse_component(updated[ref.path].decode("utf-8"), root / ref.path, variables)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_relative = f".componentpress/migrations/schema-1-{stamp}"
    backup = root / Path(backup_relative)
    for relative, data in originals.items():
        destination = backup / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = {
        "from": 1,
        "to": 2,
        "files": {relative: _sha(data) for relative, data in originals.items()},
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    journal = root / ACTIVE_JOURNAL
    record = {
        "status": "prepared",
        "backup": backup_relative,
        "files": {
            relative: {"old": _sha(data), "new": _sha(updated[relative])}
            for relative, data in originals.items()
        },
    }
    journal.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    try:
        for relative, data in updated.items():
            atomic_write(resolve_project_path(root, relative), data, _sha(originals[relative]))
        record["status"] = "committed"
        atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
        journal.unlink()
    except Exception:
        recover_migration(root)
        raise
    return backup
