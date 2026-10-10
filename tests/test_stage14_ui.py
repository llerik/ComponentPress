"""Behavioral coverage for the compact stage-14 editor shell."""

from pathlib import Path
import shutil

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox

from componentpress.bootstrap import project_service
from componentpress.domain.nodes import ImageNode
from componentpress.editor.component_dialog import ComponentDialog
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.editor.ui_state import UiState


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def make_window(tmp_path: Path, qtbot):
    root = tmp_path / "game"
    shutil.copytree(DEMO, root)
    service = project_service()
    service.register(root, "second", "Второй компонент")
    window = MainWindow(ProjectController(service))
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    return root, window, service


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
