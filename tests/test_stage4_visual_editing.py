"""Behaviour checks for stage-four visual editing."""

from pathlib import Path
import shutil

from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent, QImage
from PySide6.QtWidgets import QAbstractItemView, QMessageBox

from componentpress.bootstrap import project_service
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode
from componentpress.domain.tree import (
    group_nodes,
    locations,
    move_nodes,
    place_nodes,
    reparent_nodes,
    ungroup_node,
)
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.editor.qt_models import ID_ROLE
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.yaml_codec import dump_yaml, parse_component, update_tree
from componentpress.rendering import render_component


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def simple_component() -> ComponentDefinition:
    return ComponentDefinition(
        schema_version=2,
        id="card",
        name="Карта",
        size_mm=SizeMM(width=63, height=88),
        elements=(
            ImageNode(id="back", name="Фон", type="image", x_mm=5, y_mm=6, width_mm=20, height_mm=20, source="assets/images/a.png"),
            HtmlNode(id="front", name="Текст", type="html", x_mm=10, y_mm=12, width_mm=30, height_mm=10, font_family="Arial", font_size_pt=10, html="<p>Текст</p>"),
            HtmlNode(id="other", name="Другой", type="html", x_mm=1, y_mm=2, width_mm=10, height_mm=5, font_family="Arial", font_size_pt=8, html="x"),
        ),
    )


def test_group_ungroup_move_and_reparent_preserve_global_geometry() -> None:
    component = simple_component()
    grouped = group_nodes(component, ("back", "front"), "pair", "Пара")
    index = locations(grouped)
    assert index["back"].global_x == 5
    assert index["back"].global_y == 6
    assert index["front"].global_x == 10
    assert index["front"].global_y == 12
    assert [node.id for node in grouped.elements] == ["pair", "other"]
    moved = move_nodes(grouped, ("pair",), 7, -2)
    moved_index = locations(moved)
    assert (moved_index["back"].global_x, moved_index["back"].global_y) == (12, 4)
    child_moved = move_nodes(moved, ("front",), 1, 3)
    assert locations(child_moved)["back"].global_x == 12
    assert locations(child_moved)["front"].global_x == 18
    root_again = reparent_nodes(child_moved, ("front",), None)
    assert locations(root_again)["front"].global_x == 18
    assert locations(root_again)["front"].global_y == 13
    ungrouped = ungroup_node(root_again, "pair")
    assert locations(ungrouped)["back"].global_x == 12
    assert all(not isinstance(node, GroupNode) for node in ungrouped.elements)


def test_layer_drop_changes_storage_order_used_by_painter() -> None:
    component = simple_component()
    changed = place_nodes(component, ("back",), "other")
    assert [node.id for node in changed.elements] == ["front", "back", "other"]


def test_grouping_keeps_comments_attached_to_moved_nodes(tmp_path: Path) -> None:
    path = tmp_path / "card.yaml"
    text = """schema_version: 2
id: card
name: Card
size_mm: {width: 63, height: 88}
elements:
  # comment for back
  - id: back
    name: Back
    type: image
    x_mm: 5
    y_mm: 6
    width_mm: 20
    height_mm: 20
    source: assets/images/a.png
  # comment for front
  - id: front
    name: Front
    type: html
    x_mm: 10
    y_mm: 12
    width_mm: 30
    height_mm: 10
    font_family: Arial
    font_size_pt: 10
    html: text
"""
    model, tree = parse_component(text, path)
    grouped = group_nodes(model, ("back", "front"), "pair", "Pair")
    update_tree(tree, grouped)
    saved = dump_yaml(tree)
    reparsed, _ = parse_component(saved, path)
    assert isinstance(reparsed.elements[0], GroupNode)
    assert "# comment for back" in saved
    assert "# comment for front" in saved


def editor(tmp_path: Path, qtbot) -> tuple[MainWindow, Path]:
    root = tmp_path / "Визуальный проект"
    shutil.copytree(EXAMPLE, root)
    service = project_service()
    window = MainWindow(ProjectController(service))
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    tab = window.open_component("forest-card")
    assert tab is not None
    return window, root


