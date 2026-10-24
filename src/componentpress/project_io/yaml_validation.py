"""Validate the public YAML schema and component bindings."""

import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from componentpress.bindings.grammar import GrammarError, parse_references
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.html_resources import html_image_sources
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from .paths import validate_relative_path
from .yaml_read import _field_line


Model = TypeVar("Model", bound=BaseModel)


def _second_duplicate(tree: CommentedMap, node_id: str) -> tuple[str, int | None] | None:
    matches: list[tuple[str, int | None]] = []
    _collect_duplicate_locations(tree, node_id, (), matches, tree)
    return matches[1] if len(matches) > 1 else None


def _collect_duplicate_locations(node: Any, node_id: str, prefix: tuple[Any, ...], matches: list[tuple[str, int | None]], tree: CommentedMap) -> None:
    if isinstance(node, CommentedMap):
        _collect_mapping_ids(node, node_id, prefix, matches, tree)
    elif isinstance(node, CommentedSeq):
        for index, child in enumerate(node):
            _collect_duplicate_locations(child, node_id, (*prefix, index), matches, tree)


def _collect_mapping_ids(node: CommentedMap, node_id: str, prefix: tuple[Any, ...], matches: list[tuple[str, int | None]], tree: CommentedMap) -> None:
    if node.get("id") == node_id:
        address = (*prefix, "id")
        matches.append((".".join(map(str, address)), _field_line(tree, address)))
    for key, child in node.items():
        _collect_duplicate_locations(child, node_id, (*prefix, key), matches, tree)


def _validation_error(exc: ValidationError, tree: CommentedMap, path: Path) -> ProjectError:
    error = exc.errors()[0]
    location = tuple(error["loc"])
    field = ".".join(str(part) for part in location) or None
    message = str(error["msg"])
    code = _validation_code(message)
    if code in ("NODE_ID_DUPLICATE", "COMPONENT_ID_DUPLICATE"):
        match = re.search(r"повторный ID (?:узла|компонента) ['\"]?([^'\"\s]+)", message)
        found = _second_duplicate(tree, match.group(1)) if match else None
        if found:
            field, line = found
            return ProjectError(Diagnostic(code, message, path, field, line))
    return ProjectError(Diagnostic(code, message, path, field, _field_line(tree, location)))


def _validation_code(message: str) -> str:
    if "повторный ID узла" in message:
        return "NODE_ID_DUPLICATE"
    if "повторный ID компонента" in message:
        return "COMPONENT_ID_DUPLICATE"
    return "DOCUMENT_INVALID"


def _validate(tree: CommentedMap, cls: type[Model], path: Path) -> Model:
    version = tree.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version", _field_line(tree, ("schema_version",))))
    try:
        return cls.model_validate(tree)
    except ValidationError as exc:
        raise _validation_error(exc, tree, path) from exc


def _grammar(value: str, path: Path, field: str, tree: CommentedMap, location: tuple[Any, ...], *, has_data: bool, variables: dict[str, str] | None = None) -> None:
    try:
        refs = parse_references(value)
    except GrammarError as exc:
        raise ProjectError(Diagnostic(exc.code, f"{exc} (символ {exc.offset + 1})", path, field, _field_line(tree, location))) from exc
    for ref in refs:
        if ref.kind == "column" and not has_data:
            raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", f"столбец {ref.name!r} без привязки к данным", path, field, _field_line(tree, location)))
        if ref.kind == "variable" and variables is not None and ref.name not in variables:
            raise ProjectError(Diagnostic("VARIABLE_UNKNOWN", f"нет переменной {ref.name!r}", path, field, _field_line(tree, location)))


def _template_path(value: str, path: Path, field: str) -> None:
    # Placeholder names are replaced only for structural path validation.
    from re import sub
    candidate = sub(r"(?<!\\)\{\{[^{}]*\}\}|(?<!\\)\{[^{}]*\}", "value", value)
    candidate = candidate.replace(r"\{", "{").replace(r"\}", "}")
    try:
        validate_relative_path(candidate)
    except ProjectError as exc:
        raise ProjectError(Diagnostic(
            exc.diagnostic.code, exc.diagnostic.message, path, field,
            message_key=exc.diagnostic.message_key, params=exc.diagnostic.params,
            incident_id=exc.diagnostic.incident_id,
        )) from exc


