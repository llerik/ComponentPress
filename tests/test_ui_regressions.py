"""Regression scenarios for editor rendering, properties and dock behaviour."""

from pathlib import Path
from dataclasses import replace
import shutil
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QColorDialog, QComboBox, QDialog, QMessageBox, QPushButton

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.bindings.resolver import BindingResolver
from componentpress.bootstrap import project_controller
from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, ShapeNode
from componentpress.domain.project import DataSource, ProjectDefinition
from componentpress.domain.tree import locations
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot
from componentpress.editor.dialogs import HtmlAddDialog
from componentpress.editor.main_window import MainWindow
from componentpress.editor.canvas import ComponentCanvas
from componentpress.editor.canvas import ComponentPreview
from componentpress.editor.properties import ElementProperties
from componentpress.editor.theme import apply_theme
from componentpress.editor.ui_state import UiState
from componentpress.app_assets import load_icon
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
    project = ProjectDefinition(schema_version=6, id="test", name="Test", version="0.1.0")
    component = ComponentDefinition(schema_version=6, id="card", name="Card",
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


@pytest.mark.parametrize("dpi", [48, 96, 120, 192, 384])
@pytest.mark.parametrize("fit", ["contain", "cover", "stretch"])
def test_image_overlay_uses_exact_millimetre_geometry_at_preview_dpi(tmp_path, dpi, fit):
    ensure_gui_application()
    source = QImage(101, 67, QImage.Format.Format_ARGB32)
    source.fill(QColor("#0000FF"))
    loader = SimpleNamespace(image=lambda *args, **kwargs: SimpleNamespace(image=source))
    node = ImageNode(id="image", name="Image", type="image", x_mm=7, y_mm=9,
                     width_mm=12, height_mm=8, source="assets/image.png", fit=fit)
    value = snapshot(tmp_path, [node])
    image = render_component_image(value, "card", dpi=dpi, resource_loader=loader, preserve_logical_size=True)
    canvas = ComponentCanvas()
    canvas.set_document(image, value.documents["card"].model, selected=("image",))

    rect = canvas._overlays["image"].sceneBoundingRect()
    factor = 96 / 25.4
    assert rect.getRect() == pytest.approx((7 * factor, 9 * factor, 12 * factor, 8 * factor))
    assert canvas._pixmap_item.sceneBoundingRect().getRect() == pytest.approx((0, 0, 30 * factor, 20 * factor))


def test_actual_size_uses_96_logical_dpi_and_preview_zoom_is_independent(tmp_path, qtbot):
    ensure_gui_application()
    value = snapshot(tmp_path, [])
    component = value.documents["card"].model
    image = QImage(227, 151, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    canvas = ComponentCanvas()
    preview = ComponentPreview()
    qtbot.addWidget(canvas)
    qtbot.addWidget(preview)
    canvas.set_document(image, component)
    preview.set_frame(image, component)
    canvas.actual_size()
    preview.actual_size()
    expected = 30 * 96 / 25.4
    assert canvas.transform().m11() == pytest.approx(1.0)
    assert preview.transform().m11() == pytest.approx(1.0)
    assert canvas._pixmap_item.sceneBoundingRect().width() == pytest.approx(expected)
    assert preview._pixmap_item.sceneBoundingRect().width() == pytest.approx(expected)
    canvas.zoom_by(2)
    assert canvas.transform().m11() == pytest.approx(2.0)
    assert preview.transform().m11() == pytest.approx(1.0)
    preview.zoom_by(1.5)
    replacement = QImage(454, 302, QImage.Format.Format_ARGB32)
    replacement.fill(QColor("red"))
    preview.set_frame(replacement, component)
    assert canvas.transform().m11() == pytest.approx(2.0)
    assert preview.transform().m11() == pytest.approx(1.5)


def test_canvas_hover_cursor_matches_resize_handle_and_move(make_editor, qtbot):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Move</p>", 2, 2, node_id="cursor-node")
    tab = window.active_tab
    canvas = tab.canvas
    canvas.show()
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    canvas.select_ids(("cursor-node",))
    overlay = canvas._overlays["cursor-node"]
    expected = (
        Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeHorCursor,
        Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeHorCursor,
    )
    for index, center in enumerate(canvas._handle_centers(overlay)):
        canvas._update_hover_cursor(center)
        assert canvas.viewport().cursor().shape() == expected[index]
    inside = canvas.mapFromScene(overlay.mapToScene(overlay.rect().center()))
    canvas._update_hover_cursor(QPoint(inside))
    assert canvas.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor
    assert not errors


def test_pending_preview_keeps_stale_raster_and_current_selection_interactive(make_editor, qtbot):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Move</p>", 2, 2, node_id="move-me")
    tab = window.active_tab
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    assert tab.canvas._pixmap_item is not None and tab.canvas._pixmap_item.isVisible()
    node = locations(window.session.documents["regression"].model)["move-me"].node
    moved = node.model_copy(update={"x_mm": node.x_mm + 2})
    component = window.session.documents["regression"].model.model_copy(
        update={"elements": (moved,)}
    )
    generation = tab.queue_preview(window.session, component=component, selected=(node.id,))
    previous_item = tab.canvas._pixmap_item
    assert previous_item.isVisible()
    assert tab.canvas.selected_ids == (node.id,)
    expected_x = moved.x_mm * 96 / 25.4
    assert tab.canvas._overlays[node.id].sceneBoundingRect().left() == pytest.approx(expected_x)
    assert tab.canvas._page_contains(tab.canvas._overlays[node.id].sceneBoundingRect().center())
    qtbot.waitUntil(lambda: tab.displayed_generation == generation, timeout=15000)
    assert tab.canvas._pixmap_item is previous_item
    assert tab.canvas._pixmap_item.isVisible()
    assert not errors


def test_cancelled_window_close_keeps_preview_queue_open(make_editor, monkeypatch):
    from componentpress.preview_coordinator import PreviewJob, PreviewPhase

    window, _root, _errors = make_editor()
    tab = window.active_tab
    window.session.documents[tab.component_id].set_draft("unsaved")
    tab.queue_preview(window.session)
    tab.preview_timer.setInterval(60000)
    request = tab.preview_request_data(window._preview_session_identity)
    job = PreviewJob(request, tab, SimpleNamespace(close=lambda: None))
    window._preview_coordinator.submit(job)
    window._preview_coordinator.begin_render(request.identity.request)
    monkeypatch.setattr(window, "_ask_unsaved_choice", lambda *_args: QMessageBox.StandardButton.Cancel)

    window.close()

    assert not window._preview_coordinator.closed
    assert tab.preview_timer.isActive()
    assert window._preview_coordinator.active is job
    assert window._preview_coordinator.phase is PreviewPhase.RENDER


def test_preview_first_and_last_follow_filtered_excel_rows(make_editor, qtbot):
    window, _root, errors = make_editor(excel=True)
    tab = window.open_component("forest-card")
    assert tab is not None
    window.data_mode.setChecked(True)
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    data = window.controller.preview.cached_data(window.session.snapshot, "forest-card")
    eligible = [row.row_number for row in data.rows if row.test_copies > 0]
    assert len(eligible) >= 2

    assert not tab.preview_first.isEnabled()
    tab.preview_last.click()
    assert tab.preview_row_number == eligible[-1]
    assert tab.instance_position.text() == f"Экземпляр {len(eligible)} из {len(eligible)}"
    assert window.data_row.currentData() == eligible[-1]
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    assert not tab.preview_last.isEnabled() and tab.preview_first.isEnabled()

    tab.preview_first.click()
    assert tab.preview_row_number == eligible[0]
    assert tab.instance_position.text() == f"Экземпляр 1 из {len(eligible)}"
    assert window.data_row.currentData() == eligible[0]
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    assert not tab.preview_first.isEnabled() and tab.preview_last.isEnabled()
    assert not errors


def test_active_static_validation_ignores_missing_project_excel_and_checks_resources(make_editor):
    window, root, _errors = make_editor(excel=True)
    assert window.add_image("assets/images/missing.png", 2, 2, node_id="missing-image")
    (root / "data" / "game.xlsx").unlink()

    window._validate_active()

    messages = [window.event_list.item(i).text() for i in range(window.event_list.count())]
    assert any("RESOURCE_MISSING" in item and "missing-image" in item for item in messages)
    assert not any("DATA_SOURCE_MISSING" in item for item in messages)


@pytest.mark.parametrize("changes", [{"height_mm": 0.1}, {"width_mm": 0.1}])
def test_html_overflow_wraps_and_clips_without_aborting_render(tmp_path, changes):
    ensure_gui_application()
    value = snapshot(tmp_path, [text_node(**changes)])
    output = tmp_path / "overflow.png"
    render_component(value, "card", output, dpi=192)
    image = QImage(str(output))
    assert not image.isNull()
    assert image.width() == round(30 * 192 / 25.4)
    assert image.height() == round(20 * 192 / 25.4)


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
    theme_settings = QSettings(str(tmp_path / "theme.ini"), QSettings.Format.IniFormat)
    theme_settings.setValue("theme", "light")
    monkeypatch.setattr("componentpress.editor.main_window.theme_settings", lambda: theme_settings)

    def make(excel=False):
        apply_theme("light")
        root = tmp_path / ("excel" if excel else "static")
        shutil.copytree(ROOT / "examples" / ("demo-excel-game" if excel else "demo-game"), root,
                        ignore=shutil.ignore_patterns(".componentpress"))
        controller = project_controller()
        service = controller.service
        window = MainWindow(controller)
        monkeypatch.setattr(window, "_ask_unsaved_choice", lambda *_args: QMessageBox.StandardButton.Discard)
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


def test_empty_preview_invalidates_late_worker_result(make_editor, monkeypatch, qtbot):
    window, _root, _errors = make_editor(excel=True)
    tab = window.open_component("forest-card")
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation)
    old_generation = tab.preview_generation
    monkeypatch.setattr(window.controller.preview, "cached_data", lambda *_args, **_kwargs: None)
    assert not window._refresh_preview(tab)
    assert tab.preview_generation > old_generation
    assert tab.preview_arguments is None
    assert not tab.preview_timer.isActive()
    assert tab.canvas._pixmap_item is None
    assert not tab.apply_preview(QImage(1, 1, QImage.Format.Format_ARGB32), generation=old_generation)


def test_build_reservation_rejects_preview_from_dialog_event_loop(make_editor, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    from componentpress.editor import main_window as main_window_module

    window, _root, _errors = make_editor()
    tab = window.active_tab
    starts = []
    monkeypatch.setattr(window.controller, "start_build", lambda request, **_kwargs: starts.append(request) or "test-job")

    class Dialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *_args, **_kwargs):
            self.workers = SimpleNamespace(value=lambda: 2)

        def exec(self):
            tab.previewRequested.emit()
            return QDialog.DialogCode.Accepted

        def print_settings(self):
            return window.session.snapshot.model.print

    monkeypatch.setattr(main_window_module, "BuildOptionsDialog", Dialog)
    window._preview_coordinator.active = object()
    window._start_build(False)
    assert window._build_pending_active_only is False and not starts
    window._preview_coordinator.active = None
    window._continue_build_after_preview()
    assert len(starts) == 1
    assert window._preview_coordinator.active is None
    assert window._active_build_job == "test-job"
    window._active_build_job = None
    window._build_progress.close()
    window._build_progress.deleteLater()
    window._build_progress = None


def test_build_dialog_cancel_releases_reservation_and_resumes_preview(make_editor, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from componentpress.editor import main_window as main_window_module

    window, _root, _errors = make_editor()

    class Dialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(main_window_module, "BuildOptionsDialog", Dialog)
    window._start_build(False)
    assert window._build_pending_active_only is None
    assert window.active_tab.preview_timer.isActive()


def test_build_reservation_rejects_project_switch_methods(make_editor, monkeypatch, tmp_path):
    window, _root, _errors = make_editor()
    session = window.session
    window._build_pending_active_only = False
    monkeypatch.setattr(window.controller, "open", lambda *_args: pytest.fail("project opened during build reservation"))
    monkeypatch.setattr(window.controller, "create", lambda *_args: pytest.fail("project created during build reservation"))
    assert not window.open_path(tmp_path)
    assert not window.create_path(tmp_path, "blocked")
    assert window.session is session
    assert window._build_pending_active_only is False


def test_preparation_freezes_text_commands_until_snapshot_acceptance(make_editor):
    window, _root, _errors = make_editor()
    tab = window.active_tab
    document = window.session.documents[tab.component_id]
    tab.set_mode("text")
    tab.text_editor.insertPlainText("\n# draft")
    before_draft = document.draft_text
    before_history = tab.undo_stack.count()
    window._active_build_job = "preparing-job"
    window.controller.build = SimpleNamespace(
        is_preparing=lambda job_id: job_id == "preparing-job", cancel=lambda _job_id: True,
    )
    window._update_actions()

    assert tab.text_editor.isReadOnly()
    assert not window._apply_tree("blocked", lambda model: model)
    window._draft_changed(tab, "changed during preparation")
    window._undo()
    assert document.draft_text == before_draft
    assert tab.undo_stack.count() == before_history

    window.controller.build.is_preparing = lambda _job_id: False
    window._update_actions()
    assert not tab.text_editor.isReadOnly()
    assert window.save_all_action.isEnabled()
    window._active_build_job = None
    window._update_actions()


def test_text_undo_shortcut_does_not_fall_through_to_document_history(make_editor, qtbot):
    from componentpress.editor.project_history import ComponentHistory

    window, _root, _errors = make_editor()
    window.open_component("forest-card")
    tab = window.active_tab
    document = window.session.documents[tab.component_id]
    window._set_selection(("heading",), source="test")
    window._move_selected(("heading",), 1, 0)
    after_edit = document.model
    assert tab.undo_stack.canUndo()
    tab.set_text(document.current_text)
    tab.set_mode("text")
    window._component_history = ComponentHistory()
    window._project_history_focus = False
    window._update_actions()
    assert not window.undo_action.isEnabled()

    QTest.keyClick(tab.text_editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    assert document.model == after_edit
    assert tab.undo_stack.canUndo()


def test_cancelled_close_releases_build_reservation_and_resumes_preview(make_editor, monkeypatch):
    window, _root, _errors = make_editor()
    tab = window.active_tab
    resumed = []
    monkeypatch.setattr(window, "_confirm_all_dirty", lambda: False)
    monkeypatch.setattr(window, "_refresh_preview", lambda current: resumed.append(current))
    tab.preview_deferred = True
    window._build_pending_active_only = False
    assert not window.close()
    assert window._build_pending_active_only is None
    assert resumed == [tab]


def test_build_progress_ignores_late_values_from_older_phase(make_editor):
    from types import SimpleNamespace
    from componentpress.application.contracts import BuildProgress

    window, _root, _errors = make_editor()
    accepted = []
    window._active_build_job = "progress-test"
    window._build_progress = SimpleNamespace(
        setRange=lambda *_args: None,
        setValue=accepted.append,
        setLabelText=lambda *_args: None,
    )
    window._on_build_progress(BuildProgress("progress-test", "rendering", 2, 4))
    window._on_build_progress(BuildProgress("progress-test", "rendering", 1, 4))
    window._on_build_progress(BuildProgress("progress-test", "assembling_pdf", 1, 3))
    window._on_build_progress(BuildProgress("progress-test", "rendering", 4, 4))
    assert accepted == [2, 1]
    window._active_build_job = None
    window._build_progress = None


def selected_node(window):
    model = window.session.documents[window.active_tab.component_id].model
    return locations(model)[window._selected_ids()[0]].node


def test_work_panel_can_grow_without_screen_limit_and_restore_its_size(make_editor, qtbot):
    window, _root, _errors = make_editor()
    dock = window.right_dock
    assert dock.width() <= 360
    assert dock.maximumWidth() > window.screen().geometry().width()
    assert window.right_panel.minimumWidth() == 0 or window.right_panel.minimumWidth() <= 180
    dock.setFloating(True)
    target = window.screen().geometry().width() // 2 + 120
    dock.resize(target, 500)
    qtbot.waitUntil(lambda: dock.width() >= target)
    saved_width = dock.width()
    window.ui_state.save(window)
    dock.resize(220, 500)
    window.ui_state.restore(window)
    qtbot.waitUntil(lambda: abs(dock.width() - saved_width) <= 4)
    assert dock.maximumWidth() > window.screen().geometry().width()


def test_work_panel_can_grow_past_previous_limit_when_docked(make_editor, qtbot):
    window, _root, _errors = make_editor()
    dock = window.right_dock
    target = window.screen().geometry().width() // 2 + 80
    dock.setMinimumWidth(target)
    qtbot.waitUntil(lambda: dock.width() >= target)
    assert dock.width() > window.screen().geometry().width() // 2
    assert dock.maximumWidth() > window.screen().geometry().width()


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


def test_html_creation_uses_font_list_and_palette_and_whole_point_step(make_editor, qtbot, monkeypatch):
    window, _root, errors = make_editor()
    chosen = QColor("#2468AC")
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: chosen))
    captured = {}

    def accept_dialog(dialog):
        font = dialog.findChild(QComboBox, "newHtmlFont")
        assert font is not None and not font.isEditable() and font.count() > 0
        captured["font"] = font.currentText()
        assert dialog.font_size.decimals() == 1
        assert dialog.font_size.singleStep() == pytest.approx(1.0)
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


def test_font_properties_use_whole_point_step_without_editing_existing_document_on_selection(make_editor):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2, font_size_pt=10.26)
    before = window.session.documents["regression"].model
    count = window.active_tab.undo_stack.count()
    window._show_selection(window._selected_ids())
    assert window.session.documents["regression"].model == before
    assert window.active_tab.undo_stack.count() == count
    spin = window.element_properties.font_spin
    assert spin.decimals() == 1 and spin.singleStep() == pytest.approx(1.0)
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


