from pathlib import Path

import pytest
from openpyxl import Workbook
from PySide6.QtGui import QImage, QColor, QPainter
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt

from componentpress.data_sources.xlsx_reader import XlsxReader
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.nodes import GroupNode, ShapeNode, ImageNode
from componentpress.domain.project import PrintSettings
from componentpress.domain.tree import group_nodes, locations, resize_node, rotate_node, ungroup_node
from componentpress.build.imposition import calculate_imposition
from componentpress.build.input_snapshot import BuildInstance
from componentpress.resources.repository import ResourceRepository
from componentpress.editor.canvas.view import ComponentCanvas, NodeOverlay
from componentpress.project_io.yaml_codec import dump_yaml, parse_component, update_tree
from componentpress.domain.values import FormulaState
from ruamel.yaml.comments import CommentedMap


def _component(elements=(), *, component_id="card", width=63, height=88):
    return ComponentDefinition(schema_version=6, id=component_id, name=component_id,
                               size_mm=SizeMM(width=width, height=height), elements=tuple(elements))


def _png(path: Path, color: str = "#FF0000") -> bytes:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), "PNG")
    return path.read_bytes()


def test_group_resize_rotate_ungroup_preserves_children_world_geometry():
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=4, y_mm=7,
                                     width_mm=8, height_mm=6),))
    before = locations(component)["shape"].world
    grouped = group_nodes(component, ["shape"], "group", "Group")
    assert grouped.elements[0].frame.width_mm > 0
    resized = resize_node(grouped, "group", 16, 12)
    resized_again = resize_node(resized, "group", 16, 12)
    assert locations(resized_again)["group"].world.a == pytest.approx(locations(resized)["group"].world.a)
    assert resized.elements[0].transform.m11 == pytest.approx(2.0)
    rotated = rotate_node(resized, "group", 35)
    ungrouped = ungroup_node(rotated, "group")
    child = locations(ungrouped)["shape"].world
    assert child.a == pytest.approx(2 * before.a * 0.819152, abs=0.02)
    assert child.d == pytest.approx(2 * before.d * 0.819152, abs=0.02)
    assert ungrouped.elements[0].transform.m11 == pytest.approx(child.a)


@pytest.mark.parametrize("anchor", [(x, y) for x in (0.0, 0.5, 1.0) for y in (0.0, 0.5, 1.0) if (x, y) != (0.5, 0.5)])
def test_resize_anchor_is_stationary_for_affine_node(anchor):
    node = ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=4, y_mm=7, width_mm=8, height_mm=6,
                     transform={"m11": 0.8, "m12": -0.6, "m21": 0.6, "m22": 0.8})
    component = _component((node,))
    before = locations(component)["shape"].world.point(node.width_mm * anchor[0], node.height_mm * anchor[1])
    resized = resize_node(component, "shape", 16, 12, anchor)
    after_node = resized.elements[0]
    after = locations(resized)["shape"].world.point(after_node.width_mm * anchor[0], after_node.height_mm * anchor[1])
    assert after == pytest.approx(before)


def test_selected_canvas_node_exposes_eight_screen_space_handles(qtbot):
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=4, y_mm=7,
                                     width_mm=8, height_mm=6, transform={"m11": 0.8, "m12": -0.6, "m21": 0.6, "m22": 0.8}),))
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.set_document(QImage(630, 880, QImage.Format.Format_ARGB32), component, selected=("shape",))
    overlay = canvas._overlays["shape"]
    assert isinstance(overlay, NodeOverlay)
    assert len(overlay.handle_points()) == 8
    assert [canvas._handle_hit(overlay, QPointF(point)) for point in canvas._handle_centers(overlay)] == list(range(8))


@pytest.mark.parametrize("handle,anchor", [(0,(1,1)), (1,(0.5,1)), (2,(0,1)), (3,(0,0.5)), (4,(0,0)), (5,(0.5,0)), (6,(1,0)), (7,(1,0.5))])
def test_canvas_resize_handles_keep_the_opposite_anchor(handle, anchor, qtbot):
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20,
                                     width_mm=20, height_mm=30),), width=100, height=100)
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    canvas.set_document(QImage(1000, 1000, QImage.Format.Format_ARGB32), component, selected=("shape",))
    overlay = canvas._overlays["shape"]
    requested = []
    canvas.resizeRequested.connect(lambda *args: requested.append(args))
    point = canvas._handle_centers(overlay)[handle].toPoint()
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert requested[-1][0] == "shape"
    assert requested[-1][3:] == pytest.approx(anchor)


