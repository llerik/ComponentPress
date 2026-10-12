"""Behavioral coverage for the compact stage-14 editor shell."""

from pathlib import Path
import shutil

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QTreeView

from componentpress.bootstrap import project_service
from componentpress.editor.component_dialog import ComponentDialog
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.editor.ui_state import UiState
from componentpress.editor.qt_models import ID_ROLE
from componentpress.editor.project_settings_dialog import ProjectSettingsDialog
from componentpress.editor import main_window as main_window_module
from componentpress.editor.dialogs import format_error
from componentpress.project_io.repository import FileProjectRepository


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def make_window(tmp_path: Path, qtbot):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    root = tmp_path / "game"
    shutil.copytree(DEMO, root)
    service = project_service()
    service.register(root, "second", "Второй компонент")
    window = MainWindow(ProjectController(service))
    window.ui_state = UiState(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    return root, window, service


def use_temp_ui_settings(window: MainWindow, tmp_path: Path) -> None:
    window.ui_state = UiState(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


def test_project_settings_action_tracks_project_and_tab_state(tmp_path: Path, qtbot) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    root = tmp_path / "game"
    shutil.copytree(DEMO, root)
    service = project_service()
    service.register(root, "second", "Второй компонент")
    window = MainWindow(ProjectController(service))
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    assert not window.project_settings_action.isEnabled()
    window.load_session(service.open_session(root))
    assert window.project_settings_action.isEnabled()  # A project can have no open document.
    window.session.documents.clear()
    window._update_actions()
    assert window.project_settings_action.isEnabled()  # Empty projects still have project settings.
    window.controller.set_session(None)
    window._update_actions()
    assert not window.project_settings_action.isEnabled()


def test_window_menu_actions_toggle_panels_and_sync_close_button(tmp_path: Path, qtbot) -> None:
    root, window, service = make_window(tmp_path, qtbot)
    actions = window.windows_menu.actions()
    assert [action.text() for action in actions] == ["Инструменты", "Рабочие панели", "События", "Превью"]
    assert all(action.isCheckable() for action in actions)
    window.controller.set_session(None)
    window._update_project_visibility()
    assert all(not action.isEnabled() for action in actions)
    window.load_session(service.open_session(root))
    assert all(action.isEnabled() for action in actions)

    for action, dock, area in zip(actions, window.docks, (
        Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea,
        Qt.DockWidgetArea.BottomDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea,
    )):
        dock.hide()
        action.trigger()
        assert dock.isVisible() and action.isChecked()
        assert window.dockWidgetArea(dock) == area
        action.trigger()
        assert not dock.isVisible() and not action.isChecked()
        dock.show()  # The dock's close affordance uses the same native visibility action.
        qtbot.waitUntil(action.isChecked)
        dock.hide()
        assert not action.isChecked()

    # Hiding a panel while loaded survives a project close/open cycle.
    window.toolbox_dock.hide()
    window.ui_state.save(window)
    window._update_project_visibility()
    restored = MainWindow(ProjectController(project_service()))
    restored.ui_state = UiState(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    restored.ui_state.restore(restored)
    qtbot.addWidget(restored)
    restored.load_session(project_service().open_session(root))
    restored.show()
    assert not restored.toolbox_dock.isVisible()
    restored.toolbox_dock.toggleViewAction().trigger()
    assert restored.toolbox_dock.isVisible()
    assert restored.dockWidgetArea(restored.toolbox_dock) == Qt.DockWidgetArea.LeftDockWidgetArea


def test_delete_component_action_requires_active_tab_and_deletes_selected_component(tmp_path: Path, qtbot, monkeypatch) -> None:
    root, window, service = make_window(tmp_path, qtbot)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    assert not window.delete_component_action.isEnabled()
    window.load_session(service.open_session(root))
    assert not window.delete_component_action.isEnabled()  # Project open, no active tab.
    first = window.open_component("forest-card")
    assert first and window.delete_component_action.isEnabled()
    window._request_mode(first, "text")
    assert not first.text_editor.document().isUndoAvailable()
    second = window.open_component("second")
    window.tabs.setCurrentWidget(second)
    assert window.active_tab is second and window.active_tab.component_id == "second"
    window.delete_component_action.trigger()
    assert "second" not in window.session.documents
    assert not window.delete_component_action.isEnabled()  # Only one component remains.
    window._undo()
    assert "second" in window.session.documents
    window._redo()
    assert "second" not in window.session.documents
    window._undo()
    assert "second" in window.session.documents
    for index in reversed(range(window.tabs.count())):
        window.close_tab(index)
    assert not window.delete_component_action.isEnabled()


def test_closing_restored_component_before_branching_history_is_safe(tmp_path: Path, qtbot) -> None:
    _root, window, _service = make_window(tmp_path, qtbot)
    tab = window.open_component("second")
    assert tab is not None
    assert window.delete_component("second", confirm=False)
    window._undo()
    assert window._tabs["second"] is tab
    assert window.close_tab(window.tabs.indexOf(tab))
    qtbot.wait(20)
    assert window.add_component("third", "Третий компонент")
    assert "third" in window.session.documents


def test_excel_picker_preserves_nested_project_path_and_reuses_workbook(tmp_path: Path, qtbot, monkeypatch) -> None:
    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "game"
    shutil.copytree(source_root, root)
    service = project_service()
    window = MainWindow(ProjectController(service))
    use_temp_ui_settings(window, tmp_path)
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    workbook_before = (root / "data/game.xlsx").read_bytes()
    dialog = ProjectSettingsDialog(window.session, window.controller.preview, window)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **kw: (str(root / "data/game.xlsx"), "")))
    dialog._choose_file()
    assert dialog.source.text() == "data/game.xlsx"
    window.controller.update_project_settings(
        name=dialog.name.text(), version=dialog.version.text(), prod=dialog.prod.text(), test=dialog.test.text(),
        data_source_file=dialog.values()["source_file"],
    )
    assert window.session.snapshot.model.data_source.path == "data/game.xlsx"
    reopened = FileProjectRepository().open(root)
    assert reopened.model.data_source.path == "data/game.xlsx"
    assert (root / "data/game.xlsx").read_bytes() == workbook_before
    assert sorted(path.name for path in (root / "data").glob("*.xlsx")) == ["game.xlsx"]


def test_project_settings_external_nested_book_and_conflict_are_safe(tmp_path: Path, qtbot) -> None:
    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "game"
    shutil.copytree(source_root, root)
    external = tmp_path / "outside" / "deep" / "book.xlsx"
    external.parent.mkdir(parents=True)
    shutil.copy2(root / "data/game.xlsx", external)
    service = project_service()
    window = MainWindow(ProjectController(service))
    use_temp_ui_settings(window, tmp_path)
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.controller.update_project_settings(name="Game", version="0.1.0", prod="Prod", test="Debug", data_source_file=external)
    assert window.session.snapshot.model.data_source.path == "data/book.xlsx"
    assert (root / "data/book.xlsx").read_bytes() == external.read_bytes()
    conflict = tmp_path / "conflict" / "book.xlsx"
    conflict.parent.mkdir()
    conflict.write_bytes(b"not an xlsx")
    errors = []
    window._show_error = errors.append
    before_project = (root / "project.yaml").read_bytes()
    before_book = (root / "data/book.xlsx").read_bytes()
    try:
        window.controller.update_project_settings(name="Game", version="0.1.0", prod="Prod", test="Debug", data_source_file=conflict)
    except Exception as exc:
        assert str(exc).strip()
    else:
        raise AssertionError("invalid external XLSX unexpectedly accepted")
    assert (root / "project.yaml").read_bytes() == before_project
    assert (root / "data/book.xlsx").read_bytes() == before_book


def test_import_cleanup_hash_failure_preserves_primary_error_and_reused_book(tmp_path: Path, monkeypatch) -> None:
    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "game"
    shutil.copytree(source_root, root)
    service = project_service()
    session = service.open_session(root)
    source = tmp_path / "new.xlsx"
    shutil.copy2(root / "data/game.xlsx", source)
    original_book = (root / "data/game.xlsx").read_bytes()
    project_before = (root / "project.yaml").read_bytes()
    primary_error = RuntimeError("save failure must stay primary")
    monkeypatch.setattr(service.projects, "save_project", lambda *_args, **_kwargs: (_ for _ in ()).throw(primary_error))

    from componentpress.project_io import files as project_files

    real_hash = project_files.file_hash
    calls = 0

    def fail_rollback_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_hash(path)
        raise OSError("cleanup hash failed")

    monkeypatch.setattr(project_files, "file_hash", fail_rollback_hash)
    try:
        service.update_project_settings(
            session, name=session.snapshot.model.name, version=session.snapshot.model.version,
            prod="Prod", test="Debug", data_source_file=source,
        )
    except RuntimeError as exc:
        assert exc is primary_error
    else:
        raise AssertionError("primary save error was not raised")

    assert calls == 2
    assert (root / "data/game.xlsx").read_bytes() == original_book
    assert (root / "project.yaml").read_bytes() == project_before


def test_blank_errors_and_invalid_project_version_have_useful_message(tmp_path: Path, qtbot, monkeypatch) -> None:
    assert format_error(ValueError(" \n "))
    root, window, service = make_window(tmp_path, qtbot)
    window.load_session(service.open_session(root))
    errors = []
    original_show_error = window._show_error
    monkeypatch.setattr(window, "_show_error", errors.append)
    class InvalidSettingsDialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return {"name": "Game", "version": "bad", "prod": "Prod", "test": "Debug", "source": "", "source_file": None}

    monkeypatch.setattr(main_window_module, "ProjectSettingsDialog", InvalidSettingsDialog)
    window.project_settings_dialog()
    assert window.session.snapshot.model.version != "bad"
    assert errors and str(errors[0]).strip()

    shown = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda _parent, _title, message: shown.append(message)))
    monkeypatch.setattr(window, "_show_error", original_show_error)
    window._show_error(ValueError("  \n"))
    assert shown and shown[0].strip()