def test_html_properties_switch_between_manual_and_excel_column_controls(qtbot):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    properties.show()
    node = text_node(content_mode="column", content_column="Text")
    properties.show_node(node, columns=("Text",))
    properties.set_edit_locked(False)
    column_label = properties.form.labelForField(properties.content_column)
    assert column_label.isVisible() and properties.content_column.isVisible()
    assert not properties.html_edit.isVisible()
    assert not properties.apply_html.isVisible() and not properties.insert_image.isVisible()
    original = properties.html_edit.toPlainText()
    assert properties.html_edit.isReadOnly() or not properties.html_edit.isEnabled()
    assert not properties.apply_html.isEnabled()
    assert not properties.insert_image.isEnabled()
    qtbot.keyClicks(properties.html_edit, "must not be inserted")
    assert properties.html_edit.toPlainText() == original
    properties.content_mode.setCurrentIndex(properties.content_mode.findData("manual"))
    assert not column_label.isVisible() and not properties.content_column.isVisible()
    assert properties.html_edit.isVisible()
    assert properties.apply_html.isVisible() and properties.insert_image.isVisible()
    assert properties.html_edit.isEnabled() and not properties.html_edit.isReadOnly()
    assert properties.apply_html.isEnabled() and properties.insert_image.isEnabled()
    properties.set_edit_locked(True)
    assert not properties.html_edit.isEnabled()
    properties.set_edit_locked(False)
    assert properties.html_edit.isEnabled() and not properties.html_edit.isReadOnly()


