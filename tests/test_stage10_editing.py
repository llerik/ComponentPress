"""Behavior checks for the schema-four editing and formatting stage."""

from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QFocusEvent, QImage, QKeyEvent, QPainter, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from componentpress.application.edits import ensure_visual_edit_respects_locks
from componentpress.bootstrap import preview_service, project_repository, project_service
from componentpress.data_sources import XlsxReader
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.domain.tree import (
    TreeOperationError,
    align_node_to_component,
    align_nodes,
    locations,
    move_nodes,
    node_bounds,
    place_nodes,
    remove_nodes,
    update_node,
)
from componentpress.editor.properties import ElementProperties
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.yaml_codec import parse_component
from componentpress.rendering.exporter import ensure_gui_application


ROOT = Path(__file__).resolve().parents[1]


def _assert_red_text_is_right_aligned(image: QImage, node: HtmlNode, component: ComponentDefinition) -> None:
    scale_x = image.width() / component.size_mm.width
    scale_y = image.height() / component.size_mm.height
    left = round(node.x_mm * scale_x)
    top = round(node.y_mm * scale_y)
    right = round((node.x_mm + node.width_mm) * scale_x)
    bottom = round((node.y_mm + node.height_mm) * scale_y)
    red = [
        (x, y) for y in range(top, bottom) for x in range(left, right)
        if (color := image.pixelColor(x, y)).red() > color.green() + 30
        and color.red() > color.blue() + 30
    ]
    assert red
    assert min(x for x, _y in red) > left + 20


def _editor(tmp_path: Path, qtbot, monkeypatch):
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    service = project_service()
    window = MainWindow(ProjectController(service))
    monkeypatch.setattr(window, "_ask_unsaved_choice", lambda *_args: QMessageBox.StandardButton.Discard)
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    tab = window.open_component("forest-card")
    assert tab is not None
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    return window, root, tab


@pytest.mark.parametrize(
    ("key", "modifiers", "dx", "dy"),
    [
        (Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier, -0.1, 0.0),
        (Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier, 0.1, 0.0),
        (Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, 0.0, -0.1),
        (Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier, 0.0, 0.1),
        (Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier, 1.0, 0.0),
    ],
)
def test_arrow_nudge_commits_one_undoable_move_and_saves(
    tmp_path: Path, qtbot, monkeypatch, key, modifiers, dx: float, dy: float,
) -> None:
    window, root, tab = _editor(tmp_path, qtbot, monkeypatch)
    window._set_selection(("heading",), source="tree")
    before = locations(window.session.documents["forest-card"].model)["heading"]
    history_count = tab.undo_stack.count()

    QTest.keyPress(tab.canvas, key, modifiers)
    QTest.keyRelease(tab.canvas, key, modifiers)
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)

    after = locations(window.session.documents["forest-card"].model)["heading"]
    assert (after.node.x_mm, after.node.y_mm) == pytest.approx((before.node.x_mm + dx, before.node.y_mm + dy))
    assert tab.undo_stack.count() == history_count + 1
    assert tab.canvas.selected_ids == ("heading",)
    overlay = tab.canvas._overlays["heading"].sceneBoundingRect()
    component = window.session.documents["forest-card"].model
    expected_bounds = node_bounds(component, "heading")
    assert overlay.x() == pytest.approx(expected_bounds[0] * 96 / 25.4)
    assert overlay.y() == pytest.approx(expected_bounds[1] * 96 / 25.4)

    window.undo_group.undo()
    assert locations(window.session.documents["forest-card"].model)["heading"].node == before.node
    window.undo_group.redo()
    assert locations(window.session.documents["forest-card"].model)["heading"].node == after.node
    assert window.save_active()
    reopened = FileProjectRepository().open(root)
    assert locations(reopened.documents["forest-card"].model)["heading"].node == after.node


