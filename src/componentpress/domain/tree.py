"""Pure operations over a component node tree.

The editor never treats Qt items as document storage.  Every operation in this
module returns a newly validated component, preserving immutable Pydantic
models and making the result suitable for undo snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node


class TreeOperationError(ValueError):
    """A user operation is not valid for the current tree."""


@dataclass(frozen=True)
class NodeLocation:
    node: Node
    parent_id: str | None
    index: int
    global_x: float
    global_y: float


def locations(component: ComponentDefinition) -> dict[str, NodeLocation]:
    result: dict[str, NodeLocation] = {}

    def visit(nodes: tuple[Node, ...], parent_id: str | None, ox: float, oy: float) -> None:
        for index, node in enumerate(nodes):
            gx, gy = ox + node.x_mm, oy + node.y_mm
            result[node.id] = NodeLocation(node, parent_id, index, gx, gy)
            if isinstance(node, GroupNode):
                visit(node.children, node.id, gx, gy)

    visit(component.elements, None, 0.0, 0.0)
    return result


def parent_nodes(component: ComponentDefinition, parent_id: str | None) -> tuple[Node, ...]:
    if parent_id is None:
        return component.elements
    found = locations(component).get(parent_id)
    if found is None or not isinstance(found.node, GroupNode):
        raise TreeOperationError("родительская группа не найдена")
    return found.node.children


def _replace_children(component: ComponentDefinition, parent_id: str | None, children: tuple[Node, ...]) -> ComponentDefinition:
    if parent_id is None:
        return component.model_copy(update={"elements": children})

    def replace(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        changed: list[Node] = []
        for node in nodes:
            if node.id == parent_id:
                if not isinstance(node, GroupNode):
                    raise TreeOperationError("родитель не является группой")
                changed.append(node.model_copy(update={"children": children}))
            elif isinstance(node, GroupNode):
                changed.append(node.model_copy(update={"children": replace(node.children)}))
            else:
                changed.append(node)
        return tuple(changed)

    return component.model_copy(update={"elements": replace(component.elements)})


def update_node(component: ComponentDefinition, node_id: str, **changes: object) -> ComponentDefinition:
    current = locations(component).get(node_id)
    if current is None:
        raise TreeOperationError(f"элемент {node_id!r} не найден")
    if _is_effectively_locked(component, node_id) and not (set(changes) == {"locked"} and not _has_locked_ancestor(component, node_id)):
        raise TreeOperationError("заблокированный элемент нельзя изменять визуально")

    def replace(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        result: list[Node] = []
        for node in nodes:
            if node.id == node_id:
                values = node.model_dump(mode="python")
                values.update(changes)
                result.append(type(node).model_validate(values))
            elif isinstance(node, GroupNode):
                result.append(node.model_copy(update={"children": replace(node.children)}))
            else:
                result.append(node)
        return tuple(result)

    return ComponentDefinition.model_validate(
        component.model_copy(update={"elements": replace(component.elements)}).model_dump(mode="python")
    )


def add_node(component: ComponentDefinition, node: Node, parent_id: str | None = None) -> ComponentDefinition:
    if node.id in locations(component):
        raise TreeOperationError(f"ID {node.id!r} уже используется")
    if parent_id is not None and _is_effectively_locked(component, parent_id):
        raise TreeOperationError("нельзя добавлять элементы в заблокированную группу")
    children = (*parent_nodes(component, parent_id), node)
    return ComponentDefinition.model_validate(
        _replace_children(component, parent_id, children).model_dump(mode="python")
    )


def _selection(component: ComponentDefinition, node_ids: Iterable[str], *, same_parent: bool = True) -> tuple[list[NodeLocation], str | None]:
    index = locations(component)
    unique = tuple(dict.fromkeys(node_ids))
    if not unique:
        raise TreeOperationError("ничего не выбрано")
    try:
        selected = [index[node_id] for node_id in unique]
    except KeyError as exc:
        raise TreeOperationError(f"элемент {exc.args[0]!r} не найден") from exc
    ids = set(unique)
    for item in selected:
        parent = item.parent_id
        while parent is not None:
            if parent in ids:
                raise TreeOperationError("нельзя одновременно выбрать группу и её потомка")
            parent = index[parent].parent_id
    parents = {item.parent_id for item in selected}
    if same_parent and len(parents) != 1:
        raise TreeOperationError("для операции нужны элементы одного уровня")
    return selected, next(iter(parents)) if len(parents) == 1 else None


def move_nodes(component: ComponentDefinition, node_ids: Iterable[str], dx_mm: float, dy_mm: float) -> ComponentDefinition:
    selected, _ = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "переместить")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "переместить")
    result = component
    for item in selected:
        result = update_node(result, item.node.id, x_mm=item.node.x_mm + dx_mm, y_mm=item.node.y_mm + dy_mm)
    return result


def resize_node(component: ComponentDefinition, node_id: str, width_mm: float, height_mm: float) -> ComponentDefinition:
    item = locations(component).get(node_id)
    if item is None:
        raise TreeOperationError(f"элемент {node_id!r} не найден")
    if isinstance(item.node, GroupNode):
        raise TreeOperationError("размер группы вычисляется по её содержимому")
    _ensure_unlocked_selection(component, (node_id,), "изменить размер")
    return update_node(component, node_id, width_mm=width_mm, height_mm=height_mm)


def remove_nodes(component: ComponentDefinition, node_ids: Iterable[str]) -> ComponentDefinition:
    selected, parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "удалить")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "удалить")
    remove = {item.node.id for item in selected}
    children = tuple(node for node in parent_nodes(component, parent_id) if node.id not in remove)
    return _replace_children(component, parent_id, children)


def reorder_nodes(component: ComponentDefinition, node_ids: Iterable[str], direction: int) -> ComponentDefinition:
    """Move a selection one step in back-to-front storage order."""
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    selected, parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "переставить")
    ids = {item.node.id for item in selected}
    nodes = list(parent_nodes(component, parent_id))
    if direction > 0:
        for index in range(len(nodes) - 2, -1, -1):
            if nodes[index].id in ids and nodes[index + 1].id not in ids:
                nodes[index], nodes[index + 1] = nodes[index + 1], nodes[index]
    else:
        for index in range(1, len(nodes)):
            if nodes[index].id in ids and nodes[index - 1].id not in ids:
                nodes[index], nodes[index - 1] = nodes[index - 1], nodes[index]
    return _replace_children(component, parent_id, tuple(nodes))


def place_nodes(component: ComponentDefinition, node_ids: Iterable[str], target_id: str) -> ComponentDefinition:
    """Place selected siblings at the target sibling's layer position."""
    selected, parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "переставить")
    index = locations(component)
    target = index.get(target_id)
    if target is None or target.parent_id != parent_id:
        raise TreeOperationError("перетаскивать порядок можно только среди соседей")
    ids = {item.node.id for item in selected}
    if target_id in ids:
        return component
    nodes = list(parent_nodes(component, parent_id))
    moved = [node for node in nodes if node.id in ids]
    remaining = [node for node in nodes if node.id not in ids]
    position = next(i for i, node in enumerate(remaining) if node.id == target_id)
    remaining[position:position] = moved
    return _replace_children(component, parent_id, tuple(remaining))