def test_layer_buttons_match_tool_dimensions(make_editor):
    window, _root, _errors = make_editor()
    buttons = (window.layer_up, window.layer_down, window.group_button, window.ungroup_button)
    assert all(button.size() == window.select_tool.size() == QSize(34, 34) for button in buttons)
    assert all(not button.icon().isNull() and button.accessibleName() for button in buttons)
    assert not hasattr(window, "reparent_button")


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
    before_image = canvas._pixmap_item.pixmap().toImage().copy()
    start = canvas.mapFromScene(QPoint(8, 8))
    end = canvas.mapFromScene(QPoint(38, 30))
    window._begin_tool(kind)
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert selected_node(window).type == kind
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    image = canvas._pixmap_item.pixmap().toImage()
    assert image != before_image
    for field, picker in (("stroke", "shapeStrokePicker"), ("fill", "shapeFillPicker")):
        button = window.element_properties.findChild(QPushButton, picker)
        if field == "fill" and kind == "line":
            assert button is None or not button.isVisible()
            continue
        before = getattr(selected_node(window), field)
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: QColor("#802468AC")))
        button.click()
        assert getattr(selected_node(window), field) == "#802468AC"
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


def test_html_color_swatch_keeps_alpha_and_accepts_editable_hex(qtbot):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    node = text_node(color="#80123456")
    properties.show_node(node)
    changes = []
    picked = []
    properties.nodeChanged.connect(changes.append)
    properties.colorRequested.connect(picked.append)
    assert not properties.color_edit.isReadOnly()
    assert properties.color_edit.maxLength() == 9
    assert properties.color_edit.width() >= properties.color_edit.fontMetrics().horizontalAdvance("#AARRGGBB")
    assert not properties.color_swatch.icon().isNull()
    properties.color_swatch.click()
    assert picked == ["#80123456"]
    properties.color_edit.setText("#2468AC80")
    properties.color_edit.editingFinished.emit()
    assert changes == [{"color": "#2468AC80"}]
    assert properties.color_edit.text() == "#2468AC80"
    properties.show_node(text_node(color="#2468AC80"))
    properties.color_edit.setText("#NOTHEX")
    properties.color_edit.editingFinished.emit()
    assert changes == [{"color": "#2468AC80"}]
    assert properties.color_edit.text() == "#2468AC80"
    assert not properties.color_swatch.icon().isNull()


