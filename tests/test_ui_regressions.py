"""Regression scenarios for editor rendering, properties and dock behaviour."""

from pathlib import Path
from dataclasses import replace
import shutil

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import QColorDialog, QComboBox, QDialog, QMessageBox, QPushButton

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.bindings.resolver import BindingResolver
from componentpress.bootstrap import project_controller
from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, LineNode, ShapeNode
from componentpress.domain.project import DataSource, ProjectDefinition
from componentpress.domain.tree import locations
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot
from componentpress.editor.dialogs import HtmlAddDialog
from componentpress.editor.main_window import MainWindow
from componentpress.editor.properties import ElementProperties
from componentpress.editor.ui_state import UiState
from componentpress.project_io.repository import FileProjectRepository
from componentpress.rendering import render_component
from componentpress.rendering.exporter import ensure_gui_application, render_component_image
from componentpress.rendering.fonts import available_font_families


ROOT = Path(__file__).resolve().parents[1]


def text_node(**changes):
    values = dict(id="text", name="Text", type="html", x_mm=4, y_mm=4,
                  width_mm=20, height_mm=10, font_family="Arial", font_size_pt=10,
                  color="#000000", html="<p>MMMM</p>")
    return HtmlNode(**(values | changes))


def snapshot(tmp_path, elements):
    project = ProjectDefinition(schema_version=5, id="test", name="Test", version="0.1.0")
    component = ComponentDefinition(schema_version=5, id="card", name="Card",
                                    size_mm=SizeMM(width=30, height=20), elements=tuple(elements))
    return ProjectSnapshot(tmp_path, "", "", project,
                           {"card": DocumentSnapshot(tmp_path / "card.yaml", "", "", component)})


def ink_bounds(image):
    pixels = [(x, y) for y in range(image.height()) for x in range(image.width())
              if image.pixelColor(x, y).red() < 220]
    assert pixels, "rendered text has no visible pixels"
    xs, ys = zip(*pixels)
    ratio = image.devicePixelRatio()
    return tuple(value / ratio for value in (min(xs), min(ys), max(xs) + 1, max(ys) + 1))


@pytest.mark.parametrize("dpi", [48, 96, 192, 384])
def test_html_pixels_stay_in_frame_and_keep_logical_size_at_preview_dpi(tmp_path, dpi):
    ensure_gui_application()
    node = text_node()
    value = snapshot(tmp_path, [node])
    baseline = ink_bounds(render_component_image(value, "card", dpi=96))
    image = render_component_image(value, "card", dpi=dpi, preserve_logical_size=True)
    bounds = ink_bounds(image)
    assert bounds == pytest.approx(baseline, abs=2)
    x, y, right, bottom = bounds
    factor = 96 / 25.4
    assert x >= node.x_mm * factor - 1
    assert y >= node.y_mm * factor - 1
    assert right <= (node.x_mm + node.width_mm) * factor + 1
    assert bottom <= (node.y_mm + node.height_mm) * factor + 1
    assert value.documents["card"].model.elements[0].font_size_pt == 10


@pytest.mark.parametrize("changes", [{"height_mm": 0.1}, {"width_mm": 0.1}])
def test_html_overflow_still_blocks_export_without_publishing(tmp_path, changes):
    ensure_gui_application()
    value = snapshot(tmp_path, [text_node(**changes)])
    output = tmp_path / "overflow.png"
    with pytest.raises(ProjectError) as caught:
        render_component(value, "card", output, dpi=192)
    assert caught.value.diagnostic.code == "TEXT_OVERFLOW"
    assert not output.exists()


