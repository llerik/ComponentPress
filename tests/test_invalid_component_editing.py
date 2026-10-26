"""Recovery behavior for readable component YAML that fails validation."""

from pathlib import Path
import shutil

import pytest

from componentpress.bootstrap import project_service
from componentpress.domain.diagnostics import ProjectError
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.project_io.repository import FileProjectRepository


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    return root


def test_unknown_schema_opens_for_text_and_can_be_saved_verbatim(tmp_path: Path) -> None:
    root = project(tmp_path)
    path = root / "components" / "forest-card.yaml"
    original = path.read_text(encoding="utf-8")
    invalid = original.replace("schema_version: 9", "schema_version: 7", 1)
    path.write_bytes(invalid.encode("utf-8"))

    service = project_service()
    session = service.open_session(root)
    document = session.documents["forest-card"]
    assert document.model is None
    assert document.current_diagnostics[0].code == "SCHEMA_UNSUPPORTED"
    assert document.current_text == invalid
    with pytest.raises(ProjectError):
        FileProjectRepository().open(root)

    changed = invalid + "# keep this recovery note\n"
    document.set_draft(changed, (document.current_diagnostics[0],),
                       FileProjectRepository().prepare_text(session.snapshot, "forest-card", changed))
    service.save_document(session, "forest-card")
    assert path.read_text(encoding="utf-8") == changed
    assert session.documents["forest-card"].dirty is False


def test_invalid_component_is_text_only_until_yaml_is_fixed(tmp_path: Path, qtbot) -> None:
    root = project(tmp_path)
    path = root / "components" / "forest-card.yaml"
    original = path.read_text(encoding="utf-8")
    invalid = original.replace("schema_version: 9", "schema_version: 7", 1)
    path.write_bytes(invalid.encode("utf-8"))

    service = project_service()
    window = MainWindow(ProjectController(service))
    qtbot.addWidget(window)
    window.load_session(service.open_session(root))
    window.show()
    tab = window.open_component("forest-card")
    assert tab is not None
    assert not tab.layout_mode.isChecked()
    assert not tab.layout_mode.isEnabled()
    assert len(tab.text_editor.extraSelections()) == 1

    fixed = invalid.replace("schema_version: 7", "schema_version: 9", 1)
    tab.set_text(fixed)
    window._draft_changed(tab, fixed)
    assert window.session.documents["forest-card"].model_ready
    assert not tab.text_editor.extraSelections()
    assert tab.layout_mode.isEnabled()
    window._request_mode(tab, "layout")
    assert tab.layout_mode.isChecked()
    window.session.documents["forest-card"].revert()
    window.close()