@pytest.mark.parametrize("field", ("fill", "stroke"))
def test_shape_colors_use_editable_hex_and_keep_transparency(qtbot, field):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    node = ShapeNode(
        id="shape", name="Shape", type="rectangle", x_mm=0, y_mm=0,
        width_mm=10, height_mm=10, fill="#80123456", stroke="#AA345678",
    )
    properties.show_node(node)
    edit = properties.fill_color if field == "fill" else properties.stroke_color
    swatch = properties.fill_button if field == "fill" else properties.stroke_button
    changes = []
    properties.nodeChanged.connect(changes.append)
    assert not edit.isReadOnly()
    assert edit.maxLength() == 9
    assert not swatch.icon().isNull()
    edit.setText("#2468AC80")
    edit.editingFinished.emit()
    assert changes == [{field: "#2468AC80"}]
    edit.setText("not a color")
    edit.editingFinished.emit()
    assert edit.text() == "#2468AC80"
    assert changes == [{field: "#2468AC80"}]
    if field == "fill":
        properties.no_fill_button.click()
        assert changes[-1] == {"fill": None}
        before = swatch.icon().pixmap(24, 24).toImage()
        properties.show_node(node.model_copy(update={"fill": None}))
        assert not swatch.icon().isNull()
        assert swatch.icon().pixmap(24, 24).toImage() != before