def group_nodes(component: ComponentDefinition, node_ids: Iterable[str], group_id: str, name: str) -> ComponentDefinition:
    if group_id in locations(component):
        raise TreeOperationError(f"ID {group_id!r} уже используется")
    selected, parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "сгруппировать")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "сгруппировать")
    if len(selected) < 2:
        raise TreeOperationError("для группы нужно выбрать не менее двух элементов")
    nodes = list(parent_nodes(component, parent_id))
    ids = {item.node.id for item in selected}
    ordered = [node for node in nodes if node.id in ids]
    group_x = min(node.x_mm for node in ordered)
    group_y = min(node.y_mm for node in ordered)
    children = tuple(
        node.model_copy(update={"x_mm": node.x_mm - group_x, "y_mm": node.y_mm - group_y})
        for node in ordered
    )
    group = GroupNode(id=group_id, type="group", name=name, x_mm=group_x, y_mm=group_y, children=children)
    top_index = max(index for index, node in enumerate(nodes) if node.id in ids)
    remaining = [node for node in nodes if node.id not in ids]
    insertion = sum(1 for index, node in enumerate(nodes) if index <= top_index and node.id not in ids)
    remaining.insert(insertion, group)
    return ComponentDefinition.model_validate(
        _replace_children(component, parent_id, tuple(remaining)).model_dump(mode="python")
    )


def ungroup_node(component: ComponentDefinition, group_id: str) -> ComponentDefinition:
    item = locations(component).get(group_id)
    if item is None or not isinstance(item.node, GroupNode):
        raise TreeOperationError("нужно выбрать одну группу")
    _ensure_unlocked_selection(component, (group_id,), "разгруппировать")
    _ensure_unprotected_subtrees(component, (group_id,), "разгруппировать")
    siblings = list(parent_nodes(component, item.parent_id))
    children = tuple(
        child.model_copy(update={"x_mm": child.x_mm + item.node.x_mm, "y_mm": child.y_mm + item.node.y_mm})
        for child in item.node.children
    )
    siblings[item.index:item.index + 1] = children
    return ComponentDefinition.model_validate(
        _replace_children(component, item.parent_id, tuple(siblings)).model_dump(mode="python")
    )


