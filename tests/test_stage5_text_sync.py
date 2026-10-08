"""Independent behavioural checks for stage-five text synchronization."""

from pathlib import Path
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from componentpress.application.identifiers import IdentifierGenerator
from componentpress.bootstrap import project_service
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import ImageNode
from componentpress.domain.tree import locations
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io import migrations
from componentpress.project_io.yaml_codec import parse_component


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def copy_example(tmp_path: Path) -> Path:
    root = tmp_path / "Этап 5"
    shutil.copytree(EXAMPLE, root)
    return root


def make_window(root: Path, qtbot) -> MainWindow:
    service = project_service()
    window = MainWindow(ProjectController(service))
    window.load_session(service.open_session(root))
    qtbot.addWidget(window)
    window.show()
    assert window.open_component("forest-card") is not None
    return window


def test_legacy_project_is_rejected_without_migration_or_writes(tmp_path: Path) -> None:
    root = copy_example(tmp_path)
    project = root / "project.yaml"
    component = root / "components/forest-card.yaml"
    project.write_text(project.read_text(encoding="utf-8").replace("schema_version: 4", "schema_version: 2"), encoding="utf-8")
    originals = (project.read_bytes(), component.read_bytes())
    repository = FileProjectRepository()
    try:
        repository.open(root)
    except ProjectError as exc:
        assert exc.diagnostic.code == "SCHEMA_UNSUPPORTED"
    else:
        raise AssertionError("старый формат должен быть отклонён")
    assert (project.read_bytes(), component.read_bytes()) == originals
    assert not (root / ".componentpress/migrations").exists()


def test_legacy_migration_entry_point_is_read_only(tmp_path: Path, monkeypatch) -> None:
    root = copy_example(tmp_path)
    project = root / "project.yaml"
    component = root / "components/forest-card.yaml"
    project.write_text(project.read_text(encoding="utf-8").replace("schema_version: 4", "schema_version: 1"), encoding="utf-8")
    component.write_text(component.read_text(encoding="utf-8").replace("schema_version: 4", "schema_version: 1"), encoding="utf-8")
    originals = (project.read_bytes(), component.read_bytes())
    try:
        migrations.migrate_v1_to_v2(root)
    except ProjectError as exc:
        assert exc.diagnostic.code == "SCHEMA_UNSUPPORTED"
    else:
        raise AssertionError("legacy schema must not migrate")
    assert (project.read_bytes(), component.read_bytes()) == originals
    assert not (root / migrations.ACTIVE_JOURNAL).exists()


def test_identifier_generator_retries_collision_and_keeps_same_names_independent() -> None:
    tokens = iter(("deadbeef", "cafebabe", "01234567"))
    generator = IdentifierGenerator(lambda: next(tokens))
    first = generator.create("Карта", {"component-deadbeef"}, prefix="component")
    second = generator.create("Карта", {"component-deadbeef", first}, prefix="component")
    assert first == "component-cafebabe"
    assert second == "component-01234567"


def test_text_and_layout_are_synchronized_with_one_undo_step(tmp_path: Path, qtbot) -> None:
    root = copy_example(tmp_path)
    window = make_window(root, qtbot)
    tab = window.active_tab
    assert tab is not None and window.session is not None
    original = window.session.documents["forest-card"].model
    assert locations(original)["leaf-art"].node.x_mm == 4

    window._request_mode(tab, "text")
    edited = tab.text_editor.toPlainText().replace("schema_version: 4", "schema_version: 4\n# Новый комментарий")
    edited = edited.replace("    x_mm: 4\n", "    x_mm: 7\n", 1)
    tab.text_editor.setPlainText(edited)
    assert locations(window.session.documents["forest-card"].model)["leaf-art"].node.x_mm == 4
    assert window._apply_draft("forest-card")
    assert locations(window.session.documents["forest-card"].model)["leaf-art"].node.x_mm == 7
    assert tab.undo_stack.count() == 1

    window._request_mode(tab, "layout")
    window._set_selection(("leaf-art",), source="tree")
    window._move_selected(("leaf-art",), 1, 0)
    current = window.session.documents["forest-card"].committed
    assert locations(current.model)["leaf-art"].node.x_mm == 8
    assert "# Новый комментарий" in current.text
    reparsed, _ = parse_component(current.text, current.path)
    assert locations(reparsed)["leaf-art"].node.x_mm == 8
    window.undo_group.undo()
    assert locations(window.session.documents["forest-card"].model)["leaf-art"].node.x_mm == 7
    assert "# Новый комментарий" in window.session.documents["forest-card"].committed.text
    window.undo_group.redo()
    assert locations(window.session.documents["forest-card"].model)["leaf-art"].node.x_mm == 8
    window._clear_tabs(discard=True)


