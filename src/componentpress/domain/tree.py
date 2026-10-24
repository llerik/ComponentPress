"""Pure operations over a component node tree.

The editor never treats Qt items as document storage.  Every operation in this
module returns a newly validated component, preserving immutable Pydantic
models and making the result suitable for undo snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Iterable

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, LineNode, Node, ShapeNode
from componentpress.domain.geometry import Affine, group_frame, local_bounds


class TreeOperationError(ValueError):
    """A user operation is not valid for the current tree."""


@dataclass(frozen=True)
class NodeLocation:
    node: Node
    parent_id: str | None
    index: int
    global_x: float
    global_y: float
    world: Affine


def locations(component: ComponentDefinition) -> dict[str, NodeLocation]:
    result: dict[str, NodeLocation] = {}

    def visit(nodes: tuple[Node, ...], parent_id: str | None, parent_world: Affine) -> None:
        for index, node in enumerate(nodes):
            world = parent_world.then(Affine.node(node))
            result[node.id] = NodeLocation(node, parent_id, index, world.x, world.y, world)
            if isinstance(node, GroupNode):
                visit(node.children, node.id, world)

    visit(component.elements, None, Affine())
    return result


def _refresh_frames(component: ComponentDefinition) -> ComponentDefinition:
    def visit(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        refreshed = []
        for node in nodes:
            if isinstance(node, GroupNode):
                children = visit(node.children)
                frame = group_frame(children) if children else node.frame
                refreshed.append(node.model_copy(update={"children": children, "frame": frame}))
            else:
                refreshed.append(node)
        return tuple(refreshed)

    return ComponentDefinition.model_validate(component.model_copy(update={"elements": visit(component.elements)}).model_dump(mode="python"))


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
    elements = _replace_nodes(component.elements, node_id, changes)
    return _refresh_frames(component.model_copy(update={"elements": elements}))


def _replace_nodes(nodes: tuple[Node, ...], node_id: str, changes: dict[str, object]) -> tuple[Node, ...]:
    result = []
    for node in nodes:
        if node.id == node_id:
            result.append(_updated_node(node, changes))
        elif isinstance(node, GroupNode):
            result.append(node.model_copy(update={"children": _replace_nodes(node.children, node_id, changes)}))
        else:
            result.append(node)
    return tuple(result)


def _updated_node(node: Node, changes: dict[str, object]) -> Node:
    values = node.model_dump(mode="python")
    values.update(changes)
    return type(node).model_validate(values)


def add_node(component: ComponentDefinition, node: Node, parent_id: str | None = None) -> ComponentDefinition:
    if node.id in locations(component):
        raise TreeOperationError(f"ID {node.id!r} уже используется")
    if parent_id is not None and _is_effectively_locked(component, parent_id):
        raise TreeOperationError("нельзя добавлять элементы в заблокированную группу")
    children = (*parent_nodes(component, parent_id), node)
    return _refresh_frames(_replace_children(component, parent_id, children))


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
        parent = locations(result).get(item.node.id)
        if parent is None:
            continue
        parent_world = Affine() if item.parent_id is None else locations(result)[item.parent_id].world
        local_dx = parent_world.inverse().a * dx_mm + parent_world.inverse().b * dy_mm
        local_dy = parent_world.inverse().c * dx_mm + parent_world.inverse().d * dy_mm
        result = update_node(result, item.node.id, x_mm=parent.node.x_mm + local_dx, y_mm=parent.node.y_mm + local_dy)
    return result


def resize_node(
    component: ComponentDefinition, node_id: str, width_mm: float, height_mm: float,
    anchor: tuple[float, float] = (0.0, 0.0),
) -> ComponentDefinition:
    item = locations(component).get(node_id)
    if item is None:
        raise TreeOperationError(f"элемент {node_id!r} не найден")
    _ensure_unlocked_selection(component, (node_id,), "изменить размер")
    if isinstance(item.node, GroupNode):
        return _resize_group(component, item, width_mm, height_mm, anchor)
    if isinstance(item.node, LineNode):
        return _resize_line(component, item.node, width_mm, height_mm, anchor)
    return _resize_positioned(component, item.node, width_mm, height_mm, anchor)


def _resize_group(component, item, width_mm, height_mm, anchor):
    _ensure_unprotected_subtrees(component, (item.node.id,), "изменить размер")
    old = item.node
    frame, transform = old.frame, old.transform
    scale_x = width_mm / (frame.width_mm * math.hypot(transform.m11, transform.m21))
    scale_y = height_mm / (frame.height_mm * math.hypot(transform.m12, transform.m22))
    ax, ay = frame.x_mm + frame.width_mm * anchor[0], frame.y_mm + frame.height_mm * anchor[1]
    shift_x = transform.m11 * ax + transform.m12 * ay - transform.m11 * scale_x * ax - transform.m12 * scale_y * ay
    shift_y = transform.m21 * ax + transform.m22 * ay - transform.m21 * scale_x * ax - transform.m22 * scale_y * ay
    changes = {"x_mm": old.x_mm + shift_x, "y_mm": old.y_mm + shift_y}
    changes["transform"] = _scaled_transform(transform, scale_x, scale_y)
    return update_node(component, old.id, **changes)


def _scaled_transform(transform, scale_x, scale_y):
    return {"m11": transform.m11 * scale_x, "m12": transform.m12 * scale_y,
            "m21": transform.m21 * scale_x, "m22": transform.m22 * scale_y}


def _resize_line(component, old, width_mm, height_mm, anchor):
    left, top, old_width, old_height = local_bounds(old)
    scale_x, scale_y = _transform_scales(old)
    width, height = _line_resize_dimensions(width_mm / scale_x, height_mm / scale_y)
    anchor_x = left + old_width * anchor[0]
    anchor_y = top + old_height * anchor[1]
    new_left = anchor_x - width * anchor[0]
    new_top = anchor_y - height * anchor[1]
    dx, dy = _signed_line_size(old, width, height)
    x_mm, y_mm = _line_resize_origin(old, new_left, new_top, width, height, dx, dy)
    return update_node(component, old.id, x_mm=x_mm, y_mm=y_mm, dx_mm=dx, dy_mm=dy)


def _line_resize_dimensions(width, height):
    return (0.001, height) if math.hypot(width, height) <= 0.001 else (width, height)


def _signed_line_size(old, width, height):
    return math.copysign(width, old.dx_mm or 1.0), math.copysign(height, old.dy_mm or 1.0)


def _line_resize_origin(old, left, top, width, height, dx, dy):
    x, y = (left if dx >= 0 else left + width), (top if dy >= 0 else top + height)
    return Affine.node(old).point(x, y)


def _resize_positioned(component, old, width_mm, height_mm, anchor):
    scale_x, scale_y = _transform_scales(old)
    width, height = width_mm / scale_x, height_mm / scale_y
    ax, ay = old.width_mm * anchor[0], old.height_mm * anchor[1]
    x_mm, y_mm = _positioned_resize_origin(old, width, height, anchor, ax, ay)
    return update_node(component, old.id, x_mm=x_mm, y_mm=y_mm, width_mm=width, height_mm=height)


def _positioned_resize_origin(old, width, height, anchor, ax, ay):
    transform = old.transform
    x_shift = transform.m11 * (ax - width * anchor[0]) + transform.m12 * (ay - height * anchor[1])
    y_shift = transform.m21 * (ax - width * anchor[0]) + transform.m22 * (ay - height * anchor[1])
    return old.x_mm + x_shift, old.y_mm + y_shift


def _transform_scales(node):
    transform = node.transform
    return math.hypot(transform.m11, transform.m21), math.hypot(transform.m12, transform.m22)


def rotate_node(component: ComponentDefinition, node_id: str, degrees: float) -> ComponentDefinition:
    item = locations(component).get(node_id)
    if item is None:
        raise TreeOperationError(f"элемент {node_id!r} не найден")
    _ensure_unlocked_selection(component, (node_id,), "повернуть")
    if isinstance(item.node, GroupNode):
        _ensure_unprotected_subtrees(component, (node_id,), "повернуть")
    left, top, width, height = local_bounds(item.node)
    current = Affine.node(item.node)
    center_x, center_y = left + width / 2, top + height / 2
    world_center = current.point(center_x, center_y)
    delta = math.radians(degrees - math.degrees(math.atan2(current.c, current.a)))
    cosine, sine = math.cos(delta), math.sin(delta)
    rotated = Affine(cosine, -sine, sine, cosine).then(current)
    rotated = Affine(rotated.a, rotated.b, rotated.c, rotated.d,
                     world_center[0] - rotated.a * center_x - rotated.b * center_y,
                     world_center[1] - rotated.c * center_x - rotated.d * center_y)
    return update_node(component, node_id, x_mm=rotated.x, y_mm=rotated.y, transform=rotated.transform_model())


def resize_line_endpoint(component: ComponentDefinition, node_id: str, dx_mm: float, dy_mm: float) -> ComponentDefinition:
    item = locations(component).get(node_id)
    if item is None or not isinstance(item.node, LineNode):
        raise TreeOperationError("нужно выбрать линию")
    _ensure_unlocked_selection(component, (node_id,), "изменить размер")
    return update_node(component, node_id, dx_mm=dx_mm, dy_mm=dy_mm)


def remove_nodes(component: ComponentDefinition, node_ids: Iterable[str]) -> ComponentDefinition:
    selected, parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "удалить")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "удалить")
    remove = {item.node.id for item in selected}
    children = tuple(node for node in parent_nodes(component, parent_id) if node.id not in remove)
    return _replace_children(component, parent_id, children)


def duplicate_nodes(
    component: ComponentDefinition,
    node_ids: Iterable[str],
    new_id: Callable[[Node, str], str],
    new_default_name: Callable[[Node], str | None],
) -> ComponentDefinition:
    """Copy selected subtrees at the front of each original parent's layer list."""
    selected, _ = _selection(component, node_ids, same_parent=False)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "дублировать")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "дублировать")
    generated_ids = set(locations(component))
    clones = {item.node.id: _clone_node(item.node, generated_ids, new_id, new_default_name) for item in selected}
    return _insert_duplicates(component, selected, clones)