def test_resize_target_extends_ten_pixels_around_cube_and_uses_screen_space(qtbot):
    node = ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20, width_mm=20, height_mm=30,
                     transform={"m11":0, "m12":-1, "m21":1, "m22":0})
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    canvas.set_document(QImage(1000, 1000, QImage.Format.Format_ARGB32), _component((node,), width=100, height=100), selected=("shape",))
    overlay = canvas._overlays["shape"]
    center = canvas._handle_centers(overlay)[3].toPoint()
    assert canvas._handle_hit(overlay, QPointF(center.x() + 13, center.y())) == 3
    assert canvas._handle_hit(overlay, QPointF(center.x() + 15, center.y())) is None
    middle = canvas.mapFromScene(overlay.mapToScene(overlay.rect().center()))
    assert canvas._handle_hit(overlay, QPointF(middle)) is None


def test_short_object_handle_cubes_remain_separately_clickable(qtbot):
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    overlay = NodeOverlay("short", False, False, QRectF(0, 0, 0.1, 0.1), 1)
    canvas.scene().addItem(overlay)
    overlay.setSelected(True)
    centers = canvas._handle_centers(overlay)
    assert all((a - b).manhattanLength() >= 8 for i, a in enumerate(centers) for b in centers[i + 1:])
    assert [canvas._handle_hit(overlay, QPointF(center)) for center in centers] == list(range(8))


def test_dragging_right_side_resizes_around_the_stationary_left_side(qtbot):
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20,
                                     width_mm=20, height_mm=30),), width=100, height=100)
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    canvas.set_document(QImage(1000, 1000, QImage.Format.Format_ARGB32), component, selected=("shape",))
    overlay = canvas._overlays["shape"]
    requests = []
    moves = []
    canvas.resizeRequested.connect(lambda *args: requests.append(args))
    canvas.moveRequested.connect(lambda *args: moves.append(args))
    start = canvas._handle_centers(overlay)[3].toPoint()
    end = start + QPoint(-12, 0)
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert requests
    assert requests[-1][3:] == pytest.approx((0.0, 0.5))
    assert requests[-1][1] < 20
    assert requests[-1][2] == pytest.approx(30)
    assert not moves


@pytest.mark.parametrize("color,expected", [("#0B78D0", "#FFFFFF"), ("#FFFFFF", "#0B78D0")])
def test_resize_marker_contrasts_only_with_matching_preview(color, expected, qtbot):
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    image = QImage(1000, 1000, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20,
                                     width_mm=20, height_mm=30),), width=100, height=100)
    canvas.set_document(image, component, selected=("shape",))
    center = canvas._handle_centers(canvas._overlays["shape"])[0]
    rendered = QImage(canvas.viewport().size(), QImage.Format.Format_ARGB32)
    rendered.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rendered)
    canvas.drawForeground(painter, QRectF())
    painter.end()
    assert rendered.pixelColor(round(center.x()), round(center.y())) == QColor(expected)


def test_resize_marker_whitens_only_its_overlap_with_matching_preview(qtbot):
    image = QImage(1000, 1000, QImage.Format.Format_ARGB32)
    image.fill(QColor("#FFFFFF"))
    painter = QPainter(image)
    painter.fillRect(200, 200, 800, 800, QColor("#0B78D0"))
    painter.end()
    component = _component((ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=20, y_mm=20,
                                     width_mm=20, height_mm=30),), width=100, height=100)
    canvas = ComponentCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    canvas.set_document(image, component, selected=("shape",))
    center = canvas._handle_centers(canvas._overlays["shape"])[0]
    rendered = QImage(canvas.viewport().size(), QImage.Format.Format_ARGB32)
    rendered.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rendered)
    canvas.drawForeground(painter, QRectF())
    painter.end()
    pixels = [rendered.pixelColor(round(center.x() - 4 + x), round(center.y() - 4 + y)) for y in range(8) for x in range(8)]
    assert QColor("#FFFFFF") in pixels
    assert QColor("#0B78D0") in pixels