def test_gui_add_group_undo_redo_save_and_export(tmp_path: Path, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))
    window, root = editor(tmp_path, qtbot)
    assert window.add_html("<p>Новый текст</p>", 2, 3, node_id="new-title")
    assert window.add_image("assets/images/animals/enemy/leaf.png", 8, 9, node_id="new-art")
    assert window.session is not None and window.active_tab is not None
    model = window.session.documents["forest-card"].model
    assert locations(model)["new-title"].node.width_mm == 40
    assert window.active_tab.undo_stack.count() == 2

    window._set_selection(("new-title", "new-art"), source="tree")
    assert window.group_selected("new-group", "Новая группа")
    grouped = window.session.documents["forest-card"].model
    assert locations(grouped)["new-title"].global_x == 2
    assert window.active_tab.undo_stack.count() == 3
    assert window._index_for_id("new-group").isValid()
    window._set_selection(("new-group",), source="tree")
    group_before = locations(grouped)["new-group"].node
    window._move_selected(("new-group",), 3.0, 4.0)
    moved = window.session.documents["forest-card"].model
    group_after = locations(moved)["new-group"].node
    assert (group_after.x_mm, group_after.y_mm) == (group_before.x_mm + 3, group_before.y_mm + 4)
    group_overlay = window.active_tab.canvas._overlays["new-group"]
    title_overlay = window.active_tab.canvas._overlays["new-title"]
    assert window.active_tab.canvas._overlay_at(title_overlay.sceneBoundingRect().center()) is group_overlay
    window.undo_group.undo()
    assert locations(window.session.documents["forest-card"].model)["new-group"].node.x_mm == group_before.x_mm
    window.undo_group.undo()
    assert "new-group" not in locations(window.session.documents["forest-card"].model)
    window.undo_group.redo()
    window.undo_group.redo()
    assert "new-group" in locations(window.session.documents["forest-card"].model)

    assert window.save_active()
    reopened = FileProjectRepository().open(root)
    assert "new-group" in locations(reopened.documents["forest-card"].model)
    assert "# Статический макет с вложенной группой" in (root / "components/forest-card.yaml").read_text(encoding="utf-8")
    output = tmp_path / "visual.png"
    render_component(reopened, "forest-card", output, dpi=96)
    image = QImage(str(output))
    assert not image.isNull()
    # Export contains only the renderer output; editor overlays live in a separate scene layer.
    assert image.size().width() == 238


def test_tree_and_canvas_selection_stay_synchronised(tmp_path: Path, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))
    window, _root = editor(tmp_path, qtbot)
    heading = window._index_for_id("heading")
    window.element_tree.selectionModel().select(
        heading,
        window.element_tree.selectionModel().SelectionFlag.ClearAndSelect
        | window.element_tree.selectionModel().SelectionFlag.Rows,
    )
    assert window.active_tab is not None
    assert window.active_tab.canvas.selected_ids == ("heading",)
    window.active_tab.canvas.select_ids(("caption",))
    window.active_tab.canvas.selectionChanged.emit(("caption",))
    selected = [index.data(ID_ROLE) for index in window.element_tree.selectionModel().selectedRows(0)]
    assert selected == ["caption"]
    assert window.element_properties.html_edit.toPlainText().startswith("<p>")


def test_layer_tree_uses_non_destructive_drop_mode(tmp_path: Path, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))
    window, _root = editor(tmp_path, qtbot)
    assert window.element_tree.dragDropMode() == QAbstractItemView.DragDropMode.DragDrop
    assert window.element_tree.defaultDropAction() == Qt.DropAction.CopyAction
    before = set(locations(window.session.documents["forest-card"].model))
    source = window._index_for_id("leaf-art")
    target = window._index_for_id("text-group")
    window.element_tree.selectionModel().select(
        source,
        window.element_tree.selectionModel().SelectionFlag.ClearAndSelect
        | window.element_tree.selectionModel().SelectionFlag.Rows,
    )
    target_center = window.element_tree.visualRect(target).center()
    event = QDropEvent(
        QPointF(target_center),
        Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.element_tree.dropEvent(event)
    # The presentation model is never edited by Qt itself during the drop.
    assert window._index_for_id("leaf-art").isValid()
    qtbot.wait(1)
    after = set(locations(window.session.documents["forest-card"].model))
    assert after == before
    assert window._index_for_id("leaf-art").isValid()


def test_one_shot_tool_is_cancelled_by_escape_and_tab_switch(tmp_path: Path, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))
    window, _root = editor(tmp_path, qtbot)
    first = window.active_tab
    assert first is not None
    window._begin_tool("image")
    assert first.canvas.active_tool == "image"
    qtbot.keyClick(first.canvas, Qt.Key.Key_Escape)
    assert first.canvas.active_tool is None
    window._begin_tool("image")
    qtbot.mouseClick(window.element_tree.viewport(), Qt.MouseButton.LeftButton)
    assert first.canvas.active_tool is None
    assert window.add_component("second", "Вторая карта")
    second = window.active_tab
    assert second is not None
    window.tabs.setCurrentWidget(first)
    window._begin_tool("html")
    window.tabs.setCurrentWidget(second)
    assert first.canvas.active_tool is None