def _clone_node(node, generated_ids, new_id, new_default_name):
    name = new_default_name(node) or node.name
    node_id = new_id(node, name)
    if not node_id or node_id in generated_ids:
        raise TreeOperationError("для копии нужен новый уникальный ID")
    generated_ids.add(node_id)
    changes = {"id": node_id, "name": name}
    if isinstance(node, GroupNode):
        changes["children"] = tuple(_clone_node(child, generated_ids, new_id, new_default_name) for child in node.children)
    return type(node).model_validate(node.model_copy(update=changes).model_dump(mode="python"))


def _insert_duplicates(component, selected, clones):
    parents = dict.fromkeys(item.parent_id for item in selected)
    result = component
    for parent_id in parents:
        chosen = {item.node.id for item in selected if item.parent_id == parent_id}
        siblings = parent_nodes(result, parent_id)
        copies = tuple(clones[node.id] for node in siblings if node.id in chosen)
        result = _replace_children(result, parent_id, (*siblings, *copies))
    return _refresh_frames(result)


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
    selected, parent_id, nodes, ids = _group_selection(component, node_ids, group_id)
    ordered = [node for node in nodes if node.id in ids]
    group_x, group_y = ordered[0].x_mm, ordered[0].y_mm
    offset = Affine(1, 0, 0, 1, -group_x, -group_y)
    children = tuple(
        node.model_copy(update={
            "x_mm": offset.then(Affine.node(node)).x,
            "y_mm": offset.then(Affine.node(node)).y,
            "transform": offset.then(Affine.node(node)).transform_model(),
        })
        for node in ordered
    )
    group = GroupNode(id=group_id, type="group", name=name, x_mm=group_x, y_mm=group_y, children=children)
    top_index = max(index for index, node in enumerate(nodes) if node.id in ids)
    remaining = [node for node in nodes if node.id not in ids]
    insertion = sum(1 for index, node in enumerate(nodes) if index <= top_index and node.id not in ids)
    remaining.insert(insertion, group)
    return _refresh_frames(_replace_children(component, parent_id, tuple(remaining)))