@pytest.mark.parametrize("node_type", ["image", "html"])
def test_drag_moves_image_and_html_in_one_undoable_edit(tmp_path, qtbot, monkeypatch, node_type):
    window, _root, tab = _editor(tmp_path, qtbot, monkeypatch)
    assert window.add_component("drag-test", "Drag test")
    tab = window.active_tab
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    if node_type == "image":
        assert window.add_image("assets/images/leaf.png", 10, 10, width_mm=8, height_mm=6)
        node_id = tab.canvas.selected_ids[0]
    else:
        assert window.add_html("<p>Text</p>", 10, 10, name="Text")
        node_id = tab.canvas.selected_ids[0]
    component = window.session.documents[tab.component_id].model
    before = node_bounds(component, node_id)
    history_count = tab.undo_stack.count()
    start = tab.canvas.mapFromScene(tab.canvas._overlays[node_id].sceneBoundingRect().center())
    end = start + QPoint(24, 13)

    qtbot.mousePress(tab.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    assert type(tab.canvas._gesture).__name__ == "MoveGesture"
    qtbot.mouseMove(tab.canvas.viewport(), pos=end)
    qtbot.mouseRelease(tab.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)

    after = node_bounds(window.session.documents[tab.component_id].model, node_id)
    assert after[:2] != pytest.approx(before[:2])
    assert after[2:] == pytest.approx(before[2:])
    assert tab.undo_stack.count() == history_count + 1


def test_arrow_hold_batches_repeats_and_blocked_group_warning_cannot_reenter(
    tmp_path: Path, qtbot, monkeypatch,
) -> None:
    window, _root, tab = _editor(tmp_path, qtbot, monkeypatch)
    window._set_selection(("heading", "caption"), source="tree")
    before = locations(window.session.documents["forest-card"].model)
    history_count = tab.undo_stack.count()

    QTest.keyPress(tab.canvas, Qt.Key.Key_Right)
    for _ in range(3):
        event = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier,
            "", True, 1,
        )
        QApplication.sendEvent(tab.canvas, event)
    QTest.keyRelease(tab.canvas, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)

    moved = locations(window.session.documents["forest-card"].model)
    assert moved["heading"].global_x == pytest.approx(before["heading"].global_x + 0.4)
    assert moved["caption"].global_x == pytest.approx(before["caption"].global_x + 0.4)
    assert tab.undo_stack.count() == history_count + 1
    window.undo_group.undo()
    assert locations(window.session.documents["forest-card"].model)["heading"].global_x == before["heading"].global_x

    assert window._apply_tree("Блокировка потомка", update_node, "caption", selected=("caption",), locked=True)
    group_before = locations(window.session.documents["forest-card"].model)["text-group"].node
    locked_model = window.session.documents["forest-card"].model
    window._set_selection(("caption",), source="tree")
    history_count = tab.undo_stack.count()
    QTest.keyPress(tab.canvas, Qt.Key.Key_Right)
    QTest.keyRelease(tab.canvas, Qt.Key.Key_Right)
    assert window.session.documents["forest-card"].model == locked_model
    assert tab.undo_stack.count() == history_count

    group_overlay_before = tab.canvas._overlays["text-group"].sceneBoundingRect()
    window._set_selection(("text-group",), source="tree")
    history_count = tab.undo_stack.count()
    warnings = []

    def reject_and_lose_focus(*_args) -> None:
        warnings.append(True)
        event = QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason)
        QApplication.sendEvent(tab.canvas, event)

    monkeypatch.setattr(QMessageBox, "warning", reject_and_lose_focus)
    QTest.keyPress(tab.canvas, Qt.Key.Key_Right)
    QTest.keyRelease(tab.canvas, Qt.Key.Key_Right)

    assert len(warnings) == 1
    assert tab.undo_stack.count() == history_count
    assert window.session.documents["forest-card"].model == locked_model
    assert locations(window.session.documents["forest-card"].model)["text-group"].node == group_before
    group_overlay_after = tab.canvas._overlays["text-group"].sceneBoundingRect()
    assert group_overlay_after.x() == pytest.approx(group_overlay_before.x())


