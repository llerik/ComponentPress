"""Project data conversion for schema 8 to schema 9."""

from __future__ import annotations

from pathlib import Path
import json

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.paths import PROJECT_MANIFEST, project_manifest_path, resolve_project_path
from componentpress.project_io.yaml_codec import _load, dump_yaml, parse_component, parse_project


ID = "schema-8-to-9"
SOURCE_VERSION = 8
TARGET_VERSION = 9


def prepare(root: Path) -> tuple[list[tuple[str, bytes]], str]:
    _reject_legacy_recovery(root)
    manifest = project_manifest_path(root)
    tree = _read_tree(manifest)
    _require_schema(tree, SOURCE_VERSION, manifest)
    refs = tree.get("components")
    if not isinstance(refs, list):
        raise _error("SCHEMA_MIGRATION_INVALID", "реестр компонентов повреждён", manifest)
    targets = [_convert_yaml(PROJECT_MANIFEST, manifest, tree)]
    paths = [item.get("path") for item in refs if isinstance(item, dict)]
    if len(paths) != len(refs) or not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths):
        raise _error("SCHEMA_MIGRATION_INVALID", "путь компонента повреждён", manifest)
    targets.extend(_convert_component(root, relative) for relative in paths)
    project, _ = parse_project(targets[0][1].decode("utf-8"), manifest)
    _validate_converted(root, project, targets[1:])
    return targets, project.id


def _reject_legacy_recovery(root: Path) -> None:
    transactions = root / ".componentpress" / "transactions"
    journals = [transactions / "add-component.json", transactions / "component-membership.json"]
    journals.extend((transactions / "resource-imports").glob("*.json"))
    journals.extend((root / ".componentpress" / "migrations").glob("*-active.json"))
    journals.extend((root / ".componentpress" / "versioning").glob("*/publish.json"))
    if any(path.is_file() for path in journals):
        raise _error("SCHEMA_MIGRATION_RECOVERY_PENDING", "сначала восстановите незавершённые операции в версии 0.21.9", root)


def _convert_component(root: Path, relative: str) -> tuple[str, bytes]:
    path = resolve_project_path(root, relative)
    tree = _read_tree(path)
    _require_schema(tree, SOURCE_VERSION, path)
    return _convert_yaml(relative, path, tree)


def _convert_yaml(relative: str, path: Path, tree: dict) -> tuple[str, bytes]:
    tree["schema_version"] = TARGET_VERSION
    for icon in (tree.get("icons") or {}).values():
        if isinstance(icon, dict):
            icon.pop("width_px", None)
            icon.pop("height_px", None)
    try:
        data = dump_yaml(tree).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error("SCHEMA_MIGRATION_INVALID", "не удалось преобразовать YAML", path) from exc
    return relative, data


def _validate_converted(root: Path, project, components: list[tuple[str, bytes]]) -> None:
    from componentpress.project_io.paths import check_exact_case

    for relative in (*project.images, *(icon.path for icon in project.icons.values())):
        check_exact_case(root, relative)
        resolve_project_path(root, relative)
    for relative, data in components:
        model, _ = parse_component(data.decode("utf-8"), root / relative, project.variables)
        reference = next(item for item in project.components if item.path == relative)
        if model.id != reference.id:
            raise _error("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", root / relative)
        _validate_node_paths(root, root / relative, model.elements)


def _validate_node_paths(root: Path, owner: Path, nodes: tuple) -> None:
    from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode
    from componentpress.domain.html_resources import html_image_sources

    for node in nodes:
        if isinstance(node, ImageNode) and node.source_mode == "manual":
            _check_resource(root, node.source)
        elif isinstance(node, HtmlNode) and node.content_mode == "manual":
            for path in html_image_sources(node.html):
                _check_resource(root, path)
        elif isinstance(node, GroupNode):
            _validate_node_paths(root, owner, node.children)


def _check_resource(root: Path, relative: str) -> None:
    from componentpress.project_io.paths import check_exact_case
    from componentpress.bindings.grammar import parse_references

    if parse_references(relative):
        return
    check_exact_case(root, relative)
    resolve_project_path(root, relative)


def validate_staged(root: Path, files: list[dict], project_id: str | None) -> None:
    manifest = next(item for item in files if item["target"] == PROJECT_MANIFEST)
    model, _ = parse_project((root / manifest["staged"]).read_text(encoding="utf-8"), root / PROJECT_MANIFEST)
    if model.id != project_id:
        raise _error("SCHEMA_MIGRATION_CONFLICT", "ID проекта изменился", root / PROJECT_MANIFEST)
    expected = {PROJECT_MANIFEST, *(item.path for item in model.components)}
    if expected != {item["target"] for item in files}:
        raise _error("SCHEMA_MIGRATION_CONFLICT", "набор компонентов изменился", root / PROJECT_MANIFEST)
    components = [(item["target"], (root / item["staged"]).read_bytes())
                  for item in files if item["target"] != PROJECT_MANIFEST]
    _validate_converted(root, model, components)


def _read_tree(path: Path) -> dict:
    try:
        tree = _load(path.read_text(encoding="utf-8"), path)
    except OSError as exc:
        raise _error("FILE_READ", str(exc), path) from exc
    if not isinstance(tree, dict):
        raise _error("SCHEMA_MIGRATION_INVALID", "корневой YAML должен быть объектом", path)
    return tree


def _require_schema(tree: dict, version: int, path: Path) -> None:
    if type(tree.get("schema_version")) is not int or tree["schema_version"] != version:
        raise _error("SCHEMA_UNSUPPORTED", f"ожидалась schema {version}", path)


def _error(code: str, message: str, path: Path) -> ProjectError:
    return ProjectError(Diagnostic(code, message, path))
