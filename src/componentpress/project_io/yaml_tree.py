"""Convert domain models into round-trip YAML while retaining comments."""

from io import StringIO
from typing import Any

from pydantic import BaseModel
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, PositionedNode
from .yaml_read import _yaml


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
        _id_pool, _leading_comments = {}, {}
        _collect_comments(tree, _id_pool, _leading_comments)
    _update_tree(tree, _schema_data(value) if isinstance(value, BaseModel) else value, _id_pool, _leading_comments)


def _comment_text(tokens: Any) -> str:
    candidates = tokens if isinstance(tokens, (list, tuple)) else (tokens,)
    lines = [line.split("#", 1)[1].strip() for token in candidates for line in getattr(token, "value", "").splitlines() if "#" in line]
    return "\n".join(line for line in lines if line)


def _collect_comments(node: Any, ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    if isinstance(node, CommentedMap):
        _collect_mapping_comments(node, ids, leading)
    elif isinstance(node, CommentedSeq):
        _collect_sequence_comments(node, ids, leading)


def _collect_mapping_comments(node: CommentedMap, ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    node_id = node.get("id")
    if isinstance(node_id, str):
        ids[node_id] = node
    for key, child in node.items():
        _collect_mapping_child(node, key, child, ids, leading)


def _collect_mapping_child(node: CommentedMap, key: Any, child: Any, ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    if isinstance(child, CommentedSeq):
        if _collect_sequence_comments(child, ids, leading):
            _clear_sequence_start(node, key)
    else:
        _collect_comments(child, ids, leading)


def _clear_sequence_start(node: CommentedMap, key: Any) -> None:
    attachment = node.ca.items.get(key)
    if attachment and len(attachment) > 3:
        attachment[3] = None


def _collect_sequence_comments(node: CommentedSeq, ids: dict[str, CommentedMap], leading: dict[str, str]) -> bool:
    has_leading = _collect_sequence_start(node, leading)
    _collect_between_comments(node, leading)
    for child in node:
        _collect_comments(child, ids, leading)
    return has_leading


def _collect_sequence_start(node: CommentedSeq, leading: dict[str, str]) -> bool:
    comments = getattr(node.ca, "comment", None)
    first = node[0].get("id") if node and isinstance(node[0], CommentedMap) else None
    if comments and comments[1] and isinstance(first, str):
        text = _comment_text(comments[1])
        if text:
            leading[first] = text
            comments[1] = None
            return True
    return False


def _collect_between_comments(node: CommentedSeq, leading: dict[str, str]) -> None:
    for index, child in enumerate(node[:-1]):
        next_node = node[index + 1]
        if not isinstance(child, CommentedMap) or not isinstance(next_node, CommentedMap):
            continue
        _collect_item_comments(child, next_node.get("id"), leading)


def _collect_item_comments(node: CommentedMap, next_id: Any, leading: dict[str, str]) -> None:
    if not isinstance(next_id, str):
        return
    for attachment in node.ca.items.values():
        token = attachment[2] if len(attachment) > 2 else None
        value = getattr(token, "value", "")
        if token is not None and value.startswith("\n") and "#" in value:
            text = _comment_text(token)
            if text:
                leading[next_id] = text
                attachment[2] = None


def _schema_data(model: BaseModel) -> dict[str, Any]:
    raw = model.model_dump(mode="python", exclude_unset=True)
    if isinstance(model, ComponentDefinition):
        _schema_component(model, raw)
    elif isinstance(model, ImageNode):
        _schema_image(raw)
    elif isinstance(model, HtmlNode):
        _schema_html(raw)
    elif isinstance(model, GroupNode):
        _schema_group(model, raw)
    if isinstance(model, PositionedNode):
        raw["transform"] = model.transform.model_dump(mode="python")
    if isinstance(model, ConditionalGroupNode) and model.condition_operator in {"is_empty", "is_not_empty"}:
        raw.pop("condition_value", None)
    return raw


def _schema_component(model: ComponentDefinition, raw: dict[str, Any]) -> None:
    data = raw.get("data")
    if isinstance(data, dict):
        data.pop("source", None)
    raw["elements"] = [_schema_data(node) for node in model.elements]


def _schema_image(raw: dict[str, Any]) -> None:
    mode = raw.pop("source_mode", "manual")
    column = raw.pop("source_column", None)
    source = {"path": raw.pop("source")} if mode == "manual" else {"column": column}
    raw["source"] = {"mode": mode, **source}


def _schema_html(raw: dict[str, Any]) -> None:
    mode = raw.pop("content_mode", "manual")
    column = raw.pop("content_column", None)
    content = {"html": raw.pop("html", "")} if mode == "manual" else {"column": column}
    raw["content"] = {"mode": mode, **content}


def _schema_group(model: GroupNode, raw: dict[str, Any]) -> None:
    raw["frame"] = model.frame.model_dump(mode="python")
    raw["children"] = [_schema_data(node) for node in model.children]


def _update_tree(tree: Any, value: Any, ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    if isinstance(tree, CommentedMap) and isinstance(value, dict):
        _update_mapping(tree, value, ids, leading)
    elif isinstance(tree, CommentedSeq) and isinstance(value, (list, tuple)):
        _update_sequence(tree, value, ids, leading)


def _update_mapping(tree: CommentedMap, value: dict[str, Any], ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    for key in tuple(tree):
        if key not in value:
            del tree[key]
    for key, item in value.items():
        _update_mapping_item(tree, key, item, ids, leading)


def _update_mapping_item(tree: CommentedMap, key: Any, item: Any, ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    current = tree.get(key)
    if isinstance(current, (CommentedMap, CommentedSeq)) and isinstance(item, (dict, list, tuple)):
        _update_tree(current, item, ids, leading)
    elif isinstance(item, dict):
        tree[key] = _updated_map(item, ids, leading)
    elif isinstance(item, (list, tuple)):
        tree[key] = _updated_sequence(item, ids, leading)
    else:
        tree[key] = item


def _updated_map(value: dict[str, Any], ids: dict[str, CommentedMap], leading: dict[str, str]) -> CommentedMap:
    tree = CommentedMap()
    _update_tree(tree, value, ids, leading)
    return tree


def _updated_sequence(value: list[Any] | tuple[Any, ...], ids: dict[str, CommentedMap], leading: dict[str, str]) -> CommentedSeq:
    tree = CommentedSeq()
    _update_sequence(tree, value, ids, leading)
    return tree


def _update_sequence(tree: CommentedSeq, value: list[Any] | tuple[Any, ...], ids: dict[str, CommentedMap], leading: dict[str, str]) -> None:
    tree[:] = [_sequence_item(item, index, tree, ids, leading) for index, item in enumerate(value)]
    _format_sequence(tree)


def _sequence_item(item: Any, index: int, tree: CommentedSeq, ids: dict[str, CommentedMap], leading: dict[str, str]) -> Any:
    raw = item.model_dump(mode="python", exclude_unset=True) if isinstance(item, BaseModel) else item
    raw_id = raw.get("id") if isinstance(raw, dict) else None
    existing = _match_sequence_item(raw, raw_id, index, tree, ids)
    if existing is not None:
        _update_tree(existing, raw, ids, leading)
        _restore_leading_comment(existing, raw_id, leading)
        return existing
    return _new_sequence_item(raw, raw_id, ids, leading)


def _match_sequence_item(raw: Any, raw_id: Any, index: int, tree: CommentedSeq, ids: dict[str, CommentedMap]) -> Any:
    existing = ids.get(raw_id) if isinstance(raw_id, str) else None
    if existing is None and raw_id is None and index < len(tree):
        candidate = tree[index]
        if isinstance(candidate, (CommentedMap, CommentedSeq)) and isinstance(raw, (dict, list, tuple)):
            existing = candidate
    if isinstance(existing, (CommentedMap, CommentedSeq)) and isinstance(raw, (dict, list, tuple)):
        return existing
    return None


def _new_sequence_item(raw: Any, raw_id: Any, ids: dict[str, CommentedMap], leading: dict[str, str]) -> Any:
    if isinstance(raw, dict):
        return _restore_leading_comment(_updated_map(raw, ids, leading), raw_id, leading)
    if isinstance(raw, (list, tuple)):
        return _updated_sequence(raw, ids, leading)
    return raw


def _restore_leading_comment(node: Any, node_id: Any, leading: dict[str, str]) -> Any:
    if isinstance(node_id, str) and node_id in leading:
        node.yaml_set_start_comment(leading[node_id])
    return node


def _format_sequence(tree: CommentedSeq) -> None:
    if not all(isinstance(item, CommentedMap) and "id" in item and "type" in item for item in tree):
        return
    for index, item in enumerate(tree[:-1]):
        if not _ends_with_blank(item):
            _add_blank_line(_trailing_mapping(item, item))


def _ends_with_blank(node: Any) -> bool:
    if isinstance(node, CommentedSeq):
        return bool(node) and _ends_with_blank(node[-1])
    if not isinstance(node, CommentedMap) or not node:
        return False
    key = next(reversed(node))
    attachment = node.ca.items.get(key)
    if attachment and len(attachment) > 2 and _has_blank_line(attachment[2]):
        return True
    return _ends_with_blank(node[key])


def _has_blank_line(token: Any) -> bool:
    tokens = token if isinstance(token, (list, tuple)) else (token,)
    return any("\n\n" in getattr(item, "value", "") for item in tokens if item is not None)


def _trailing_mapping(node: Any, fallback: CommentedMap) -> CommentedMap:
    if isinstance(node, CommentedSeq) and node:
        return _trailing_mapping(node[-1], fallback)
    if isinstance(node, CommentedMap) and node:
        value = node[next(reversed(node))]
        return _trailing_mapping(value, fallback) if isinstance(value, (CommentedMap, CommentedSeq)) else node
    return fallback


def _add_blank_line(node: CommentedMap) -> None:
    key = next(reversed(node))
    attachment = list(node.ca.items.get(key, (None, None, None, None)))
    attachment.extend([None] * (4 - len(attachment)))
    attachment[2] = _blank_token(attachment[2])
    node.ca.items[key] = attachment


def _blank_token(token: Any) -> CommentToken:
    tokens = token if isinstance(token, (list, tuple)) else (token,)
    value = "".join(getattr(item, "value", "") for item in tokens if item is not None)
    return CommentToken(value + "\n\n", CommentMark(0))
