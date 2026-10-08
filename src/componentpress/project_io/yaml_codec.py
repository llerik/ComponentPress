"""Round-trip YAML codec with strict schema and location-aware diagnostics."""

from copy import deepcopy
from io import StringIO
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken

from componentpress.bindings.grammar import GrammarError, parse_references
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from .paths import validate_relative_path


Model = TypeVar("Model", bound=BaseModel)


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.version = (1, 2)
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.width = 4096
    return parser


def _field_line(tree: Any, location: tuple[Any, ...]) -> int | None:
    node = tree
    line: int | None = None
    for segment in location:
        try:
            if isinstance(node, CommentedMap):
                line = node.lc.key(segment)[0] + 1
            elif isinstance(node, CommentedSeq):
                line = node.lc.item(segment)[0] + 1
            node = node[segment]
        except (KeyError, IndexError, TypeError):
            break
    return line


def _load(text: str, path: Path) -> CommentedMap:
    yaml = _yaml()
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise ProjectError(Diagnostic("YAML_UNSUPPORTED", "теги и якоря YAML не поддерживаются", path, line=token.start_mark.line + 1))
        tree = yaml.load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        line = mark.line + 1 if mark else None
        raise ProjectError(Diagnostic("YAML_SYNTAX", str(exc).splitlines()[0], path, line=line)) from exc
    if not isinstance(tree, CommentedMap):
        raise ProjectError(Diagnostic("YAML_STRUCTURE", "корневое значение должно быть словарём", path))

    def reject_merge(node: Any) -> None:
        if isinstance(node, CommentedMap):
            if getattr(node, "merge", None) or "<<" in node:
                raise ProjectError(Diagnostic("YAML_UNSUPPORTED", "слияние YAML не поддерживается", path))
            for value in node.values():
                reject_merge(value)
        elif isinstance(node, CommentedSeq):
            for value in node:
                reject_merge(value)

    reject_merge(tree)
    return tree


def _validate(tree: CommentedMap, cls: type[Model], path: Path) -> Model:
    version = tree.get("schema_version")
    if type(version) is not int or version != 4:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version", _field_line(tree, ("schema_version",))))
    try:
        return cls.model_validate(tree)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = tuple(error["loc"])
        field = ".".join(str(part) for part in location) or None
        message = str(error["msg"])
        code = "NODE_ID_DUPLICATE" if "повторный ID узла" in message else "COMPONENT_ID_DUPLICATE" if "повторный ID компонента" in message else "DOCUMENT_INVALID"
        line = _field_line(tree, location)
        if code in ("NODE_ID_DUPLICATE", "COMPONENT_ID_DUPLICATE"):
            duplicate = re.search(r"повторный ID (?:узла|компонента) ['\"]?([^'\"\s]+)", message)
            if duplicate:
                count = 0

                def locate(node: Any, prefix: tuple[Any, ...]) -> tuple[str, int | None] | None:
                    nonlocal count
                    if isinstance(node, CommentedMap):
                        if node.get("id") == duplicate.group(1):
                            count += 1
                            if count == 2:
                                address = (*prefix, "id")
                                return ".".join(map(str, address)), _field_line(tree, address)
                        for key, child in node.items():
                            found = locate(child, (*prefix, key))
                            if found:
                                return found
                    elif isinstance(node, CommentedSeq):
                        for index, child in enumerate(node):
                            found = locate(child, (*prefix, index))
                            if found:
                                return found
                    return None

                found = locate(tree, ())
                if found:
                    field, line = found
        raise ProjectError(Diagnostic(code, message, path, field, line)) from exc


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
        raise ProjectError(Diagnostic(exc.diagnostic.code, exc.diagnostic.message, path, field)) from exc