def _group_selection(component: ComponentDefinition, node_ids: Iterable[str], group_id: str):
    if group_id in locations(component):
        raise TreeOperationError(f"ID {group_id!r} уже используется")
    selected, parent_id = _selection(component, node_ids)
    if len(selected) < 2:
        raise TreeOperationError("для группировки выберите не менее двух элементов")
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "сгруппировать")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "сгруппировать")
    return selected, parent_id, list(parent_nodes(component, parent_id)), {item.node.id for item in selected}


def ungroup_node(component: ComponentDefinition, group_id: str) -> ComponentDefinition:
    item = locations(component).get(group_id)
    if item is None or not isinstance(item.node, GroupNode):
        raise TreeOperationError("нужно выбрать одну группу")
    _ensure_unlocked_selection(component, (group_id,), "разгруппировать")
    _ensure_unprotected_subtrees(component, (group_id,), "разгруппировать")
    siblings = list(parent_nodes(component, item.parent_id))
    children = tuple(
        child.model_copy(update={
            "x_mm": item.node.transform.m11 * child.x_mm + item.node.transform.m12 * child.y_mm + item.node.x_mm,
            "y_mm": item.node.transform.m21 * child.x_mm + item.node.transform.m22 * child.y_mm + item.node.y_mm,
            "transform": Affine.node(item.node).then(Affine.node(child)).transform_model(),
        })
        for child in item.node.children
    )
    siblings[item.index:item.index + 1] = children
    return _refresh_frames(_replace_children(component, item.parent_id, tuple(siblings)))