@pytest.mark.parametrize("nesting", ["root", "group", "conditional"])
def test_resolver_keeps_shapes_and_signed_line_visible_in_nested_trees(tmp_path, nesting):
    ensure_gui_application()
    shapes = (
        ShapeNode(id="rect", name="Rect", type="rectangle", x_mm=2, y_mm=2,
                  width_mm=6, height_mm=6, fill="#FF0000"),
        ShapeNode(id="ellipse", name="Ellipse", type="ellipse", x_mm=10, y_mm=2,
                  width_mm=6, height_mm=6, fill="#00FF00"),
        LineNode(id="line", name="Line", type="line", x_mm=25, y_mm=16,
                 dx_mm=-5, dy_mm=-5, stroke="#0000FF", stroke_width_mm=0.8),
    )
    value = snapshot(tmp_path, shapes)
    component = value.documents["card"].model
    data = row = None
    if nesting == "group":
        component = component.model_copy(update={"elements": (
            GroupNode(id="group", name="Group", type="group", x_mm=0, y_mm=0, children=shapes),)})
    elif nesting == "conditional":
        component = component.model_copy(update={"data": DataBinding(sheet="Sheet"), "elements": (
            ConditionalGroupNode(id="group", name="Group", type="conditional_group", x_mm=0, y_mm=0,
                                 children=shapes, condition_column="Kind", condition_value="yes"),)})
        project = value.model.model_copy(update={"data_source": DataSource(path="data.xlsx")})
        value = replace(value, model=project)
        row = DataRow(2, "row", 1, {"Kind": CellValue("yes", "A2")})
        data = DataSheetSnapshot("main", tmp_path / "data.xlsx", "Sheet", "hash", 1, ("Kind",), (row,))
    resolved = BindingResolver(tmp_path, value.model, validate_resources=False).resolve_component(component, data, row)
    index = locations(resolved.component)
    assert all(index[node.id].node == node for node in shapes)
    image = render_component_image(value, "card", component=resolved.component, dpi=192)
    factor = 192 / 25.4
    for x, y, expected in ((5, 5, "#FF0000"), (13, 5, "#00FF00"), (22.5, 13.5, "#0000FF")):
        color = image.pixelColor(round(x * factor), round(y * factor))
        assert color == QColor(expected)


@pytest.fixture
def make_editor(tmp_path, qtbot, monkeypatch):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Discard))

    def make(excel=False):
        root = tmp_path / ("excel" if excel else "static")
        shutil.copytree(ROOT / "examples" / ("demo-excel-game" if excel else "demo-game"), root,
                        ignore=shutil.ignore_patterns(".componentpress"))
        controller = project_controller()
        service = controller.service
        window = MainWindow(controller)
        window.ui_state = UiState(QSettings(str(tmp_path / (root.name + ".ini")), QSettings.Format.IniFormat))
        errors = []
        monkeypatch.setattr(window, "_show_error", errors.append)
        qtbot.addWidget(window)
        window.load_session(service.open_session(root))
        window.show()
        assert window.add_component("regression", "Regression")
        qtbot.waitUntil(lambda: window.active_tab.displayed_generation == window.active_tab.preview_generation, timeout=15000)
        return window, root, errors

    return make


def selected_node(window):
    model = window.session.documents[window.active_tab.component_id].model
    return locations(model)[window._selected_ids()[0]].node


def test_work_panel_width_limit_survives_float_restore_and_resize(make_editor, qtbot):
    window, _root, _errors = make_editor()
    maximum = window.screen().geometry().width() // 2
    assert window.right_dock.maximumWidth() == maximum
    window.right_dock.setFloating(True)
    window.right_dock.resize(maximum + 200, 500)
    qtbot.waitUntil(lambda: window.right_dock.width() <= maximum)
    window.ui_state.save(window)
    window.ui_state.restore(window)
    window.right_dock.setFloating(False)
    window.resize(900, 650)
    qtbot.wait(20)
    assert window.right_dock.maximumWidth() == maximum
    assert window.right_dock.width() <= maximum


def test_floating_work_panel_uses_its_own_screen_and_tracks_screen_changed(make_editor, qtbot, monkeypatch):
    window, _root, _errors = make_editor()
    dock = window.right_dock
    dock.setFloating(True)
    qtbot.waitUntil(lambda: window._right_dock_handle is dock.windowHandle())
    screen_width = [1000]

    class AlternateScreen:
        def geometry(self):
            return QRect(0, 0, screen_width[0], 800)

    monkeypatch.setattr(dock, "screen", lambda: AlternateScreen())
    dock.windowHandle().screenChanged.emit(window.screen())
    assert dock.maximumWidth() == 500
    screen_width[0] = 1400
    dock.windowHandle().screenChanged.emit(window.screen())
    assert dock.maximumWidth() == 700
    dock.setFloating(False)
    qtbot.waitUntil(lambda: window._right_dock_handle is None)
    assert dock.maximumWidth() == window.screen().geometry().width() // 2