def test_component_background_picker_and_hex_follow_html_color_control(make_editor, qtbot, monkeypatch):
    window, _root, errors = make_editor()
    background = window.properties
    component_id = window.active_tab.component_id
    original = window.session.documents[component_id].model.background
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: QColor("#2468AC")))
    background.background_button.click()
    assert window.session.documents[component_id].model.background == "#2468AC"
    assert background.background_edit.text() == "#2468AC"
    assert not background.background_button.icon().isNull()
    window.active_tab.undo_stack.undo()
    assert background.background_edit.text() == original
    background.background_edit.setText("#XYZXYZ")
    background.background_edit.editingFinished.emit()
    assert background.background_edit.text() == original
    assert not errors


def test_validate_project_toolbar_action_forces_excel_refresh(make_editor, monkeypatch):
    window, _root, errors = make_editor(excel=True)
    preview = window.controller.preview
    validate_all = preview.validate_all
    calls = []

    def validate(snapshot, **kwargs):
        calls.append(kwargs)
        return validate_all(snapshot, **kwargs)

    monkeypatch.setattr(preview, "validate_all", validate)
    window.validate_all_action.trigger()
    assert calls == [{"force_read": True, "manual": True}]
    assert not errors


def test_rotation_precision_does_not_round_model_when_field_is_untouched(qtbot):
    properties = ElementProperties()
    qtbot.addWidget(properties)
    properties.show_node(text_node(transform={"m11": 0.8, "m12": -0.6, "m21": 0.6, "m22": 0.8}))
    requested = []
    properties.rotationRequested.connect(requested.append)
    assert properties.rotation_spin.decimals() == 1
    assert properties.rotation_spin.singleStep() == pytest.approx(0.1)
    properties.rotation_spin.editingFinished.emit()
    assert requested == []
    properties.rotation_spin.setValue(properties.rotation_spin.value() + 0.1)
    properties.rotation_spin.editingFinished.emit()
    assert requested == [pytest.approx(37.0)]


