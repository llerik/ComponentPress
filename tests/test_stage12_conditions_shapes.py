"""Stage-12 behaviour: shared schema, conditional trees and printed geometry."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from pydantic import ValidationError

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.bindings.resolver import BindingResolver
from componentpress.bindings.conditions import condition_matches
from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, LineNode, ShapeNode
from componentpress.domain.project import DataSource, ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.domain.tree import add_node, move_nodes, node_bounds, resize_line_endpoint
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.schema_checks import preflight_project_schemas
from componentpress.project_io.yaml_codec import parse_component
from componentpress.rendering.exporter import ensure_gui_application, render_component_image
from componentpress.rendering.painter import paint_component
from componentpress.rendering.resources import ProjectResourceLoader
from componentpress.editor.canvas import ComponentCanvas


ROOT = Path(__file__).resolve().parents[1]


def _models(elements=()):
    project = ProjectDefinition.model_validate({
        "schema_version": 8, "id": "test", "name": "Test", "version": "0.1.0",
        "data_source": {"path": "data.xlsx"},
    })
    component = ComponentDefinition.model_validate({
        "schema_version": 8, "id": "card", "name": "Card", "size_mm": {"width": 30, "height": 20},
        "data": {"sheet": "Sheet1"}, "elements": list(elements),
    })
    return project, component


def _sheet(rows=()):
    return DataSheetSnapshot(
        "main", Path("data.xlsx"), "Sheet1", "hash", 1, ("Тип", "Пустой"), tuple(rows)
    )


def _row(number: int, value, *, state=None):
    return DataRow(number, str(number), 1, {
        "Тип": CellValue(value, f"A{number}", state) if state is not None else CellValue(value, f"A{number}"),
        "Пустой": CellValue(None, f"B{number}"),
    })


def _condition(children=()):
    return ConditionalGroupNode(
        id="conditional", type="conditional_group", name="If type", x_mm=0, y_mm=0,
        children=tuple(children), condition_column="Тип", condition_value="да",
    )


def test_yaml_round_trip_accepts_schema_five_and_nested_stage12_nodes(tmp_path: Path) -> None:
    text = '''schema_version: 8
id: card
name: Card
size_mm: {width: 30, height: 20}
data: {sheet: Sheet1}
elements:
  - type: conditional_group
    id: conditional
    name: If type
    x_mm: 1
    y_mm: 1
    condition_column: Тип
    condition_value: "да"
    children:
      - type: rectangle
        id: rectangle
        name: Rectangle
        x_mm: 1
        y_mm: 2
        width_mm: 5
        height_mm: 4
        fill: "#FF0000"
      - type: line
        id: line
        name: Line
        x_mm: 0
        y_mm: 0
        dx_mm: -2
        dy_mm: 3
'''
    model, _tree = parse_component(text, tmp_path / "card.yaml")
    assert model.schema_version == SCHEMA_VERSION == 8
    assert isinstance(model.elements[0], ConditionalGroupNode)
    assert isinstance(model.elements[0].children[0], ShapeNode)
    assert isinstance(model.elements[0].children[1], LineNode)


def test_condition_uses_exact_stable_cell_text_and_keeps_hidden_children_validated() -> None:
    project, component = _models([_condition()])
    resolver = BindingResolver(Path.cwd(), project, validate_resources=False)
    data = _sheet((_row(2, "да"), _row(3, " Да"), _row(4, True)))
    matches = [
        resolver.resolve_component(component, data, row).component.elements[0].condition_matches
        for row in data.rows
    ]
    assert matches == [True, False, False]


@pytest.mark.parametrize(
    ("operator", "value", "empty", "expected", "result"),
    [
        ("equals", " да", False, " да", True),
        ("not_equals", " Да", False, "да", True),
        ("is_empty", "", True, None, True),
        ("is_not_empty", "0", False, None, True),
        ("is_not_empty", "false", False, None, True),
        ("is_not_empty", " ", False, None, True),
        ("is_empty", "0", False, None, False),
        ("is_empty", "false", False, None, False),
    ],
)
def test_pure_condition_rule_preserves_exact_text_and_empty_values(
    operator: str, value: str, empty: bool, expected: str | None, result: bool,
) -> None:
    assert condition_matches(operator, value, empty, expected) is result


def test_column_error_is_reported_on_empty_sheet_and_valid_column_has_no_row_state() -> None:
    project, component = _models([_condition()])
    resolver = BindingResolver(Path.cwd(), project, validate_resources=False)
    missing = _condition()
    missing = missing.model_copy(update={"condition_column": "Нет такого столбца"})
    with pytest.raises(ProjectError) as error:
        resolver.resolve_component(component.model_copy(update={"elements": (missing,)}), _sheet(), None)
    assert error.value.diagnostic.code == "COLUMN_UNKNOWN"

    empty = _sheet()
    resolved_empty = resolver.resolve_component(component, empty, None).component.elements[0]
    assert resolved_empty.condition_matches is None
    with pytest.raises(ProjectError) as no_row:
        resolver.resolve_component(component, None, None)
    assert no_row.value.diagnostic.code == "DATA_NOT_LOADED"

    # A valid empty worksheet has a checked header separately from selecting a row.
    row = _row(2, "да")
    resolved = resolver.resolve_component(component, _sheet((row,)), row).component.elements[0]
    assert resolved.condition_matches is True


def test_nested_visibility_is_resolved_without_mutating_the_source_model() -> None:
    inner = ConditionalGroupNode(
        id="inner", type="conditional_group", name="Inner", x_mm=1, y_mm=1,
        condition_column="Тип", condition_value="да", children=(),
    )
    outer = _condition((inner,))
    project, component = _models([outer])
    row = _row(2, "нет")
    resolved = BindingResolver(Path.cwd(), project, validate_resources=False).resolve_component(component, _sheet((row,)), row)
    assert component.elements[0].condition_matches is None
    assert resolved.component.elements[0].condition_matches is False
    assert resolved.component.elements[0].children[0].condition_matches is False


def test_shape_and_signed_line_bounds_move_resize_and_render_consistently(tmp_path: Path) -> None:
    ensure_gui_application()
    project, component = _models([
        {"type": "rectangle", "id": "rect", "name": "Rect", "x_mm": 2, "y_mm": 2,
         "width_mm": 8, "height_mm": 6, "fill": "#FFFF0000", "stroke": "#FF000000"},
        {"type": "ellipse", "id": "ellipse", "name": "Ellipse", "x_mm": 12, "y_mm": 2,
         "width_mm": 8, "height_mm": 6, "fill": "#FF00FF00", "stroke_style": "dot"},
        {"type": "line", "id": "line", "name": "Line", "x_mm": 20, "y_mm": 14,
         "dx_mm": -8, "dy_mm": -6, "stroke": "#FF0000FF", "stroke_style": "dash"},
    ])
    assert node_bounds(component, "line") == pytest.approx((12, 8, 8, 6))
    shifted = move_nodes(component, ("line",), 1, 2)
    assert node_bounds(shifted, "line") == pytest.approx((13, 10, 8, 6))
    resized = resize_line_endpoint(shifted, "line", 0, -4)
    assert resized.elements[2].dx_mm == 0
    assert resized.elements[2].dy_mm == -4

    snapshot = ProjectSnapshot(
        tmp_path, "", "", project,
        {"card": DocumentSnapshot(tmp_path / "card.yaml", "", "", component)},
    )
    rendered = render_component_image(snapshot, "card", component=component, dpi=96)
    assert rendered.pixelColor(round(5 * 96 / 25.4), round(5 * 96 / 25.4)).red() > 240
    assert rendered.pixelColor(round(15 * 96 / 25.4), round(5 * 96 / 25.4)).green() > 230
    assert rendered.pixelColor(round(16 * 96 / 25.4), round(11 * 96 / 25.4)).blue() > 180


def test_lines_do_not_shift_later_shapes_or_their_canvas_hit_targets(tmp_path: Path, qtbot) -> None:
    ensure_gui_application()
    project, component = _models([
        {"type": "line", "id": "line", "name": "Line", "x_mm": 10, "y_mm": 10,
         "dx_mm": 5, "dy_mm": 0, "stroke": "#FF00FF",
         "transform": {"m11": 1, "m12": 0.3, "m21": 0.2, "m22": 1}},
        {"type": "rectangle", "id": "yellow", "name": "Yellow", "x_mm": 30, "y_mm": 10,
         "width_mm": 10, "height_mm": 10, "fill": "#FFFF00"},
        {"type": "group", "id": "group", "name": "Group", "x_mm": 5, "y_mm": 50,
         "children": [
             {"type": "line", "id": "nested-line", "name": "Nested line", "x_mm": 2, "y_mm": 2,
              "dx_mm": 8, "dy_mm": 0, "stroke": "#FF00FF",
              "transform": {"m11": 1, "m12": 0.2, "m21": 0.1, "m22": 1}},
             {"type": "rectangle", "id": "nested-yellow", "name": "Nested yellow", "x_mm": 15, "y_mm": 2,
              "width_mm": 5, "height_mm": 5, "fill": "#FFFF00"},
         ]},
        {"type": "rectangle", "id": "green", "name": "Green", "x_mm": 50, "y_mm": 10,
         "width_mm": 5, "height_mm": 5, "fill": "#00FF00"},
    ])
    component = component.model_copy(update={"size_mm": SizeMM(width=100, height=100)})
    image = render_component_image(
        ProjectSnapshot(tmp_path, "", "", project, {
            "card": DocumentSnapshot(tmp_path / "card.yaml", "", "", component),
        }),
        "card", component=component, dpi=96, bindings_resolved=True, register_fonts=False,
    )
    scale = 96 / 25.4
    assert image.pixelColor(round(35 * scale), round(15 * scale)) == QColor("#FFFF00")
    assert image.pixelColor(round(45 * scale), round(25 * scale)) != QColor("#FFFF00")
    assert image.pixelColor(round(21 * scale), round(54 * scale)) == QColor("#FFFF00")
    assert image.pixelColor(round(26 * scale), round(58 * scale)) != QColor("#FFFF00")
    assert image.pixelColor(round(52 * scale), round(12 * scale)) == QColor("#00FF00")

    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(700, 700)
    canvas.set_document(image, component)
    canvas.show()
    qtbot.waitExposed(canvas)
    click = canvas.mapFromScene(QPointF(35 * scale, 15 * scale))
    QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=click)
    assert "yellow" in canvas.selected_ids

    caller_image = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(caller_image)
    baseline = painter.worldTransform()
    painter.save()
    painter.translate(3, 4)
    paint_component(
        painter, component, ProjectResourceLoader(tmp_path), tmp_path / "card.yaml", {},
        bindings_resolved=True,
    )
    painter.restore()
    assert painter.worldTransform() == baseline
    painter.end()


def test_schema_preflight_rejects_old_referenced_and_journal_documents_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    component = root / "components" / "forest-card.yaml"
    component.write_text(component.read_text(encoding="utf-8").replace("schema_version: 8", "schema_version: 4", 1), encoding="utf-8")
    journal_dir = root / ".componentpress" / "transactions"
    journal_dir.mkdir(parents=True)
    journal = journal_dir / "add-component.json"
    journal.write_text(json.dumps({"component": "components/forest-card.yaml"}), encoding="utf-8")
    files = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ProjectError) as caught:
        FileProjectRepository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == files
    assert not (root / ".componentpress" / "locks").exists()


def test_schema_rejects_previous_project_version_before_migrate_can_create_lock(tmp_path: Path) -> None:
    root = tmp_path / "game"
    shutil.copytree(ROOT / "examples" / "demo-game", root)
    project = root / "project.cpress"
    project.write_text(project.read_text(encoding="utf-8").replace("schema_version: 8", "schema_version: 4", 1), encoding="utf-8")
    original = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ProjectError) as caught:
        FileProjectRepository().migrate(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()} == original
    assert not (root / ".componentpress" / "locks").exists()