def reparent_nodes(component: ComponentDefinition, node_ids: Iterable[str], new_parent_id: str | None) -> ComponentDefinition:
    selected, old_parent_id = _selection(component, node_ids)
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "перенести")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "перенести")
    index = locations(component)
    _validate_reparent_target(component, index, selected, new_parent_id)
    if old_parent_id == new_parent_id:
        return component
    return _move_to_parent(component, selected, old_parent_id, new_parent_id)


def _validate_reparent_target(component, index, selected, new_parent_id):
    if new_parent_id is not None and _is_effectively_locked(component, new_parent_id):
        raise TreeOperationError("нельзя переносить в заблокированную группу")
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


def _move_to_parent(component, selected, old_parent_id, new_parent_id):
    old_children = tuple(node for node in parent_nodes(component, old_parent_id) if node.id not in {item.node.id for item in selected})
    result = _replace_children(component, old_parent_id, old_children)
    refreshed = locations(result)
    parent_world = refreshed[new_parent_id].world if new_parent_id is not None else Affine()
    moved = []
    for item in sorted(selected, key=lambda candidate: candidate.index):
        local = parent_world.inverse().then(item.world)
        moved.append(item.node.model_copy(update={"x_mm": local.x, "y_mm": local.y, "transform": local.transform_model()}))
    return _refresh_frames(_replace_children(result, new_parent_id, (*parent_nodes(result, new_parent_id), *moved)))


def drop_nodes(
    component: ComponentDefinition,
    node_ids: Iterable[str],
    new_parent_id: str | None,
    target_id: str | None = None,
    *,
    before: bool = True,
) -> ComponentDefinition:
    """Reparent nodes and place them before/after a sibling in document order."""
    ids = tuple(dict.fromkeys(node_ids))
    if target_id in ids:
        return component
    result = reparent_nodes(component, ids, new_parent_id)
    return result if target_id is None else _order_dropped_nodes(result, ids, new_parent_id, target_id, before)


