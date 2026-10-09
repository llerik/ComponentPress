"""Behavior checks for the schema-four editing and formatting stage."""

from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError
from PySide6.QtCore import Qt

from componentpress.application.preview_service import PreviewService
from componentpress.application.edits import ensure_visual_edit_respects_locks
from componentpress.bootstrap import project_repository
from componentpress.data_sources import XlsxReader
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.domain.tree import (
    TreeOperationError,
    align_nodes,
    move_nodes,
    node_bounds,
    place_nodes,
    remove_nodes,
    update_node,
)
from componentpress.editor.properties import ElementProperties
from componentpress.project_io.yaml_codec import parse_component
from componentpress.rendering.exporter import ensure_gui_application


ROOT = Path(__file__).resolve().parents[1]


def _component() -> ComponentDefinition:
    return ComponentDefinition(
        schema_version=5,
        id="card",
        name="Card",
        size_mm=SizeMM(width=63, height=88),
        elements=(
            ImageNode(id="back", name="Back", type="image", x_mm=2, y_mm=3, width_mm=10, height_mm=8, source="assets/images/animals/enemy/leaf.png"),
            ImageNode(id="front", name="Front", type="image", x_mm=20, y_mm=15, width_mm=5, height_mm=12, source="assets/images/animals/enemy/leaf.png"),
            GroupNode(
                id="group", name="Group", type="group", x_mm=40, y_mm=35,
                children=(ImageNode(id="child", name="Child", type="image", x_mm=3, y_mm=4, width_mm=8, height_mm=5, source="assets/images/animals/enemy/leaf.png"),),
            ),
        ),
    )


def test_project_and_new_component_templates_use_shared_schema_five(tmp_path: Path) -> None:
    repository = project_repository()
    snapshot = repository.create(tmp_path / "game", "Game")
    assert snapshot.model.schema_version == 5
    snapshot = repository.add_component(snapshot, "card", "Card")
    assert snapshot.documents["card"].model.schema_version == 5
    assert snapshot.model.schema_version == snapshot.documents["card"].model.schema_version == SCHEMA_VERSION == 5
    assert "schema_version: 5" in snapshot.documents["card"].text


@pytest.mark.parametrize("version", [1, 2, 3, 4, 6, "5", "true", "5.0", 5.0])
def test_old_or_mistyped_project_schema_is_rejected_without_rewriting(tmp_path: Path, version: object) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    project = root / "project.yaml"
    source = project.read_text(encoding="utf-8")
    scalar = repr(version) if isinstance(version, str) else str(version)
    if version == "true":
        scalar = "true"
    scalar = "true" if version == "true" else scalar
    project.write_text(source.replace("schema_version: 5", f"schema_version: {scalar}", 1), encoding="utf-8")
    original = project.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert project.read_bytes() == original


def test_old_component_schema_is_rejected_without_rewriting(tmp_path: Path) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    component = root / "components" / "forest-card.yaml"
    component.write_text(component.read_text(encoding="utf-8").replace("schema_version: 5", "schema_version: 4", 1), encoding="utf-8")
    original = component.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert component.read_bytes() == original


@pytest.mark.parametrize("model_type", [ProjectDefinition, ComponentDefinition])
@pytest.mark.parametrize("value", [5.0, "5", True, False, 4, 6])
def test_document_models_require_schema_version_to_be_strict_integer_five(model_type, value) -> None:
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
    text = f'''schema_version: 5
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
    diagnostics = PreviewService(XlsxReader()).validate_all(snapshot)
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


def test_lock_inheritance_guards_visual_edits_but_allows_sibling_reordering() -> None:
    component = _component().model_copy(update={
        "elements": (
            ImageNode(id="free", name="Free", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/animals/enemy/leaf.png"),
            ImageNode(id="locked", name="Locked", type="image", x_mm=4, y_mm=1, width_mm=2, height_mm=2, source="assets/images/animals/enemy/leaf.png", locked=True),
            GroupNode(id="parent", name="Parent", type="group", x_mm=10, y_mm=10, locked=True, children=(
                ImageNode(id="inherited", name="Inherited", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/animals/enemy/leaf.png"),
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


def test_visual_lock_guard_allows_layer_crossing_and_preserves_yaml_edit_path() -> None:
    component = ComponentDefinition(
        schema_version=5, id="card", name="Card", size_mm=SizeMM(width=63, height=88),
        elements=(
            ImageNode(id="locked", name="Locked", type="image", x_mm=1, y_mm=1, width_mm=2, height_mm=2, source="assets/images/animals/enemy/leaf.png", locked=True),
            ImageNode(id="free", name="Free", type="image", x_mm=4, y_mm=1, width_mm=2, height_mm=2, source="assets/images/animals/enemy/leaf.png"),
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

    yaml_text = '''schema_version: 5
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
    source: {mode: manual, path: assets/images/animals/enemy/leaf.png}
  - id: free
    name: Free
    type: image
    x_mm: 4
    y_mm: 1
    width_mm: 2
    height_mm: 2
    source: {mode: manual, path: assets/images/animals/enemy/leaf.png}
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