def test_invalid_draft_keeps_last_model_and_blocks_save(tmp_path: Path, qtbot) -> None:
    root = copy_example(tmp_path)
    window = make_window(root, qtbot)
    tab = window.active_tab
    assert tab is not None and window.session is not None
    before = window.session.documents["forest-card"].model
    window._request_mode(tab, "text")
    initial = tab.text_editor.toPlainText()
    tab.text_editor.insertPlainText("# local undo\n")
    qtbot.keyClick(tab.text_editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert tab.text_editor.toPlainText() == initial
    assert tab.undo_stack.count() == 0
    tab.text_editor.setPlainText("schema_version: 4\nelements: [")
    document = window.session.documents["forest-card"]
    assert document.model == before
    assert document.has_invalid_draft
    assert tab.diagnostic.isVisible()
    assert window.element_tree.model() is not None
    assert not window.element_tree.isEnabled()
    assert not window.save_active()
    assert (root / "components/forest-card.yaml").read_text(encoding="utf-8") == document.source.text
    window._clear_tabs(discard=True)


def test_same_names_get_unique_ids_and_rename_keeps_identity(tmp_path: Path, qtbot) -> None:
    root = copy_example(tmp_path)
    window = make_window(root, qtbot)
    assert window.session is not None and window.active_tab is not None
    assert window.add_image("assets/images/animals/enemy/leaf.png", 1, 1, name="Повтор")
    assert window.add_image("assets/images/animals/enemy/leaf.png", 2, 2, name="Повтор")
    added = [node for node in window.session.documents["forest-card"].model.elements if isinstance(node, ImageNode) and node.name == "Повтор"]
    assert len(added) == 2
    assert added[0].id != added[1].id
    original_id = added[0].id
    window._set_selection((original_id,), source="tree")
    window._edit_selected_node({"name": "Новое имя"})
    assert locations(window.session.documents["forest-card"].model)[original_id].node.name == "Новое имя"
    window.undo_group.undo()
    assert locations(window.session.documents["forest-card"].model)[original_id].node.name == "Повтор"
    window.undo_group.redo()
    assert locations(window.session.documents["forest-card"].model)[original_id].node.name == "Новое имя"

    assert window.add_component("Одинаковое имя")
    first = window.active_tab.component_id
    assert window.add_component("Одинаковое имя")
    second = window.active_tab.component_id
    assert first != second
    assert (root / f"components/{first}.yaml").is_file()
    assert (root / f"components/{second}.yaml").is_file()
    window._clear_tabs(discard=True)


def test_external_edit_blocks_saving_applied_text(tmp_path: Path, qtbot, monkeypatch) -> None:
    root = copy_example(tmp_path)
    window = make_window(root, qtbot)
    tab = window.active_tab
    assert tab is not None
    window._request_mode(tab, "text")
    tab.text_editor.setPlainText(tab.text_editor.toPlainText() + "# Локальная правка\n")
    assert window._apply_draft("forest-card")
    path = root / "components/forest-card.yaml"
    external = path.read_text(encoding="utf-8") + "# Внешняя правка\n"
    path.write_text(external, encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *args, **kwargs: None))
    assert not window.save_active()
    assert path.read_text(encoding="utf-8") == external
    window._clear_tabs(discard=True)