def test_switching_tabs_cancels_an_uncommitted_arrow_nudge(tmp_path: Path, qtbot, monkeypatch) -> None:
    window, _root, tab = _editor(tmp_path, qtbot, monkeypatch)
    before = window.session.documents["forest-card"].model
    history_count = tab.undo_stack.count()
    assert window.add_component("second", "Вторая карта")
    second = window.active_tab
    assert second is not None
    QTest.keyPress(tab.canvas, Qt.Key.Key_Right)

    window.tabs.setCurrentWidget(second)

    assert window.session.documents["forest-card"].model == before
    assert tab.undo_stack.count() == history_count
    assert not tab.canvas.gesture_active


def test_focus_loss_commits_one_pending_arrow_nudge(tmp_path: Path, qtbot, monkeypatch) -> None:
    window, _root, tab = _editor(tmp_path, qtbot, monkeypatch)
    window._set_selection(("heading",), source="tree")
    before = locations(window.session.documents["forest-card"].model)["heading"].node
    history_count = tab.undo_stack.count()
    QTest.keyPress(tab.canvas, Qt.Key.Key_Right)
    QApplication.sendEvent(tab.canvas, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason))

    after = locations(window.session.documents["forest-card"].model)["heading"].node
    assert after.x_mm == pytest.approx(before.x_mm + 0.1)
    assert tab.undo_stack.count() == history_count + 1
    assert not tab.canvas.gesture_active


def _component() -> ComponentDefinition:
    return ComponentDefinition(
        schema_version=8,
        id="card",
        name="Card",
        size_mm=SizeMM(width=63, height=88),
        elements=(
            ImageNode(id="back", name="Back", type="image", x_mm=2, y_mm=3, width_mm=10, height_mm=8, source="assets/images/leaf.png"),
            ImageNode(id="front", name="Front", type="image", x_mm=20, y_mm=15, width_mm=5, height_mm=12, source="assets/images/leaf.png"),
            GroupNode(
                id="group", name="Group", type="group", x_mm=40, y_mm=35,
                children=(ImageNode(id="child", name="Child", type="image", x_mm=3, y_mm=4, width_mm=8, height_mm=5, source="assets/images/leaf.png"),),
            ),
        ),
    )


def test_project_and_new_component_templates_use_shared_schema_eight(tmp_path: Path) -> None:
    repository = project_repository()
    snapshot = repository.create(tmp_path / "game", "Game")
    assert snapshot.model.schema_version == 8
    snapshot = repository.add_component(snapshot, "card", "Card")
    assert snapshot.documents["card"].model.schema_version == 8
    assert snapshot.model.schema_version == snapshot.documents["card"].model.schema_version == SCHEMA_VERSION == 8
    assert "schema_version: 8" in snapshot.documents["card"].text


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, "5", "true", "5.0", 5.0])
def test_old_or_mistyped_project_schema_is_rejected_without_rewriting(tmp_path: Path, version: object) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    project = root / "project.cpress"
    source = project.read_text(encoding="utf-8")
    scalar = repr(version) if isinstance(version, str) else str(version)
    if version == "true":
        scalar = "true"
    scalar = "true" if version == "true" else scalar
    project.write_text(source.replace("schema_version: 8", f"schema_version: {scalar}", 1), encoding="utf-8")
    original = project.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert project.read_bytes() == original


def test_old_component_schema_is_rejected_without_rewriting(tmp_path: Path) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    component = root / "components" / "forest-card.yaml"
    component.write_text(component.read_text(encoding="utf-8").replace("schema_version: 8", "schema_version: 4", 1), encoding="utf-8")
    original = component.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert component.read_bytes() == original


@pytest.mark.parametrize("model_type", [ProjectDefinition, ComponentDefinition])
@pytest.mark.parametrize("value", [6, 6.0, "7", "6", True, False, 4, 5])
def test_document_models_require_schema_version_to_be_strict_integer_seven(model_type, value) -> None:
    document = {"schema_version": value, "id": "card", "name": "Card"}
    if model_type is ProjectDefinition:
        document["version"] = "0.1.0"
    else:
        document["size_mm"] = {"width": 63, "height": 88}
    with pytest.raises(ValidationError):
        model_type.model_validate(document)