def test_demo_excel_component_tree_is_the_visible_interactive_tree(tmp_path: Path, qtbot) -> None:
    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "demo-excel-game"
    shutil.copytree(source_root, root)
    service = project_service()
    window = MainWindow(ProjectController(service))
    use_temp_ui_settings(window, tmp_path)
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    tree = window.right_tabs.widget(0).findChild(QTreeView, "componentTree")
    assert tree is window.component_tree and tree.model().rowCount() == 2
    ids = [tree.model().index(row, 0).data(ID_ROLE) for row in range(tree.model().rowCount())]
    assert ids == ["forest-card", "event-card"]
    event_index = tree.model().index(1, 0)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualRect(event_index).center())
    qtbot.waitUntil(lambda: window.active_tab is not None and window.active_tab.component_id == "event-card")
    forest = tree.model().index(0, 0)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualRect(forest).center())
    qtbot.waitUntil(lambda: window.active_tab.component_id == "forest-card")
    tree.setFocus()
    QTest.keyClick(tree, Qt.Key.Key_Down)
    qtbot.waitUntil(lambda: window.active_tab.component_id == "event-card")
    other = window.open_component("event-card")
    assert other is not None
    assert tree.currentIndex().data(ID_ROLE) == "event-card"
    forest_tab = window.open_component("forest-card")
    window.tabs.setCurrentWidget(forest_tab)
    qtbot.waitUntil(lambda: tree.currentIndex().data(ID_ROLE) == "forest-card")
    while window.tabs.count():
        window.close_tab(window.tabs.count() - 1)
    assert not tree.currentIndex().isValid()