def reparent_nodes(component: ComponentDefinition, node_ids: Iterable[str], new_parent_id: str | None) -> ComponentDefinition:
    selected, old_parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "перенести")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "перенести")
    if new_parent_id is not None and _is_effectively_locked(component, new_parent_id):
        raise TreeOperationError("нельзя переносить в заблокированную группу")
    index = locations(component)
    if new_parent_id is not None:
        target = index.get(new_parent_id)
        if target is None or not isinstance(target.node, GroupNode):
            raise TreeOperationError("целевая группа не найдена")
        if new_parent_id in {item.node.id for item in selected}:
            raise TreeOperationError("группу нельзя поместить внутрь самой себя")
        ancestor = target.parent_id
        selected_ids = {item.node.id for item in selected}
        while ancestor is not None:
            if ancestor in selected_ids:
                raise TreeOperationError("перенос создаст цикл")
            ancestor = index[ancestor].parent_id
    if old_parent_id == new_parent_id:
        return component
    old_children = tuple(node for node in parent_nodes(component, old_parent_id) if node.id not in {item.node.id for item in selected})
    result = _replace_children(component, old_parent_id, old_children)
    refreshed = locations(result)
    target_x = refreshed[new_parent_id].global_x if new_parent_id is not None else 0.0
    target_y = refreshed[new_parent_id].global_y if new_parent_id is not None else 0.0
    moved = tuple(
        item.node.model_copy(update={"x_mm": item.global_x - target_x, "y_mm": item.global_y - target_y})
        for item in sorted(selected, key=lambda candidate: candidate.index)
    )
    return _replace_children(result, new_parent_id, (*parent_nodes(result, new_parent_id), *moved))


def _is_effectively_locked(component: ComponentDefinition, node_id: str) -> bool:
    index = locations(component)
    item = index[node_id]
    if item.node.locked:
        return True
    parent_id = item.parent_id
    while parent_id is not None:
        parent = index[parent_id]
        if parent.node.locked:
            return True
        parent_id = parent.parent_id
    return False


def _has_locked_ancestor(component: ComponentDefinition, node_id: str) -> bool:
    index = locations(component)
    parent_id = index[node_id].parent_id
    while parent_id is not None:
        parent = index[parent_id]
        if parent.node.locked:
            return True
        parent_id = parent.parent_id
    return False


def effectively_locked_ids(component: ComponentDefinition) -> frozenset[str]:
    return frozenset(node_id for node_id in locations(component) if _is_effectively_locked(component, node_id))


def _ensure_unlocked_selection(component: ComponentDefinition, node_ids: Iterable[str], action: str) -> None:
    locked = [node_id for node_id in node_ids if _is_effectively_locked(component, node_id)]
    if locked:
        raise TreeOperationError(f"нельзя {action}: элемент или его родительская группа заблокированы")


def _ensure_unprotected_subtrees(component: ComponentDefinition, node_ids: Iterable[str], action: str) -> None:
    index = locations(component)

    def has_locked_descendant(node: Node) -> bool:
        return node.locked or (isinstance(node, GroupNode) and any(has_locked_descendant(child) for child in node.children))

    if any(has_locked_descendant(index[node_id].node) for node_id in node_ids):
        raise TreeOperationError(f"нельзя {action}: в группе есть заблокированный элемент")


def align_nodes(component: ComponentDefinition, node_ids: Iterable[str], edge: str) -> ComponentDefinition:
    if edge not in {"left", "right", "top", "bottom"}:
        raise ValueError("неизвестная сторона выравнивания")
    selected, _ = _selection(component, node_ids)
    if len(selected) < 2:
        raise TreeOperationError("для выравнивания выберите не менее двух элементов")
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "выровнять")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "выровнять")
    bounds = {item.node.id: node_bounds(component, item.node.id) for item in selected}
    if edge in {"left", "top"}:
        target = min(bound[0 if edge == "left" else 1] for bound in bounds.values())
    else:
        target = max(
            bound[0] + bound[2] if edge == "right" else bound[1] + bound[3]
            for bound in bounds.values()
        )
    result = component
    for item in selected:
        x, y, width, height = bounds[item.node.id]
        if edge == "left":
            dx, dy = target - x, 0.0
        elif edge == "right":
            dx, dy = target - (x + width), 0.0
        elif edge == "top":
            dx, dy = 0.0, target - y
        else:
            dx, dy = 0.0, target - (y + height)
        if dx or dy:
            current = locations(result)[item.node.id].node
            result = update_node(result, item.node.id, x_mm=current.x_mm + dx, y_mm=current.y_mm + dy)
    return result


def node_bounds(component: ComponentDefinition, node_id: str) -> tuple[float, float, float, float]:
    index = locations(component)
    item = index[node_id]
    node = item.node
    if not isinstance(node, GroupNode):
        return item.global_x, item.global_y, node.width_mm, node.height_mm
    if not node.children:
        return item.global_x, item.global_y, 0.0, 0.0
    child_bounds = [node_bounds(component, child.id) for child in node.children]
    left = min(bound[0] for bound in child_bounds)
    top = min(bound[1] for bound in child_bounds)
    right = max(bound[0] + bound[2] for bound in child_bounds)
    bottom = max(bound[1] + bound[3] for bound in child_bounds)
    return left, top, right - left, bottom - top