def _project_paths(model: ProjectDefinition) -> list[tuple[str, str]]:
    paths = [(f"components.{i}.path", item.path) for i, item in enumerate(model.components)]
    if model.data_source is not None:
        paths.append(("data_source.path", model.data_source.path))
    paths.extend((f"images.{i}", item) for i, item in enumerate(model.images))
    paths.extend((f"icons.{name}.path", item.path) for name, item in model.icons.items())
    return paths


def _validate_project_paths(model: ProjectDefinition, path: Path) -> None:
    for field, value in _project_paths(model):
        _validate_project_path(field, value, path)


def _validate_project_path(field: str, value: str, path: Path) -> None:
    try:
        validate_relative_path(value)
    except ProjectError as exc:
        raise _path_error(exc, path, field) from exc
    if field.startswith(("icons.", "images.")):
        _validate_image_path(value, path, field)
    if field == "data_source.path" and not value.lower().endswith(".xlsx"):
        raise ProjectError(Diagnostic("DATA_SOURCE_PATH", "ожидается файл .xlsx", path, field))


def _path_error(exc: ProjectError, path: Path, field: str) -> ProjectError:
    item = exc.diagnostic
    return ProjectError(Diagnostic(item.code, item.message, path, field, message_key=item.message_key, params=item.params, incident_id=item.incident_id))


def _validate_image_path(value: str, path: Path, field: str) -> None:
    flat = value.startswith("assets/images/") and "/" not in value[len("assets/images/"):]
    if not flat or not value.lower().endswith((".png", ".jpg", ".jpeg")):
        raise ProjectError(Diagnostic("ICON_PATH", "изображение должно ссылаться на PNG/JPEG непосредственно в assets/images", path, field))


def _normalize_image_node(item: dict[str, Any], path: Path, tree: CommentedMap, location: tuple[Any, ...]) -> None:
    if item.get("type") != "image":
        return
    source = item.get("source")
    if not isinstance(source, dict):
        raise ProjectError(Diagnostic("CONTENT_SOURCE_INVALID", "источник изображения должен задавать mode и path/column", path, "source"))
    mode = source.get("mode", "manual")
    _reject_unknown_fields(source, _source_fields(mode), path, tree, (*location, "source"))
    item["source_mode"] = mode
    item["source_column"] = source.get("column")
    item["source"] = source.get("path", "")


def _normalize_html_node(item: dict[str, Any], path: Path, tree: CommentedMap, location: tuple[Any, ...]) -> None:
    if item.get("type") != "html":
        return
    content = item.get("content")
    if not isinstance(content, dict):
        raise ProjectError(Diagnostic("CONTENT_SOURCE_INVALID", "содержимое HTML должно задавать mode и html/column", path, "content"))
    mode = content.get("mode", "manual")
    _reject_unknown_fields(content, _content_fields(mode), path, tree, (*location, "content"))
    item["content_mode"] = mode
    item["content_column"] = content.get("column")
    item["html"] = content.get("html", "")
    if item["content_mode"] == "manual":
        from componentpress.bindings.html_policy import validate_html

        validate_html(item["html"], path, f"elements.{item.get('id', '?')}.content.html")
    del item["content"]


def _normalize_component_nodes(items: Any, path: Path, tree: CommentedMap, prefix: tuple[Any, ...] = ("elements",)) -> None:
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        location = (*prefix, index)
        _reject_internal_fields(item, path, tree, location)
        _normalize_image_node(item, path, tree, location)
        _normalize_html_node(item, path, tree, location)
        if item.get("type") in ("group", "conditional_group"):
            _normalize_component_nodes(item.get("children"), path, tree, (*location, "children"))


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], path: Path, tree: CommentedMap, location: tuple[Any, ...]) -> None:
    for key in value:
        if key not in allowed:
            field = ".".join(map(str, (*location, key)))
            line = _field_line(tree, (*location, key))
            raise ProjectError(Diagnostic("DOCUMENT_INVALID", "Unknown YAML field", path, field, line, message_key="error.yaml_unknown_field", params={"field": str(key)}))