def test_loading_excel_project_validates_every_component_and_renders_selected_layout(tmp_path: Path, qtbot) -> None:
    from componentpress.bootstrap import project_controller

    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "demo-excel-game"
    shutil.copytree(source_root, root)
    window = MainWindow(project_controller())
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    window.load_session(window.controller.open(root))

    snapshot = window.session.snapshot
    assert window.controller.preview.cached_data(snapshot, "forest-card") is not None
    assert window.controller.preview.cached_data(snapshot, "event-card") is not None
    tab = window.open_component("forest-card")
    assert tab is not None
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation)
    assert tab.canvas._pixmap_item is not None
    assert {"leaf-art", "text-group", "heading", "caption"}.issubset(tab.canvas._overlays)


def test_moving_and_undoing_element_keeps_excel_preview_visible(tmp_path: Path, qtbot) -> None:
    from componentpress.bootstrap import project_controller

    root = tmp_path / "demo-excel-game"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game", root)
    window = MainWindow(project_controller())
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    window.load_session(window.controller.open(root))
    tab = window.open_component("forest-card")
    assert tab is not None
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation)
    service = window.controller.preview
    snapshot = window.session.snapshot
    data = service.cached_data(snapshot, "forest-card")
    assert data is not None and tab.canvas._pixmap_item is not None

    window._move_selected(("leaf-art",), 2.0, 0.0)
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation)
    assert service.cached_data(snapshot, "forest-card") is data
    assert tab.canvas._pixmap_item is not None

    tab.undo_stack.undo()
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation)
    assert service.cached_data(snapshot, "forest-card") is data
    assert tab.canvas._pixmap_item is not None