class _ImageSources(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            for name, value in attrs:
                if name.lower() == "src" and value is not None:
                    self.sources.append(value)


def html_image_sources(html: str) -> tuple[str, ...]:
    parser = _ImageSources()
    parser.feed(html)
    return tuple(parser.sources)


def parse_project(text: str, path: Path) -> tuple[ProjectDefinition, CommentedMap]:
    tree = _load(text, path)
    model = _validate(tree, ProjectDefinition, path)
    paths: list[tuple[str, str]] = []
    paths.extend((f"components.{i}.path", item.path) for i, item in enumerate(model.components))
    if model.data_source is not None:
        paths.append(("data_source.path", model.data_source.path))
    paths.extend((f"icons.{name}.path", item.path) for name, item in model.icons.items())
    for field, value in paths:
        try:
            validate_relative_path(value)
        except ProjectError as exc:
            raise ProjectError(Diagnostic(exc.diagnostic.code, exc.diagnostic.message, path, field)) from exc
        if field.startswith("icons.") and (not value.startswith("assets/images/") or not value.lower().endswith((".png", ".jpg", ".jpeg"))):
            raise ProjectError(Diagnostic("ICON_PATH", "иконка должна ссылаться на PNG/JPEG в assets/images", path, field))
        if field == "data_source.path" and not value.lower().endswith(".xlsx"):
            raise ProjectError(Diagnostic("DATA_SOURCE_PATH", "ожидается файл .xlsx", path, field))
    return model, tree


def parse_component(text: str, path: Path, variables: dict[str, str] | None = None) -> tuple[ComponentDefinition, CommentedMap]:
    tree = _load(text, path)
    version = tree.get("schema_version")
    if type(version) is not int or version != 4:
        raise ProjectError(Diagnostic("SCHEMA_UNSUPPORTED", f"неподдерживаемая версия схемы {version!r}", path, "schema_version", _field_line(tree, ("schema_version",))))
    model_tree = deepcopy(tree)
    # Keep the application model convenient for the renderer while the public
    # YAML format expresses an explicit manual/column content source.
    def normalize_nodes(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image" and not isinstance(item.get("source"), dict):
                raise ProjectError(Diagnostic("CONTENT_SOURCE_INVALID", "источник изображения должен задавать mode и path/column", path, "source"))
            if item.get("type") == "image" and isinstance(item.get("source"), dict):
                source = item["source"]
                item["source_mode"] = source.get("mode", "manual")
                item["source_column"] = source.get("column")
                item["source"] = source.get("path", "")
            if item.get("type") == "html" and not isinstance(item.get("content"), dict):
                raise ProjectError(Diagnostic("CONTENT_SOURCE_INVALID", "содержимое HTML должно задавать mode и html/column", path, "content"))
            if item.get("type") == "html" and isinstance(item.get("content"), dict):
                content = item["content"]
                item["content_mode"] = content.get("mode", "manual")
                item["content_column"] = content.get("column")
                item["html"] = content.get("html", "")
                if item["content_mode"] == "manual":
                    from componentpress.bindings.html_policy import validate_html

                    validate_html(item["html"], path, f"elements.{item.get('id', '?')}.content.html")
                del item["content"]
            if item.get("type") == "group":
                normalize_nodes(item.get("children"))
        # The v3 project has one source; components only select a sheet.
    if isinstance(model_tree.get("data"), dict):
        model_tree["data"].setdefault("source", "main")
    normalize_nodes(model_tree.get("elements"))
    model = _validate(model_tree, ComponentDefinition, path)

    def visit(nodes: tuple[Node, ...], prefix: tuple[Any, ...]) -> None:
        for i, node in enumerate(nodes):
            base = (*prefix, i)
            if isinstance(node, ImageNode):
                location = (*base, "source")
                field = ".".join(map(str, location))
                if node.source_mode == "manual":
                    _template_path(node.source, path, field)
                elif model.data is None:
                    raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "столбец изображения без привязки к листу", path, field))
            elif isinstance(node, HtmlNode):
                location = (*base, "html")
                field = ".".join(map(str, location))
                if node.content_mode == "manual":
                    _grammar(node.html, path, field, tree, location, has_data=False, variables=variables)
                    for source in html_image_sources(node.html):
                        _template_path(source, path, f"{field}.img.src")
                elif model.data is None:
                    raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "столбец HTML без привязки к листу", path, field))
            elif isinstance(node, GroupNode):
                visit(node.children, (*base, "children"))

    visit(model.elements, ("elements",))
    return model, tree


def dump_yaml(tree: CommentedMap) -> str:
    output = StringIO()
    _yaml().dump(tree, output)
    return output.getvalue()