def _source_fields(mode: Any) -> set[str]:
    return {"mode", "path"} if mode == "manual" else {"mode", "column"} if mode == "column" else {"mode", "path", "column"}


def _content_fields(mode: Any) -> set[str]:
    return {"mode", "html"} if mode == "manual" else {"mode", "column"} if mode == "column" else {"mode", "html", "column"}


def _reject_internal_fields(item: dict[str, Any], path: Path, tree: CommentedMap, location: tuple[Any, ...]) -> None:
    _reject_unknown_fields(item, item.keys() - {"source_mode", "content_mode"}, path, tree, location)


def _check_component_schema(tree: CommentedMap, path: Path) -> None:
    version = tree.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version", _field_line(tree, ("schema_version",))))


def _field_address(location: tuple[Any, ...]) -> str:
    return ".".join(map(str, location))


def _column_without_data(path: Path, field: str, message: str) -> ProjectError:
    return ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", message, path, field))


def _validate_image_binding(node: ImageNode, base: tuple[Any, ...], model: ComponentDefinition, path: Path) -> None:
    location = (*base, "source")
    field = _field_address(location)
    if node.source_mode == "manual":
        if not (node.optional and not node.source):
            _template_path(node.source, path, field)
    elif model.data is None:
        raise _column_without_data(path, field, "столбец изображения без привязки к листу")


def _validate_html_references(node: HtmlNode, path: Path, field: str, tree: CommentedMap, location: tuple[Any, ...], variables: dict[str, str] | None) -> None:
    try:
        refs = parse_references(node.html)
    except GrammarError as exc:
        raise ProjectError(Diagnostic(exc.code, f"{exc} (символ {exc.offset + 1})", path, field, _field_line(tree, location))) from exc
    for ref in refs:
        if ref.kind == "column":
            if not re.fullmatch(r"[A-Za-z0-9_-]+", ref.name):
                raise ProjectError(Diagnostic("TEMPLATE_SYNTAX", "неверное имя ключа изображения", path, field, _field_line(tree, location)))
        elif variables is not None and ref.name not in variables:
            raise ProjectError(Diagnostic("VARIABLE_UNKNOWN", f"нет переменной {ref.name!r}", path, field, _field_line(tree, location)))
    _validate_html_paths(node, path, field)


def _validate_html_paths(node: HtmlNode, path: Path, field: str) -> None:
    for source in html_image_sources(node.html):
        _template_path(source, path, f"{field}.img.src")


def _validate_html_binding(
    node: HtmlNode, base: tuple[Any, ...], model: ComponentDefinition,
    path: Path, tree: CommentedMap, variables: dict[str, str] | None,
) -> None:
    location = (*base, "html")
    field = _field_address(location)
    if node.content_mode == "manual":
        _validate_html_references(node, path, field, tree, location, variables)
    elif model.data is None:
        raise _column_without_data(path, field, "столбец HTML без привязки к листу")


def _validate_conditional_binding(node: ConditionalGroupNode, base: tuple[Any, ...], path: Path) -> None:
    if node.condition_column:
        field = _field_address((*base, "condition_column"))
        raise ProjectError(Diagnostic(
            "COLUMN_WITHOUT_DATA",
            "Условной группе нужен источник Excel для столбца {column}.",
            path,
            field,
            message_key="error.condition_column_without_data",
            params={"column": node.condition_column},
        ))


def _visit_component_nodes(
    nodes: tuple[Node, ...], prefix: tuple[Any, ...], model: ComponentDefinition,
    path: Path, tree: CommentedMap, variables: dict[str, str] | None,
) -> None:
    for index, node in enumerate(nodes):
        base = (*prefix, index)
        if isinstance(node, ImageNode):
            _validate_image_binding(node, base, model, path)
        elif isinstance(node, HtmlNode):
            _validate_html_binding(node, base, model, path, tree, variables)
        elif isinstance(node, ConditionalGroupNode) and model.data is None:
            _validate_conditional_binding(node, base, path)
        if isinstance(node, GroupNode):
            _visit_component_nodes(node.children, (*base, "children"), model, path, tree, variables)