@pytest.mark.parametrize("markup", [
    '<p style="">text</p>',
    '<p style>text</p>',
    '<p STYLE="color:red">text</p>',
    '<style>p { color: red }</style>',
    '<link rel="stylesheet" href="theme.css" />',
])
def test_css_is_rejected_from_manual_component_html(tmp_path: Path, markup: str) -> None:
    owner = tmp_path / "card.yaml"
    text = f'''schema_version: 8
id: card
name: Card
size_mm: {{width: 63, height: 88}}
elements:
  - id: heading
    name: Heading
    type: html
    x_mm: 0
    y_mm: 0
    width_mm: 40
    height_mm: 10
    font_family: Arial
    font_size_pt: 10
    content:
      mode: manual
      html: {markup!r}
'''
    with pytest.raises(ProjectError) as caught:
        parse_component(text, owner)
    assert caught.value.diagnostic.code == "HTML_UNSUPPORTED"
    assert caught.value.diagnostic.field == "elements.heading.content.html"


def test_html_semantics_remain_while_literal_markup_stays_text() -> None:
    from componentpress.bindings.html_policy import validate_html

    owner = Path("card.yaml")
    validate_html('<p align="center"><b>Bold</b> &lt;p style="color:red"&gt;</p><table border="1" bgcolor="#FFFFFF"><tr><td>Cell</td></tr></table>', owner, "content.html")