def test_malformed_project_font_reports_error_and_cancels_html_placement(make_editor):
    window, root, errors = make_editor()
    font = root / "assets" / "fonts" / "broken.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"not a valid TrueType font")
    document = window.session.documents["regression"]
    before_model, before_text = document.model, document.draft_text
    count = window.active_tab.undo_stack.count()
    window._begin_tool("html")
    window._place_element("html", 2, 2)
    assert len(errors) == 1 and isinstance(errors[0], ProjectError)
    assert errors[0].diagnostic.code == "FONT_INVALID"
    assert window.active_tab.canvas.active_tool is None
    assert document.model == before_model and document.draft_text == before_text
    assert window.active_tab.undo_stack.count() == count


def test_selecting_html_keeps_malformed_font_diagnostic_visible(make_editor, qtbot):
    window, root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2, node_id="text")
    qtbot.waitUntil(lambda: window.active_tab.displayed_generation == window.active_tab.preview_generation, timeout=15000)
    font = root / "assets" / "fonts" / "broken.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"not a valid TrueType font")
    window._set_selection(("text",), source="tree")
    assert "FONT_INVALID" in window.statusBar().currentMessage()
    assert not errors


def test_html_creation_uses_font_list_and_palette_and_tenth_point_precision(make_editor, qtbot, monkeypatch):
    window, _root, errors = make_editor()
    chosen = QColor("#2468AC")
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: chosen))
    captured = {}

    def accept_dialog(dialog):
        font = dialog.findChild(QComboBox, "newHtmlFont")
        assert font is not None and not font.isEditable() and font.count() > 0
        captured["font"] = font.currentText()
        assert dialog.font_size.decimals() == 1
        assert dialog.font_size.singleStep() == pytest.approx(0.1)
        dialog.font_size.setValue(10.26)
        assert dialog.font_size.value() == pytest.approx(10.3)
        dialog.findChild(QPushButton, "newHtmlColorPicker").click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(HtmlAddDialog, "exec", accept_dialog)
    window._place_element("html", 2, 2)
    node = selected_node(window)
    assert node.font_family == captured["font"]
    assert node.font_size_pt == pytest.approx(10.3)
    assert QColor(node.color) == chosen
    assert not errors


def test_cancelled_html_palette_does_not_replace_existing_color(tmp_path, qtbot, monkeypatch):
    dialog = HtmlAddDialog(tmp_path, lambda *_: "")
    qtbot.addWidget(dialog)
    before = dialog.color.text()
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: QColor()))
    dialog.findChild(QPushButton, "newHtmlColorPicker").click()
    assert dialog.color.text() == before


def test_font_picker_registers_unused_project_font_and_releases_registration(tmp_path, qtbot, monkeypatch):
    font = Path("C:/Windows/Fonts/arial.ttf")
    if not font.is_file():
        pytest.skip("the local Windows Arial font fixture is unavailable")
    folder = tmp_path / "assets" / "fonts" / "unused"
    folder.mkdir(parents=True)
    shutil.copy2(font, folder / "unused.ttf")
    added, removed = [], []
    add_font = QFontDatabase.addApplicationFontFromData
    remove_font = QFontDatabase.removeApplicationFont

    def add(data):
        font_id = add_font(data)
        added.append(font_id)
        return font_id

    def remove(font_id):
        removed.append(font_id)
        return remove_font(font_id)

    monkeypatch.setattr(QFontDatabase, "addApplicationFontFromData", staticmethod(add))
    monkeypatch.setattr(QFontDatabase, "removeApplicationFont", staticmethod(remove))
    assert "Arial" in available_font_families(tmp_path)
    assert len(added) == 1 and added[0] >= 0 and removed == added
    dialog = HtmlAddDialog(tmp_path, lambda *_: "")
    qtbot.addWidget(dialog)
    assert dialog.findChild(QComboBox, "newHtmlFont").findText("Arial") >= 0
    assert len(added) == 2 and removed == added


def test_font_properties_use_tenth_points_without_editing_existing_document_on_selection(make_editor):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2, font_size_pt=10.26)
    before = window.session.documents["regression"].model
    count = window.active_tab.undo_stack.count()
    window._show_selection(window._selected_ids())
    assert window.session.documents["regression"].model == before
    assert window.active_tab.undo_stack.count() == count
    spin = window.element_properties.font_spin
    assert spin.decimals() == 1 and spin.singleStep() == pytest.approx(0.1)
    spin.setValue(11.26)
    spin.editingFinished.emit()
    assert selected_node(window).font_size_pt == pytest.approx(11.3)
    assert not errors