def test_loading_excel_project_reports_bad_component_and_keeps_other_validation(tmp_path: Path, qtbot) -> None:
    from openpyxl import load_workbook
    from componentpress.bootstrap import project_controller

    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "demo-excel-game"
    shutil.copytree(source_root, root)
    book_path = root / "data" / "game.xlsx"
    book = load_workbook(book_path)
    book["Карты"]["D2"] = "assets/images/missing.png"
    book.save(book_path)
    book.close()

    window = MainWindow(project_controller())
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    window.load_session(window.controller.open(root))
    snapshot = window.session.snapshot

    assert window.controller.preview.cached_data(snapshot, "forest-card") is None
    assert window.controller.preview.cached_data(snapshot, "event-card") is not None
    assert any("RESOURCE_MISSING" in window.event_list.item(i).text() for i in range(window.event_list.count()))
    assert not window.events_dock.isHidden()
    tab = window.open_component("forest-card")
    assert tab is not None
    assert window.preview_image.text() == "Проверьте данные Excel, чтобы увидеть экземпляры"


def test_loading_excel_project_with_no_prod_instances_keeps_preview_message(tmp_path: Path, qtbot) -> None:
    from openpyxl import load_workbook
    from componentpress.bootstrap import project_controller

    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "demo-excel-game"
    shutil.copytree(source_root, root)
    book_path = root / "data" / "game.xlsx"
    book = load_workbook(book_path)
    book["Карты"]["I2"] = 0
    book["Карты"]["I3"] = 0
    book.save(book_path)
    book.close()

    window = MainWindow(project_controller())
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    window.load_session(window.controller.open(root))
    tab = window.open_component("forest-card")

    assert tab is not None
    assert window.preview_image.text() == "В выбранном режиме нет экземпляров"
    assert window.controller.preview.cached_data(window.session.snapshot, "forest-card") is not None
    assert not any("VALIDATION_ERROR" in window.event_list.item(i).text() for i in range(window.event_list.count()))


def test_reopening_project_invalidates_previous_excel_validation(tmp_path: Path, qtbot) -> None:
    from openpyxl import load_workbook
    from componentpress.bootstrap import project_controller

    source_root = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"
    root = tmp_path / "demo-excel-game"
    shutil.copytree(source_root, root)
    controller = project_controller()
    window = MainWindow(controller)
    use_temp_ui_settings(window, tmp_path)
    qtbot.addWidget(window)
    window.load_session(controller.open(root))
    assert controller.preview.cached_data(window.session.snapshot, "forest-card") is not None

    book_path = root / "data" / "game.xlsx"
    book = load_workbook(book_path)
    book["Карты"]["D2"] = "assets/images/missing.png"
    book.save(book_path)
    book.close()
    window.load_session(controller.open(root))

    assert controller.preview.cached_data(window.session.snapshot, "forest-card") is None
    assert controller.preview.cached_data(window.session.snapshot, "event-card") is not None
    assert any("RESOURCE_MISSING" in window.event_list.item(i).text() for i in range(window.event_list.count()))


def test_compact_shell_has_icon_tools_and_project_docks(tmp_path: Path, qtbot) -> None:
    _root, window, _service = make_window(tmp_path, qtbot)

    assert window.right_tabs.tabText(0) == "Компоненты"
    assert window.right_tabs.tabText(1) == "Элементы"
    assert window.events_dock.isVisible()
    assert window.preview_dock.isVisible()
    assert window.select_tool.icon().isNull() is False
    assert window.select_tool.text() == ""
    assert window.select_tool.toolTip() == "Выбор"
    assert window.about_menu.title() == "О программе"