def update_tree(
    tree: Any,
    value: Any,
    _id_pool: dict[str, CommentedMap] | None = None,
    _leading_comments: dict[str, str] | None = None,
) -> None:
    """Apply a validated model dump while retaining existing comments and key order."""
    if _id_pool is None:
        _id_pool = {}
        _leading_comments = {}

        def comment_text(tokens: Any) -> str:
            if tokens is None:
                return ""
            candidates = tokens if isinstance(tokens, (list, tuple)) else (tokens,)
            lines: list[str] = []
            for token in candidates:
                value = getattr(token, "value", "")
                for line in value.splitlines():
                    if "#" in line:
                        lines.append(line.split("#", 1)[1].strip())
            return "\n".join(line for line in lines if line)

        def collect(node: Any) -> None:
            if isinstance(node, CommentedMap):
                node_id = node.get("id")
                if isinstance(node_id, str):
                    _id_pool[node_id] = node
                for child in node.values():
                    collect(child)
            elif isinstance(node, CommentedSeq):
                if node and getattr(node.ca, "comment", None) and node.ca.comment[1]:
                    first_id = node[0].get("id") if isinstance(node[0], CommentedMap) else None
                    text = comment_text(node.ca.comment[1])
                    if isinstance(first_id, str) and text:
                        _leading_comments[first_id] = text
                        node.ca.comment[1] = None
                for index, child in enumerate(node[:-1]):
                    if not isinstance(child, CommentedMap):
                        continue
                    next_id = node[index + 1].get("id") if isinstance(node[index + 1], CommentedMap) else None
                    if not isinstance(next_id, str):
                        continue
                    for attachment in child.ca.items.values():
                        token = attachment[2] if len(attachment) > 2 else None
                        value_text = getattr(token, "value", "")
                        if token is not None and value_text.startswith("\n") and "#" in value_text:
                            text = comment_text(token)
                            if text:
                                _leading_comments[next_id] = text
                                attachment[2] = None
                for child in node:
                    collect(child)

        collect(tree)
    def schema_data(model: BaseModel) -> dict[str, Any]:
        raw = model.model_dump(mode="python", exclude_unset=True)
        if isinstance(model, ComponentDefinition):
            data = raw.get("data")
            if isinstance(data, dict):
                data.pop("source", None)
            raw["elements"] = [schema_data(node) for node in model.elements]
        elif isinstance(model, ImageNode):
            mode = raw.pop("source_mode", "manual")
            column = raw.pop("source_column", None)
            raw["source"] = {"mode": mode, **({"path": raw.pop("source")} if mode == "manual" else {"column": column})}
        elif isinstance(model, HtmlNode):
            mode = raw.pop("content_mode", "manual")
            column = raw.pop("content_column", None)
            html = raw.pop("html", "")
            raw["content"] = {"mode": mode, **({"html": html} if mode == "manual" else {"column": column})}
        elif isinstance(model, GroupNode):
            raw["children"] = [schema_data(node) for node in model.children]
        return raw

    if isinstance(value, BaseModel):
        value = schema_data(value)
    if isinstance(tree, CommentedMap) and isinstance(value, dict):
        for key in tuple(tree):
            if key not in value:
                del tree[key]
        for key, item in value.items():
            if key in tree and isinstance(tree[key], (CommentedMap, CommentedSeq)) and isinstance(item, (dict, list, tuple)):
                update_tree(tree[key], item, _id_pool, _leading_comments)
            elif isinstance(item, dict):
                created_map = CommentedMap()
                update_tree(created_map, item, _id_pool, _leading_comments)
                tree[key] = created_map
            elif isinstance(item, (list, tuple)):
                created_sequence = CommentedSeq()
                update_tree(created_sequence, item, _id_pool, _leading_comments)
                tree[key] = created_sequence
            else:
                tree[key] = item
    elif isinstance(tree, CommentedSeq) and isinstance(value, (list, tuple)):
        # Match mappings by stable IDs before falling back to their position.
        # This keeps comments attached to an element when layer operations reorder it.
        rebuilt: list[Any] = []
        for index, item in enumerate(value):
            raw = item.model_dump(mode="python", exclude_unset=True) if isinstance(item, BaseModel) else item
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            existing = _id_pool.get(raw_id) if isinstance(raw_id, str) else None
            if existing is None and raw_id is None and index < len(tree):
                candidate = tree[index]
                if isinstance(candidate, (CommentedMap, CommentedSeq)) and isinstance(raw, (dict, list, tuple)):
                    existing = candidate
            if existing is not None and isinstance(existing, (CommentedMap, CommentedSeq)) and isinstance(raw, (dict, list, tuple)):
                update_tree(existing, raw, _id_pool, _leading_comments)
                if isinstance(raw_id, str) and _leading_comments and raw_id in _leading_comments:
                    existing.yaml_set_start_comment(_leading_comments[raw_id])
                rebuilt.append(existing)
            elif isinstance(raw, dict):
                created = CommentedMap()
                update_tree(created, raw, _id_pool, _leading_comments)
                if isinstance(raw_id, str) and _leading_comments and raw_id in _leading_comments:
                    created.yaml_set_start_comment(_leading_comments[raw_id])
                rebuilt.append(created)
            elif isinstance(raw, (list, tuple)):
                created_sequence = CommentedSeq()
                update_tree(created_sequence, raw, _id_pool, _leading_comments)
                rebuilt.append(created_sequence)
            else:
                rebuilt.append(raw)
        tree.clear()
        tree.extend(rebuilt)