def test_excel_html_css_fails_full_sheet_validation_in_zero_copy_row(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-excel-game", root)
    workbook_path = root / "data" / "game.xlsx"
    workbook = load_workbook(workbook_path)
    sheet = workbook["Карты"]
    sheet["I3"] = 0
    sheet["J3"] = 0
    sheet["K3"] = '<p style="color:red">CSS</p>'
    workbook.save(workbook_path)
    workbook.close()
    snapshot = project_repository().open(root)
    diagnostics = preview_service(XlsxReader()).validate_all(snapshot)
    assert any(item.code == "HTML_UNSUPPORTED" and item.cell == "K3" for item in diagnostics)


@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_geometry_alignment_uses_node_and_group_bounds(edge: str) -> None:
    component = _component()
    bounds = {node_id: node_bounds(component, node_id) for node_id in ("back", "front", "group")}
    aligned = align_nodes(component, ("back", "front", "group"), edge)
    after = {node_id: node_bounds(aligned, node_id) for node_id in bounds}
    if edge == "left":
        assert len({round(rect[0], 6) for rect in after.values()}) == 1
    elif edge == "right":
        assert len({round(rect[0] + rect[2], 6) for rect in after.values()}) == 1
    elif edge == "top":
        assert len({round(rect[1], 6) for rect in after.values()}) == 1
    else:
        assert len({round(rect[1] + rect[3], 6) for rect in after.values()}) == 1
    assert all(after[node_id][2:] == bounds[node_id][2:] for node_id in bounds)
    assert [node.id for node in aligned.elements] == [node.id for node in component.elements]


@pytest.mark.parametrize("horizontal,vertical", [
    (horizontal, vertical)
    for horizontal in ("left", "center", "right")
    for vertical in ("top", "center", "bottom")
])
def test_single_node_position_uses_component_bounds_and_preserves_size(horizontal, vertical):
    node = ImageNode(
        id="placed", name="Placed", type="image", x_mm=17, y_mm=19, width_mm=11, height_mm=7,
        source="assets/images/leaf.png", transform={"m11": 1.2, "m12": 0.2, "m21": 0.1, "m22": 1.1},
    )
    parent = GroupNode(
        id="parent", name="Parent", type="group", x_mm=8, y_mm=10,
        transform={"m11": 1.1, "m12": 0.1, "m21": 0.2, "m22": 1.2}, children=(node,),
    )
    component = _component().model_copy(update={"elements": (parent,)})
    before = node_bounds(component, node.id)

    positioned = align_node_to_component(component, node.id, horizontal, vertical)
    left, top, width, height = node_bounds(positioned, node.id)

    expected_left = {"left": 0, "center": (component.size_mm.width - width) / 2,
                     "right": component.size_mm.width - width}[horizontal]
    expected_top = {"top": 0, "center": (component.size_mm.height - height) / 2,
                    "bottom": component.size_mm.height - height}[vertical]
    assert (left, top) == pytest.approx((expected_left, expected_top))
    assert (width, height) == pytest.approx(before[2:])


def test_single_axis_centering_preserves_other_world_axis_and_locked_nodes_are_rejected():
    component = _component()
    before = node_bounds(component, "back")
    centered = align_node_to_component(component, "back", "center", None)
    after = node_bounds(centered, "back")
    assert (after[0], after[1] + after[3] / 2) == pytest.approx(
        ((component.size_mm.width - after[2]) / 2, before[1] + before[3] / 2)
    )
    locked = component.model_copy(update={"elements": (component.elements[0].model_copy(update={"locked": True}),)})
    with pytest.raises(TreeOperationError):
        align_node_to_component(locked, "back", "center", "center")


def test_multi_alignment_applies_world_offsets_inside_transformed_group():
    children = (
        ImageNode(id="first", name="First", type="image", x_mm=3, y_mm=4, width_mm=8, height_mm=5,
                  source="assets/images/leaf.png"),
        ImageNode(id="second", name="Second", type="image", x_mm=18, y_mm=20, width_mm=4, height_mm=9,
                  source="assets/images/leaf.png"),
    )
    group = GroupNode(id="parent", name="Parent", type="group", x_mm=7, y_mm=12,
                      transform={"m11": 1.2, "m12": 0.3, "m21": 0.2, "m22": 1.1}, children=children)
    component = _component().model_copy(update={"elements": (group,)})
    before = {node.id: node_bounds(component, node.id) for node in children}

    aligned = align_nodes(component, ("first", "second"), "right")
    after = {node.id: node_bounds(aligned, node.id) for node in children}

    assert after["first"][0] + after["first"][2] == pytest.approx(after["second"][0] + after["second"][2])
    assert all(after[node_id][1] == pytest.approx(before[node_id][1]) for node_id in before)


def test_aligning_an_already_aligned_selection_is_a_no_op():
    component = _component()
    aligned = align_nodes(component, ("back", "front"), "left")
    assert align_nodes(aligned, ("back", "front"), "left") == aligned


def test_initial_image_size_limit_preserves_ratio_and_rejects_unrepresentable_minimum():
    component = ComponentDefinition(schema_version=8, id="wide", name="Wide", size_mm=SizeMM(width=100, height=200))
    assert MainWindow._initial_image_size(component, 0, 0, 0.02, 0.01) == pytest.approx((0.02, 0.01))
    assert MainWindow._initial_image_size(component, 0, 0, 140, 70) == pytest.approx((75, 37.5))
    assert MainWindow._initial_image_size(component, 0, 0, 1_000_000, 1) is None
    tiny = component.model_copy(update={"size_mm": SizeMM(width=0.001, height=0.001)})
    assert MainWindow._initial_image_size(tiny, 0, 0, 1, 1) is None


def test_lock_inheritance_guards_visual_edits_but_allows_sibling_reordering() -> None:
    component = _component().model_copy(update={
        "elements": (
            ImageNode(id="free", name="Free", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/leaf.png"),
            ImageNode(id="locked", name="Locked", type="image", x_mm=4, y_mm=1, width_mm=2, height_mm=2, source="assets/images/leaf.png", locked=True),
            GroupNode(id="parent", name="Parent", type="group", x_mm=10, y_mm=10, locked=True, children=(
                ImageNode(id="inherited", name="Inherited", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/leaf.png"),
            )),
        ),
    })
    with pytest.raises(TreeOperationError):
        move_nodes(component, ("locked",), 1, 0)
    with pytest.raises(TreeOperationError):
        update_node(component, "inherited", name="Changed")
    with pytest.raises(TreeOperationError):
        remove_nodes(component, ("parent",))
    reordered = place_nodes(component, ("free",), "locked")
    assert [node.id for node in reordered.elements[:2]] == ["free", "locked"]
    unlocked = update_node(component, "parent", locked=False)
    assert not unlocked.elements[2].locked
    assert not unlocked.elements[2].children[0].locked


def test_font_picker_preserves_unknown_saved_family_without_emitting_a_change(qtbot) -> None:
    ensure_gui_application()
    node = HtmlNode(
        id="html", name="Text", type="html", x_mm=0, y_mm=0, width_mm=40, height_mm=10,
        font_family="Unavailable ComponentPress Test Family", font_size_pt=10,
        html="text",
    )
    properties = ElementProperties()
    qtbot.addWidget(properties)
    changed: list[dict] = []
    properties.nodeChanged.connect(changed.append)
    properties.show_node(node)
    assert "Unavailable ComponentPress Test Family" in properties.font_combo.currentText()
    assert not changed


def test_component_position_controls_have_all_icons_and_axis_actions(qtbot):
    ensure_gui_application()
    component = _component()
    properties = ElementProperties()
    qtbot.addWidget(properties)
    requests = []
    properties.positionRequested.connect(requests.append)
    properties.show_node(component.elements[0], component=component)

    assert len(properties.position_buttons) == 9
    assert all(not button.icon().isNull() for button in properties.position_buttons.values())
    properties.position_buttons["center"].click()
    properties.center_horizontal_button.click()
    properties.center_vertical_button.click()
    assert requests == [("center", "center"), ("center", None), (None, "center")]


def test_visual_lock_guard_allows_layer_crossing_and_preserves_yaml_edit_path() -> None:
    component = ComponentDefinition(
        schema_version=8, id="card", name="Card", size_mm=SizeMM(width=63, height=88),
        elements=(
            ImageNode(id="locked", name="Locked", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/leaf.png", locked=True),
            ImageNode(id="free", name="Free", type="image", x_mm=4, y_mm=1, width_mm=2, height_mm=2, source="assets/images/leaf.png"),
        ),
    )
    crossed = place_nodes(component, ("free",), "locked")
    ensure_visual_edit_respects_locks(component, crossed)
    moved_locked = component.model_copy(update={
        "elements": (component.elements[0].model_copy(update={"x_mm": 9}), component.elements[1]),
    })
    with pytest.raises(ProjectError) as caught:
        ensure_visual_edit_respects_locks(component, moved_locked)
    assert caught.value.diagnostic.code == "NODE_LOCKED"

    yaml_text = '''schema_version: 8
id: card
name: Card
size_mm: {width: 63, height: 88}
elements:
  - id: locked
    name: Locked
    type: image
    x_mm: 9
    y_mm: 1
    width_mm: 2
    height_mm: 2
    locked: false
    source: {mode: manual, path: assets/images/leaf.png}
  - id: free
    name: Free
    type: image
    x_mm: 4
    y_mm: 1
    width_mm: 2
    height_mm: 2
    source: {mode: manual, path: assets/images/leaf.png}
'''
    edited, _ = parse_component(yaml_text, Path("card.yaml"))
    assert edited.elements[0].x_mm == 9 and not edited.elements[0].locked


def test_component_version_is_checked_before_html_normalization(tmp_path: Path) -> None:
    text = '''schema_version: 3
id: card
name: Card
size_mm: {width: 63, height: 88}
elements:
  - id: heading
    name: Heading
    type: html
    x_mm: 0
    y_mm: 0
    width_mm: 40
    height_mm: 10
    content: invalid
'''
    with pytest.raises(ProjectError) as caught:
        parse_component(text, tmp_path / "legacy.yaml")
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"


def test_text_alignment_is_default_and_explicit_html_alignment_is_preserved(tmp_path: Path) -> None:
    ensure_gui_application()
    from componentpress.rendering.html_document import prepare_html
    from componentpress.rendering.resources import ProjectResourceLoader

    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    node = HtmlNode(
        id="text", name="Text", type="html", x_mm=0, y_mm=0, width_mm=40, height_mm=10,
        font_family="Arial", font_size_pt=10, text_align="right", html="<p>Right</p>",
    )
    prepared = prepare_html(node, node.html, ProjectResourceLoader(root), root / "components/card.yaml")
    assert prepared.document.firstBlock().blockFormat().alignment() & Qt.AlignmentFlag.AlignRight
    aligned_html = '<p align="center">Centered explicitly</p>'
    prepared = prepare_html(node, aligned_html, ProjectResourceLoader(root), root / "components/card.yaml")
    alignment = prepared.document.firstBlock().blockFormat().alignment()
    assert alignment & Qt.AlignmentFlag.AlignHCenter
    assert not alignment & Qt.AlignmentFlag.AlignRight


@pytest.mark.ui
def test_text_defaults_reach_png_and_pdf_exports(tmp_path: Path) -> None:
    ensure_gui_application()
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument
    from componentpress.rendering import render_component

    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    snapshot = FileProjectRepository().open(root)
    component = snapshot.documents["forest-card"].model
    node = HtmlNode(
        id="alignment-regression", name="Text", type="html", x_mm=2, y_mm=70,
        width_mm=55, height_mm=12, font_family="Arial", font_size_pt=14,
        color="#FF0000", text_align="right", html="Plain text",
    )
    component = component.model_copy(update={"elements": (*component.elements, node)})
    png, pdf = tmp_path / "text.png", tmp_path / "text.pdf"
    render_component(snapshot, component.id, png, component=component, dpi=144, pdf=pdf)
    _assert_red_text_is_right_aligned(QImage(str(png)), node, component)

    reader = QPdfDocument()
    assert reader.load(str(pdf)) == QPdfDocument.Error.None_
    points = reader.pagePointSize(0)
    page = reader.render(0, QSize(round(points.width() * 2), round(points.height() * 2)))
    _assert_red_text_is_right_aligned(page, node, component)
    reader.close()


@pytest.mark.ui
@pytest.mark.parametrize("html", (
    "Plain text",
    "<b>Plain text</b>",
    "<span>Plain text</span>",
    "<p>Plain text</p>",
    "<ul><li>Plain text</li></ul>",
    "<table><tr><td>Plain text</td></tr></table>",
))
@pytest.mark.parametrize("color", ("#FF0000", "#80FF0000"))
def test_text_defaults_color_and_alignment_apply_to_rendered_html(tmp_path: Path, html: str, color: str) -> None:
    ensure_gui_application()
    from componentpress.rendering.html_document import prepare_html
    from componentpress.rendering.resources import ProjectResourceLoader

    root = tmp_path / "game"
    node = HtmlNode(
        id="text", name="Text", type="html", x_mm=0, y_mm=0, width_mm=60, height_mm=12,
        font_family="Arial", font_size_pt=14, color=color, text_align="right", html=html,
    )
    prepared = prepare_html(node, html, ProjectResourceLoader(root), root / "components/card.yaml")
    cursor = prepared.document.find("Plain text")
    assert not cursor.isNull()
    assert cursor.charFormat().foreground().color() == QColor(color)
    assert cursor.block().blockFormat().alignment() & Qt.AlignmentFlag.AlignRight

    image = QImage(int(prepared.width_px) + 1, int(prepared.height_px) + 1, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    prepared.draw(painter)
    painter.end()
    red_pixels = [
        (x, y) for y in range(image.height()) for x in range(image.width())
        if (pixel := image.pixelColor(x, y)).red() > pixel.green() + 30
        and pixel.red() > pixel.blue() + 30
    ]
    assert red_pixels
    if not html.startswith("<table"):
        assert min(x for x, _y in red_pixels) > 80