def _order_dropped_nodes(result, ids, new_parent_id, target_id, before):
    siblings = list(parent_nodes(result, new_parent_id))
    moved = [node for node in siblings if node.id in ids]
    if len(moved) != len(ids):
        raise TreeOperationError("не все элементы можно перенести в выбранный уровень")
    remaining = [node for node in siblings if node.id not in ids]
    position = next((i for i, node in enumerate(remaining) if node.id == target_id), None)
    if position is None:
        raise TreeOperationError("целевой элемент не найден на выбранном уровне")
    remaining[position + (not before):position + (not before)] = moved
    return _refresh_frames(_replace_children(result, new_parent_id, tuple(remaining)))


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
    result: set[str] = set()
    def visit(nodes: tuple[Node, ...], parent_locked: bool) -> None:
        for node in nodes:
            locked = parent_locked or node.locked
            if locked:
                result.add(node.id)
            if isinstance(node, GroupNode):
                visit(node.children, locked)
    visit(component.elements, False)
    return frozenset(result)


def _ensure_unlocked_selection(component: ComponentDefinition, node_ids: Iterable[str], action: str) -> None:
    locked = [node_id for node_id in node_ids if _is_effectively_locked(component, node_id)]
    if locked:
        raise TreeOperationError(f"нельзя {action}: элемент или его родительская группа заблокированы")


def _ensure_unprotected_subtrees(component: ComponentDefinition, node_ids: Iterable[str], action: str) -> None:
    index = locations(component)
    if any(contains_locked_node(index[node_id].node) for node_id in node_ids):
        raise TreeOperationError(f"нельзя {action}: в группе есть заблокированный элемент")


def contains_locked_node(node: Node) -> bool:
    return node.locked or isinstance(node, GroupNode) and any(
        contains_locked_node(child) for child in node.children
    )


def align_nodes(component: ComponentDefinition, node_ids: Iterable[str], edge: str) -> ComponentDefinition:
    if edge not in {"left", "right", "top", "bottom"}:
        raise ValueError("неизвестная сторона выравнивания")
    selected, _ = _selection(component, node_ids)
    if len(selected) < 2:
        raise TreeOperationError("для выравнивания выберите не менее двух элементов")
    _ensure_unlocked_selection(component, (item.node.id for item in selected), "выровнять")
    _ensure_unprotected_subtrees(component, (item.node.id for item in selected), "выровнять")
    bounds = {item.node.id: node_bounds(component, item.node.id) for item in selected}
    target = _alignment_target(bounds.values(), edge)
    result = component
    for item in selected:
        dx, dy = _alignment_delta(bounds[item.node.id], edge, target)
        result = _move_aligned_node(result, item.node.id, dx, dy)
    return result


def _alignment_target(bounds, edge):
    index = 0 if edge == "left" else 1
    values = (bound[index] for bound in bounds)
    if edge in {"left", "top"}:
        return min(values)
    return max(bound[0] + bound[2] if edge == "right" else bound[1] + bound[3] for bound in bounds)


def _alignment_delta(bounds, edge, target):
    x, y, width, height = bounds
    if edge == "left":
        return target - x, 0.0
    if edge == "right":
        return target - (x + width), 0.0
    if edge == "top":
        return 0.0, target - y
    return 0.0, target - (y + height)


def _move_aligned_node(component, node_id, dx, dy):
    if not (dx or dy):
        return component
    current = locations(component)[node_id].node
    return update_node(component, node_id, x_mm=current.x_mm + dx, y_mm=current.y_mm + dy)


def node_bounds(component: ComponentDefinition, node_id: str) -> tuple[float, float, float, float]:
    index = locations(component)
    item = index[node_id]
    node = item.node
    left, top, width, height = local_bounds(node)
    points = (item.world.point(left, top), item.world.point(left + width, top),
              item.world.point(left, top + height), item.world.point(left + width, top + height))
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
