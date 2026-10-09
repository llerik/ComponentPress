"""Independent acceptance checks for the stage-three desktop shell."""

from pathlib import Path
import runpy
import shutil
import sys

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QMessageBox

from componentpress.bootstrap import project_service
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.editor.qt_models import ID_ROLE
from componentpress.gui import _run_release_smoke
from componentpress.project_io.repository import FileProjectRepository
from componentpress.rendering import render_component


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def demo(tmp_path: Path) -> Path:
    root = tmp_path / "Игра с пробелом"
    shutil.copytree(EXAMPLE, root)
    service = project_service()
    service.register(root, "event-card", "Карта события")
    return root


def window_for(root: Path, qtbot) -> MainWindow:
    service = project_service()
    window = MainWindow(ProjectController(service))
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    return window


def test_sessions_keep_component_edits_independent_and_save_comments(tmp_path: Path) -> None:
    root = demo(tmp_path)
    service = project_service()
    session = service.open_session(root)
    forest = session.documents["forest-card"]
    event = session.documents["event-card"]
    event.update(name="Изменённое событие", size_mm=event.model.size_mm.model_copy(update={"width": 70.0}))
    assert forest.model.name == "Лесная карта"
    assert forest.model.size_mm.width == 63
    assert event.dirty and not forest.dirty
    service.save_document(session, "event-card")
    assert not event.dirty
    assert "# Статический макет с вложенной группой" in (root / "components/forest-card.yaml").read_text(encoding="utf-8")
    reopened = FileProjectRepository().open(root)
    assert reopened.documents["event-card"].model.name == "Изменённое событие"
    assert reopened.documents["event-card"].model.size_mm.width == 70


def test_main_window_opens_unique_tabs_and_shows_front_layer_first(tmp_path: Path, qtbot) -> None:
    root = demo(tmp_path)
    window = window_for(root, qtbot)
    first = window.open_component("forest-card")
    assert first is not None
    assert window.tabs.count() == 1
    assert window.open_component("forest-card") is first
    assert window.tabs.count() == 1
    window.open_component("event-card")
    assert window.tabs.count() == 2
    window.tabs.setCurrentWidget(first)
    model = window.element_tree.model()
    assert model.index(0, 0).data(ID_ROLE) == "text-group"
    assert model.index(1, 0).data(ID_ROLE) == "leaf-art"
    group = model.index(0, 0)
    assert model.index(0, 0, group).data(ID_ROLE) == "caption"
    assert model.index(1, 0, group).data(ID_ROLE) == "heading"


def test_properties_dirty_marker_save_restart_and_export(tmp_path: Path, qtbot) -> None:
    root = demo(tmp_path)
    window = window_for(root, qtbot)
    forest_tab = window.open_component("forest-card")
    event_tab = window.open_component("event-card")
    assert forest_tab is not None and event_tab is not None
    window.tabs.setCurrentWidget(forest_tab)
    forest_zoom = forest_tab.canvas.zoom_percent
    forest_tab.canvas.actual_size()
    window.tabs.setCurrentWidget(event_tab)
    event_tab.canvas.zoom_by(1.2)
    assert forest_tab.canvas.zoom_percent == 100
    assert event_tab.canvas.zoom_percent != forest_tab.canvas.zoom_percent
    window.properties.name_edit.setText("Событие осени")
    window.properties.name_edit.editingFinished.emit()
    window.properties.width_spin.setValue(70.0)
    window.properties.height_spin.setValue(90.0)
    window.properties.width_spin.editingFinished.emit()
    window.properties.background_edit.setText("#ABCDEF")
    window.properties.background_edit.editingFinished.emit()
    assert window.session is not None
    assert window.session.documents["event-card"].dirty
    assert window.session.documents["forest-card"].model.size_mm.width == 63
    assert window.tabs.tabText(window.tabs.indexOf(event_tab)).endswith("*")
    assert window.save_active()
    assert not window.session.documents["event-card"].dirty
    reopened = project_service().open_session(root)
    assert reopened.documents["event-card"].model.name == "Событие осени"
    assert reopened.documents["event-card"].model.size_mm.width == 70
    assert reopened.documents["event-card"].model.size_mm.height == 90
    assert reopened.documents["event-card"].model.background == "#ABCDEF"
    output = tmp_path / "event.png"
    render_component(reopened.snapshot, "event-card", output, dpi=254)
    image = QImage(str(output))
    assert image.size().width() == 700
    assert image.size().height() == 900
    assert image.pixelColor(10, 10) == QColor("#ABCDEF")
    assert forest_zoom > 0


def test_add_empty_component_and_discard_unsaved_close(tmp_path: Path, qtbot, monkeypatch) -> None:
    root = demo(tmp_path)
    window = window_for(root, qtbot)
    assert window.add_component("token", "Жетон")
    assert window.session is not None
    assert "token" in window.session.documents
    assert window.active_tab is not None
    assert window.active_tab.component_id == "token"
    window.properties.name_edit.setText("Несохранённое имя")
    window.properties.name_edit.editingFinished.emit()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Cancel),
    )
    index = window.tabs.currentIndex()
    assert not window.close_tab(index)
    assert window.tabs.currentIndex() == index
    assert window.session.documents["token"].dirty
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard),
    )
    assert window.close_tab(index)
    assert not window.session.documents["token"].dirty
    reopened = window.open_component("token")
    assert reopened is not None
    assert window.session.documents["token"].model.name == "Жетон"


def test_create_project_through_window_method(tmp_path: Path, qtbot) -> None:
    root = tmp_path / "Новый интерфейсный проект"
    service = project_service()
    window = MainWindow(ProjectController(service))
    qtbot.addWidget(window)
    assert window.create_path(root, "Новая игра")
    assert window.session is not None
    assert window.session.snapshot.model.name == "Новая игра"
    assert window.add_component("card", "Карта")
    assert (root / "components/card.yaml").exists()


def test_module_without_arguments_dispatches_to_gui(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("componentpress.gui.main", lambda: called.append(True) or 0)
    monkeypatch.setattr(sys, "argv", ["componentpress"])
    try:
        runpy.run_module("componentpress.__main__", run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    assert called == [True]


def test_release_smoke_exercises_saved_gui_workflow(tmp_path: Path, qtbot) -> None:
    root = demo(tmp_path)
    window = MainWindow(ProjectController(project_service()))
    qtbot.addWidget(window)
    assert _run_release_smoke(window, root)
    reopened = FileProjectRepository().open(root)
    component = reopened.documents["release-smoke"].model
    assert component.size_mm == component.size_mm.model_copy(update={"width": 70.0, "height": 90.0})
    assert component.background == "#DDEEFF"
    assert "# Проверка текстового режима" in reopened.documents["release-smoke"].text
    assert component.elements[0].id == "smoke-group"
    assert {node.type for node in component.elements}.issuperset({"group", "rectangle", "ellipse", "line"})