def test_excel_area_ignores_formatting_and_stops_at_first_empty_header_and_row(tmp_path):
    path = tmp_path / "rows.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ID", "Name", "   ", "ignored"])
    sheet.append([1, "zero", None, "outside"])
    sheet.append([None, None, None, "outside too"])
    sheet.append([2, "below gap", None, "ignored"])
    sheet["A500"].number_format = "0.00"
    workbook.save(path)
    original = path.read_bytes()
    snapshot = XlsxReader().read(path, "Sheet", copies_columns=("", ""))
    assert snapshot.area == "A1:B2"
    assert len(snapshot.rows) == 1
    assert path.read_bytes() == original


def test_excel_formula_with_empty_cached_result_stays_inside_table(tmp_path):
    path = tmp_path / "formula.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ID", "Calculated"])
    sheet.append(["one", "=1+1"])
    workbook.save(path)
    snapshot = XlsxReader().read(path, "Sheet")
    assert snapshot.area == "A1:B2"
    assert snapshot.rows[0].values["Calculated"].formula_state is FormulaState.EMPTY


def test_image_folder_import_flattens_names_and_rolls_back_on_collision(tmp_path):
    root = tmp_path / "project"
    (root / "assets" / "images").mkdir(parents=True)
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    _png(source / "root.png")
    _png(source / "nested" / "icon.png", "#00FF00")
    repo = ResourceRepository()
    paths = repo.import_image_folder(root, source, "cards")
    assert paths == ("assets/images/cards/root.png", "assets/images/cards/nested_icon.png")
    assert len(repo.list_images(root)) == 2
    conflict = tmp_path / "conflict"
    conflict.mkdir()
    _png(conflict / "root.png", "#0000FF")
    with pytest.raises(Exception):
        repo.import_image_folder(root, conflict, "cards")
    assert (root / "assets/images/cards/root.png").read_bytes() == (source / "root.png").read_bytes()
    assert not list((root / ".componentpress" / "resource-imports").glob("*/journal.json"))


def test_image_folder_import_rolls_back_a_partial_publish(tmp_path, monkeypatch):
    from componentpress.project_io import files

    root = tmp_path / "project"
    (root / "assets" / "images").mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    _png(source / "a.png")
    _png(source / "b.png", "#00FF00")
    real_move = files.atomic_move
    calls = 0

    def fail_second(source_path, target_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publication failure")
        return real_move(source_path, target_path)

    monkeypatch.setattr(files, "atomic_move", fail_second)
    with pytest.raises(OSError, match="injected"):
        ResourceRepository().import_image_folder(root, source)
    assert not list((root / "assets" / "images").glob("*.png"))
    assert not list((root / ".componentpress" / "transactions" / "resource-imports").glob("*.json"))


def test_optional_blank_image_round_trips_in_schema_six_yaml():
    component = _component((ImageNode(id="optional", name="Optional", type="image", x_mm=0, y_mm=0,
                                     width_mm=10, height_mm=10, source="", optional=True),))
    tree = CommentedMap()
    update_tree(tree, component)
    parsed, _ = parse_component(dump_yaml(tree), Path("component.yaml"))
    assert parsed == component


def test_image_replacement_keeps_path_and_rejects_stale_hash(tmp_path):
    root = tmp_path / "project"
    images = root / "assets/images"
    images.mkdir(parents=True)
    target = images / "portrait.png"
    old = _png(target)
    source = tmp_path / "new.png"
    new = _png(source, "#0000FF")
    repo = ResourceRepository()
    from componentpress.project_io.files import file_hash
    expected = file_hash(target)
    repo.replace_image(root, "assets/images/portrait.png", source, expected)
    assert target.read_bytes() == new and target.exists()
    with pytest.raises(Exception):
        repo.replace_image(root, "assets/images/portrait.png", source, expected)


def test_same_size_components_share_page_in_project_order():
    components = [
        ("A", 60, 88), ("B", 38, 38), ("C", 60, 88),
    ]
    instances = tuple(BuildInstance(i, name, name, name, None, 1,
                                    _component(component_id=name, width=w, height=h), Path(name), f"{name}.png")
                      for i, (name, w, h) in enumerate(components))
    plan = calculate_imposition(instances, PrintSettings())
    assert [placement.component_id for page in plan.pages for placement in page.placements] == ["A", "C", "B"]
    assert plan.pages[0].placements[0].width_mm == 60
    assert plan.pages[-1].placements[0].width_mm == 38

