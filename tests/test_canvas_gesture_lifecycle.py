import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage

from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.geometry import local_bounds
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, LineNode, ShapeNode
from componentpress.domain.tree import locations, resize_node
from componentpress.editor.canvas import ComponentCanvas


ANCHORS = tuple((x, y) for x in (0.0, 0.5, 1.0) for y in (0.0, 0.5, 1.0))


def _world_anchor(component, node_id, anchor):
    location = locations(component)[node_id]
    left, top, width, height = local_bounds(location.node)
    return location.world.point(left + width * anchor[0], top + height * anchor[1])


@pytest.mark.parametrize("anchor", ANCHORS)
def test_positioned_resize_keeps_world_anchor_with_shear(anchor):
    shape = ShapeNode(
        id="shape", name="Shape", type="rectangle", x_mm=10, y_mm=12,
        width_mm=8, height_mm=6,
        transform={"m11": 1.5, "m12": 0.4, "m21": 0.2, "m22": 1.2},
    )
    component = ComponentDefinition(
        schema_version=9, id="card", name="Card", size_mm=SizeMM(width=100, height=100),
        elements=(shape,),
    )

    resized = resize_node(component, "shape", 20, 14, anchor)

    assert _world_anchor(resized, "shape", anchor) == pytest.approx(_world_anchor(component, "shape", anchor))


@pytest.mark.parametrize("dx,dy", [(-10, 4), (10, -4), (-10, 0), (0, -10)])
@pytest.mark.parametrize("anchor", ANCHORS)
def test_line_resize_keeps_world_anchor_and_signed_direction(dx, dy, anchor):
    line = LineNode(
        id="line", name="Line", type="line", x_mm=10, y_mm=12,
        dx_mm=dx, dy_mm=dy,
        transform={"m11": 1.5, "m12": 0.4, "m21": 0.2, "m22": 1.2},
    )
    component = ComponentDefinition(
        schema_version=9, id="card", name="Card", size_mm=SizeMM(width=100, height=100),
        elements=(line,),
    )

    resized = resize_node(component, "line", 20, 8, anchor)
    result = locations(resized)["line"].node

    assert _world_anchor(resized, "line", anchor) == pytest.approx(_world_anchor(component, "line", anchor))
    assert result.dx_mm * dx >= 0
    assert result.dy_mm * dy >= 0 if dy else result.dy_mm >= 0
    assert abs(result.dx_mm) + abs(result.dy_mm) > 0


@pytest.mark.parametrize("anchor", ANCHORS)
def test_group_resize_keeps_world_anchor_with_shear(anchor):
    shape = ShapeNode(
        id="shape", name="Shape", type="rectangle", x_mm=1, y_mm=2,
        width_mm=8, height_mm=6,
    )
    group = GroupNode(
        id="group", name="Group", type="group", x_mm=10, y_mm=12,
        frame={"x_mm": 1, "y_mm": 2, "width_mm": 8, "height_mm": 6},
        transform={"m11": 1.5, "m12": 0.4, "m21": 0.2, "m22": 1.2},
        children=(shape,),
    )
    component = ComponentDefinition(
        schema_version=9, id="card", name="Card", size_mm=SizeMM(width=100, height=100),
        elements=(group,),
    )

    resized = resize_node(component, "group", 20, 14, anchor)

    assert _world_anchor(resized, "group", anchor) == pytest.approx(_world_anchor(component, "group", anchor))


def _canvas(qtbot, node=None):
    node = node or ShapeNode(
        id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20, width_mm=20, height_mm=20,
    )
    component = ComponentDefinition(
        schema_version=9, id="card", name="Card", size_mm=SizeMM(width=100, height=100),
        elements=(node,),
    )
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    image = QImage(1000, 1000, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    canvas.set_document(image, component, selected=(node.id,))
    return canvas, component


@pytest.mark.parametrize("node", [
    ImageNode(id="image", name="Image", type="image", x_mm=20, y_mm=20, width_mm=20, height_mm=20, source="assets/image.png"),
    HtmlNode(id="html", name="HTML", type="html", x_mm=20, y_mm=20, width_mm=20, height_mm=20,
             font_family="Arial", font_size_pt=10, html="Text"),
], ids=("image", "html"))
def test_drag_emits_move_request_with_document_context(qtbot, node):
    canvas, component = _canvas(qtbot, node)
    token = ("document", component.id)
    canvas.set_component_model(component, (node.id,), context=token)
    requests = []
    canvas.moveRequested.connect(lambda *args: requests.append((args, canvas.dispatch_context)))
    start = canvas.mapFromScene(canvas._overlays[node.id].sceneBoundingRect().center())
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=start + QPoint(15, 0))
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(15, 0))

    assert len(requests) == 1
    assert len(requests[0][0]) == 3
    assert requests[0][0][0] == (node.id,)
    assert requests[0][1] == token


def _start_move(qtbot, canvas):
    center = canvas._overlays["shape"].sceneBoundingRect().center().toPoint()
    start = canvas.mapFromScene(center)
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=start + QPoint(15, 0))
    assert canvas.gesture_active
    return start + QPoint(15, 0)


def test_external_model_revision_cancels_uncommitted_move(qtbot):
    canvas, component = _canvas(qtbot)
    requests = []
    canvas.moveRequested.connect(lambda *args: requests.append(args))
    release_at = _start_move(qtbot, canvas)
    changed = component.model_copy(update={
        "elements": (component.elements[0].model_copy(update={"locked": True}),),
    })

    canvas.set_component_model(changed, ("shape",))
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=release_at)

    assert requests == []
    assert canvas._component == changed
    assert not canvas.gesture_active


def test_escape_cancels_move_and_restores_overlay(qtbot):
    canvas, _component = _canvas(qtbot)
    requests = []
    canvas.moveRequested.connect(lambda *args: requests.append(args))
    original = canvas._overlays["shape"].pos()
    release_at = _start_move(qtbot, canvas)

    qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=release_at)

    assert requests == []
    assert canvas._overlays["shape"].pos() == original
    assert not canvas.gesture_active


def test_escape_cancels_draw_preview(qtbot):
    canvas, _component = _canvas(qtbot)
    requests = []
    canvas.drawRequested.connect(lambda *args: requests.append(args))
    canvas.set_tool("rectangle")
    start = canvas.mapFromScene(canvas._pixmap_item.sceneBoundingRect().topLeft() + QPoint(30, 30))
    end = start + QPoint(40, 40)
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=end)
    assert canvas.gesture_active

    qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)

    assert requests == []
    assert canvas.active_tool is None
    assert not canvas.gesture_active


def test_releasing_preview_cancels_uncommitted_move_and_late_release(qtbot):
    canvas, _component = _canvas(qtbot)
    requests = []
    canvas.moveRequested.connect(lambda *args: requests.append(args))
    release_at = _start_move(qtbot, canvas)

    canvas.release_preview()
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=release_at)

    assert requests == []
    assert not canvas.gesture_active