def test_component_sheet_selection_updates_yaml_preview_and_undo(make_editor, qtbot):
    window, root, errors = make_editor(excel=True)
    combo = window.findChild(QComboBox, "componentPropertySheet")
    assert combo is not None and not combo.isEditable()
    for sheet in ("Карты", "События"):
        index = combo.findData(sheet)
        assert index >= 0
        combo.setCurrentIndex(index)
        assert window.session.documents["regression"].model.data.sheet == sheet
        qtbot.waitUntil(lambda: window.active_tab.displayed_generation == window.active_tab.preview_generation, timeout=15000)
        component = window.session.documents["regression"].model
        assert window.controller.preview.cached_data(window.session.snapshot, "regression", component=component).sheet == sheet
    window.active_tab.undo_stack.undo()
    assert window.session.documents["regression"].model.data.sheet == "Карты"
    window.active_tab.undo_stack.redo()
    assert window.save_active()
    assert FileProjectRepository().open(root).documents["regression"].model.data.sheet == "События"
    assert not errors


def test_column_html_cannot_be_edited_after_selection_or_lock_state_refresh(qtbot):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    properties.show()
    node = text_node(content_mode="column", content_column="Text")
    properties.show_node(node, columns=("Text",))
    properties.set_edit_locked(False)
    original = properties.html_edit.toPlainText()
    assert properties.html_edit.isReadOnly() or not properties.html_edit.isEnabled()
    assert not properties.apply_html.isEnabled()
    assert not properties.insert_image.isEnabled()
    qtbot.keyClicks(properties.html_edit, "must not be inserted")
    assert properties.html_edit.toPlainText() == original
    properties.content_mode.setCurrentIndex(properties.content_mode.findData("manual"))
    assert properties.html_edit.isEnabled() and not properties.html_edit.isReadOnly()
    assert properties.apply_html.isEnabled() and properties.insert_image.isEnabled()
    properties.set_edit_locked(True)
    assert not properties.html_edit.isEnabled()
    properties.set_edit_locked(False)
    assert properties.html_edit.isEnabled() and not properties.html_edit.isReadOnly()


def test_layer_buttons_match_tool_dimensions(make_editor):
    window, _root, _errors = make_editor()
    buttons = (window.layer_up, window.layer_down, window.group_button, window.ungroup_button, window.reparent_button)
    assert all(button.size() == window.select_tool.size() == QSize(34, 34) for button in buttons)
    assert all(not button.icon().isNull() and button.accessibleName() for button in buttons)


def test_zoom_refresh_keeps_text_and_overlay_proportions_without_document_change(make_editor, qtbot):
    window, _root, errors = make_editor()
    assert window.add_html("<p>MMMM</p>", 4, 4)
    tab = window.active_tab
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    before = window.session.documents["regression"].model
    before_text = window.session.documents["regression"].draft_text
    count = tab.undo_stack.count()
    bounds = ink_bounds(tab.canvas._pixmap_item.pixmap().toImage())
    selected = window._selected_ids()[0]
    overlay = tab.canvas._overlays[selected].rect()
    transform = tab.canvas.transform().m11()
    tab.canvas.zoom_by(2)
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    assert tab.canvas.transform().m11() == pytest.approx(transform * 2)
    assert ink_bounds(tab.canvas._pixmap_item.pixmap().toImage()) == pytest.approx(bounds, abs=2)
    after_overlay = tab.canvas._overlays[selected].rect()
    assert after_overlay.x() == pytest.approx(overlay.x(), abs=0.5)
    assert after_overlay.width() == pytest.approx(overlay.width(), abs=0.5)
    assert window.session.documents["regression"].model == before
    assert window.session.documents["regression"].draft_text == before_text
    assert tab.undo_stack.count() == count
    assert not errors