def test_command_and_preview_buttons_use_the_assigned_png_icons(tmp_path: Path, qtbot) -> None:
    _root, window, _service = make_window(tmp_path, qtbot)
    assets = Path(__file__).resolve().parents[1] / "src" / "componentpress" / "app_assets"

    def matches_asset(icon: QIcon, filename: str) -> bool:
        size = QSize(22, 22)
        expected = QPixmap(str(assets / filename)).scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        actual = icon.pixmap(size, QIcon.Mode.Normal, QIcon.State.Off)
        return not actual.isNull() and actual.toImage() == expected.toImage()

    action_icons = {
        window.project_settings_action: "settings.png", window.add_component_action: "add.png",
        window.save_action: "save.png", window.undo_action: "undo.png", window.redo_action: "redo.png",
        window.delete_action: "remove.png", window.group_action: "group.png", window.ungroup_action: "ungroup.png",
        window.zoom_in_action: "zoom_up.png", window.zoom_out_action: "zoom_down.png", window.fit_action: "by_screen.png",
    }
    for action, filename in action_icons.items():
        assert matches_asset(action.icon(), filename), (action.text(), filename)
        assert action.toolTip() == action.text()
        assert action.property("accessibleName") == action.text()

    widget_icons = {
        window.select_tool: "cursor.png", window.image_tool: "image.png", window.html_tool: "text.png",
        window.conditional_group_tool: "condition.png", window.rectangle_tool: "rectangle.png",
        window.ellipse_tool: "ellipse.png", window.line_tool: "line.png", window.layer_up: "up.png",
        window.layer_down: "down.png", window.group_button: "group.png", window.ungroup_button: "ungroup.png",
        window.reparent_button: "move.png", window.clear_events: "delete.png",
        window.add_component_button: "add.png",
    }
    for button, filename in widget_icons.items():
        assert matches_asset(button.icon(), filename), (button.accessibleName(), filename)
    assert window.clear_events.text() == "Очистить"
    assert window.add_component_button.text() == ""

    preview_icons = (
        (window.preview_previous, "left.png", "Предыдущий"),
        (window.preview_next, "right.png", "Следующий"),
        (window.preview_zoom_in, "zoom_up.png", "Увеличить"),
        (window.preview_zoom_out, "zoom_down.png", "Уменьшить"),
        (window.preview_fit, "real_size.png", "По размеру"),
    )
    for button, filename, label in preview_icons:
        assert matches_asset(button.icon(), filename)
        assert button.text() == ""
        assert button.toolTip() == label
        assert button.accessibleName() == label
        assert button.iconSize() == QSize(22, 22)

    tab = window.open_component("forest-card")
    assert tab is not None
    assert matches_asset(tab.layout_mode.icon(), "cursor.png")
    assert matches_asset(tab.text_mode.icon(), "text.png")


def test_component_creation_and_deletion_round_trip_preserves_draft_and_tab(tmp_path: Path, qtbot, monkeypatch) -> None:
    root, window, service = make_window(tmp_path, qtbot)
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))

    assert window.add_component("third", "Третий компонент")
    tab = window.active_tab
    document = window.session.documents["third"]
    window._draft_changed(tab, "schema_version: 5\nid: third\nname: [неверный черновик]\n")
    window._undo()
    assert "third" not in window.session.documents
    assert not (root / "components" / "third.yaml").exists()
    window._redo()
    assert window.session.documents["third"] is document
    assert window.active_tab is tab
    assert document.has_invalid_draft
    assert document.draft_text.startswith("schema_version: 5")
    assert service.projects.open(root).documents["third"].model.name == "Третий компонент"
    assert not errors, errors

    assert window.delete_component("third", confirm=False)
    window._undo()
    assert "third" in window.session.documents
    assert window.active_tab is tab
    assert document.has_invalid_draft
    assert service.projects.open(root).documents["third"].model.name == "Третий компонент"
    assert not errors, errors


def test_failed_component_delete_does_not_hide_tab_or_advance_history(tmp_path: Path, qtbot, monkeypatch) -> None:
    root, window, _service = make_window(tmp_path, qtbot)
    tab = window.open_component("forest-card")
    before = window._component_history_index
    (root / "project.yaml").write_text("external change\n", encoding="utf-8")
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)

    assert not window.delete_component("forest-card", confirm=False)
    assert window._component_history_index == before
    assert window.tabs.indexOf(tab) >= 0
    assert (root / "components" / "forest-card.yaml").exists()
    assert errors


def test_ui_layout_preferences_are_local_and_restored(tmp_path: Path, qtbot) -> None:
    root, window, _service = make_window(tmp_path, qtbot)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    state = UiState(settings)
    window.right_tabs.setCurrentIndex(1)
    state.save(window)
    settings.sync()

    other = MainWindow(ProjectController(project_service()))
    other.ui_state = state
    other.ui_state.restore(other)
    assert other.right_tabs.currentIndex() == 1
    assert (root / "project.yaml").exists()
    assert ".componentpress" not in (root / "project.yaml").read_text(encoding="utf-8")


def test_component_dialog_keeps_fractional_existing_size_and_lists_presets(qtbot) -> None:
    component = project_service().projects.open(DEMO).documents["forest-card"].model.model_copy(
        update={"size_mm": project_service().projects.open(DEMO).documents["forest-card"].model.size_mm.model_copy(
            update={"width": 63.25, "height": 88.5}
        )}
    )
    dialog = ComponentDialog(component=component)
    qtbot.addWidget(dialog)

    assert dialog.width.value() == 63.25
    assert dialog.height.value() == 88.5
    assert dialog.definition(component)["width"] == 63.25
    assert dialog.size.count() >= 15