def test_text_alignment_uses_existing_icons_with_accessible_names(qtbot):
    apply_theme("light")
    properties = ElementProperties()
    qtbot.addWidget(properties)
    properties.show_node(text_node())
    expected = {"left": "format_align_left.png", "center": "format_align_center.png", "right": "format_align_right.png"}
    for value, filename in expected.items():
        button = properties.alignment_buttons[value]
        assert not button.text()
        assert button.toolTip()
        assert button.accessibleName() == button.toolTip()
        assert matches_icon(button.icon(), filename)


def test_html_swatch_palette_updates_alpha_color_and_undo(make_editor, qtbot, monkeypatch):
    window, _root, errors = make_editor()
    assert window.add_html("<p>Text</p>", 2, 2, node_id="text")
    properties = window.element_properties
    swatch = properties.findChild(QPushButton, "htmlColorPicker")
    assert swatch is not None and not swatch.text()
    original_swatch = swatch.icon().pixmap(24, 24).toImage()
    chosen = QColor("#802468AC")
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **kw: chosen))
    swatch.click()
    assert selected_node(window).color == "#802468AC"
    assert properties.color_edit.text() == "#802468AC"
    changed_swatch = swatch.icon().pixmap(24, 24).toImage()
    assert changed_swatch != original_swatch
    window.active_tab.undo_stack.undo()
    assert properties.color_edit.text() == "#111111"
    assert swatch.icon().pixmap(24, 24).toImage() == original_swatch
    window.active_tab.undo_stack.redo()
    assert properties.color_edit.text() == "#802468AC"
    assert swatch.icon().pixmap(24, 24).toImage() == changed_swatch
    assert not errors


def matches_icon(icon, filename):
    size = QSize(22, 22)
    expected = load_icon(filename).pixmap(size, QIcon.Mode.Normal, QIcon.State.Off)
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