@pytest.mark.parametrize("kind", ["rectangle", "ellipse", "line"])
def test_drawing_shapes_produces_visible_preview_and_palette_changes_are_undoable(make_editor, qtbot, monkeypatch, kind):
    window, _root, errors = make_editor()
    tab = window.active_tab
    window.right_tabs.setCurrentIndex(1)
    canvas = tab.canvas
    start = canvas.mapFromScene(QPoint(8, 8))
    end = canvas.mapFromScene(QPoint(38, 30))
    window._begin_tool(kind)
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert selected_node(window).type == kind
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    image = canvas._pixmap_item.pixmap().toImage()
    assert any(image.pixelColor(x, y).red() < 100 for y in range(image.height()) for x in range(image.width()))
    for field, picker in (("stroke", "shapeStrokePicker"), ("fill", "shapeFillPicker")):
        button = window.element_properties.findChild(QPushButton, picker)
        if field == "fill" and kind == "line":
            assert button is None or not button.isVisible()
            continue
        before = getattr(selected_node(window), field)
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: QColor("#2468AC")))
        button.click()
        assert QColor(getattr(selected_node(window), field)) == QColor("#2468AC")
        tab.undo_stack.undo()
        assert getattr(selected_node(window), field) == before
        tab.undo_stack.redo()
        count = tab.undo_stack.count()
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: QColor()))
        button.click()
        assert tab.undo_stack.count() == count
    assert not errors


def test_alignment_label_is_hidden_with_unsupported_elements(qtbot):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    properties.show()
    for node in (text_node(), ShapeNode(id="rect", name="Rect", type="rectangle", x_mm=0, y_mm=0, width_mm=4, height_mm=4),
                 GroupNode(id="group", name="Group", type="group", x_mm=0, y_mm=0), text_node()):
        properties.show_node(node)
        label = properties.form.labelForField(properties.alignment_widget)
        assert label.isVisible() == (node.type == "html")
        assert properties.alignment_widget.isVisible() == (node.type == "html")


def matches_icon(icon, filename):
    size = QSize(22, 22)
    expected = QPixmap(str(ROOT / "src/componentpress/app_assets" / filename)).scaled(
        size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return icon.pixmap(size, QIcon.Mode.Normal, QIcon.State.Off).toImage() == expected.toImage()


def test_properties_lock_icons_toggle_model_and_undo(make_editor):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2)
    button = window.element_properties.findChild(QPushButton, "elementLocked")
    assert button is not None and button.isCheckable()
    assert not button.isChecked() and matches_icon(button.icon(), "lock_open.png")
    button.click()
    assert selected_node(window).locked and button.isChecked()
    assert matches_icon(button.icon(), "lock.png")
    assert not window.element_properties.x_spin.isEnabled()
    button.click()
    assert not selected_node(window).locked and window.element_properties.x_spin.isEnabled()
    window.active_tab.undo_stack.undo()
    assert selected_node(window).locked and matches_icon(button.icon(), "lock.png")
    window.active_tab.undo_stack.redo()
    assert not selected_node(window).locked and matches_icon(button.icon(), "lock_open.png")
    assert not errors


def test_tree_lock_icon_click_toggles_without_moving_element(make_editor, qtbot):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2, node_id="text")
    window.right_tabs.setCurrentIndex(1)
    tree = window.element_tree
    tree.expandAll()
    for locked, filename in ((False, "lock_open.png"), (True, "lock.png")):
        index = window._index_for_id("text")
        assert matches_icon(index.data(Qt.ItemDataRole.DecorationRole), filename)
        icon_rect = tree.lock_icon_rect(index)
        assert not icon_rect.isEmpty() and tree.visualRect(index).contains(icon_rect.center())
        qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=icon_rect.center())
        assert selected_node(window).locked is not locked
        assert (selected_node(window).x_mm, selected_node(window).y_mm) == (2, 2)
    assert not errors


def test_child_lock_control_cannot_bypass_locked_parent(make_editor):
    window, _root, errors = make_editor()
    assert window.add_html("<p>One</p>", 2, 2, node_id="one")
    assert window.add_html("<p>Two</p>", 2, 12, node_id="two")
    window._set_selection(("one", "two"), source="tree")
    assert window.group_selected("group", "Group")
    window.element_properties.findChild(QPushButton, "elementLocked").click()
    window._set_selection(("one",), source="tree")
    button = window.element_properties.findChild(QPushButton, "elementLocked")
    assert not button.isEnabled()
    assert matches_icon(button.icon(), "lock.png")
    button.click()
    assert locations(window.session.documents["regression"].model)["group"].node.locked
    assert not errors
