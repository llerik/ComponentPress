"""Observable stage-two rendering behavior and diagnostics."""

from collections.abc import Callable
from pathlib import Path
import shutil
import re
import subprocess
import sys

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtPdf import QPdfDocument

from componentpress.domain.diagnostics import ProjectError
from componentpress.bindings.static import resolve_static
from componentpress.project_io.repository import FileProjectRepository
from componentpress.rendering import render_component
from componentpress.rendering.exporter import ensure_gui_application


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "componentpress", *args], text=True, capture_output=True, encoding="utf-8")


def demo(tmp_path: Path) -> Path:
    root = tmp_path / "Игра с пробелом"
    shutil.copytree(EXAMPLE, root)
    return root


def _save_color(path: Path, width: int, height: int, color: str) -> None:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), "PNG")


def _write_project(root: Path, elements: str, *, width: float = 10, height: float = 10) -> None:
    elements = re.sub(
        r'(?m)^(\s*)source: (.+)$',
        lambda match: f'{match.group(1)}source:\n{match.group(1)}  mode: manual\n{match.group(1)}  path: {match.group(2)}',
        elements,
    )
    root.mkdir(parents=True)
    (root / "components").mkdir()
    (root / "assets/images").mkdir(parents=True)
    (root / "project.yaml").write_text(
        "schema_version: 4\nid: test\nname: Test\nversion: 0.1.0\nvariables: {}\nicons: {}\ndata_source: null\ncopies_columns: {prod: Prod, test: Debug}\n"
        "components:\n  - id: card\n    path: components/card.yaml\n"
        "print:\n  paper: A4\n  orientation: portrait\n  margin_mm: 5\n  gap_mm: 3\n  dpi: 254\n  cut_lines: true\n  cut_line_width_mm: 0.2\n",
        encoding="utf-8",
    )
    (root / "components/card.yaml").write_text(
        f'schema_version: 4\nid: card\nname: Card\nsize_mm:\n  width: {width}\n  height: {height}\nbackground: "#FFFFFF"\nelements:\n{elements}',
        encoding="utf-8",
    )


def test_cli_renders_expected_pixels_and_physical_pdf(tmp_path: Path) -> None:
    root = demo(tmp_path)
    png = tmp_path / "вывод" / "карта.png"
    pdf = tmp_path / "вывод" / "карта.pdf"
    result = cli("render", str(root), "--component", "forest-card", "--output", str(png), "--pdf", str(pdf))
    assert result.returncode == 0, result.stderr
    assert "744x1039; 300 DPI" in result.stdout
    image = QImage(str(png))
    assert image.size() == QSize(744, 1039)
    assert round(image.dotsPerMeterX() * 0.0254) == 300
    document = QPdfDocument()
    assert document.load(str(pdf)) == QPdfDocument.Error.None_
    assert document.pageCount() == 1
    points = document.pagePointSize(0)
    # QPdfWriter stores the media box in whole PostScript points (max error < 0.18 mm).
    assert points.width() == pytest.approx(63 / 25.4 * 72, abs=0.51)
    assert points.height() == pytest.approx(88 / 25.4 * 72, abs=0.51)


def test_nested_group_order_and_component_clip(tmp_path: Path) -> None:
    root = tmp_path / "project"
    elements = """  - id: base
    name: Base
    type: image
    x_mm: 0
    y_mm: 0
    width_mm: 10
    height_mm: 10
    source: assets/images/red.png
    fit: stretch
  - id: group
    type: group
    name: Group
    x_mm: 2
    y_mm: 2
    children:
      - id: top
        name: Top
        type: image
        x_mm: 2
        y_mm: 2
        width_mm: 8
        height_mm: 8
        source: assets/images/blue.png
        fit: stretch
"""
    _write_project(root, elements)
    ensure_gui_application()
    _save_color(root / "assets/images/red.png", 2, 2, "#FF0000")
    _save_color(root / "assets/images/blue.png", 2, 2, "#0000FF")
    output = tmp_path / "nested.png"
    render_component(FileProjectRepository().open(root), "card", output)
    image = QImage(str(output))  # 254 DPI makes 1 mm exactly 10 px.
    assert image.pixelColor(10, 10) == QColor("#FF0000")
    assert image.pixelColor(50, 50) == QColor("#0000FF")
    assert image.pixelColor(99, 99) == QColor("#0000FF")  # child is clipped at the component edge


@pytest.mark.parametrize(
    ("fit", "top", "middle"),
    [("contain", "#FFFFFF", "#00AA00"), ("stretch", "#00AA00", "#00AA00"), ("cover", "#00AA00", "#00AA00")],
)
def test_image_fit_modes(fit: str, top: str, middle: str, tmp_path: Path) -> None:
    root = tmp_path / fit
    elements = f"""  - id: picture
    name: Picture
    type: image
    x_mm: 0
    y_mm: 0
    width_mm: 10
    height_mm: 10
    source: assets/images/wide.png
    fit: {fit}
"""
    _write_project(root, elements)
    ensure_gui_application()
    _save_color(root / "assets/images/wide.png", 20, 10, "#00AA00")
    output = tmp_path / f"{fit}.png"
    render_component(FileProjectRepository().open(root), "card", output)
    image = QImage(str(output))
    assert image.pixelColor(50, 5) == QColor(top)
    assert image.pixelColor(50, 50) == QColor(middle)


def test_html_table_rowspan_colspan_and_inline_images_render_at_two_dpi(tmp_path: Path) -> None:
    root = demo(tmp_path)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    snapshot = FileProjectRepository().open(root)
    render_component(snapshot, "forest-card", first, dpi=96)
    render_component(snapshot, "forest-card", second, dpi=192)
    one, two = QImage(str(first)), QImage(str(second))
    assert one.size() == QSize(238, 333)
    assert two.size() == QSize(476, 665)
    # Text/table area contains non-white pixels at both resolutions.
    assert any(one.pixelColor(x, y) != QColor("#FFFFFF") for x in range(15, 225, 10) for y in range(205, 325, 10))
    assert any(two.pixelColor(x, y) != QColor("#FFFFFF") for x in range(30, 450, 20) for y in range(410, 650, 20))


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda text: text.replace("height_mm: 25", "height_mm: 1"), "TEXT_OVERFLOW"),
        (lambda text: text.replace("assets/images/animals/enemy/leaf.png", "assets/images/missing.png"), "RESOURCE_MISSING"),
        (lambda text: text.replace("font_family: Arial", "font_family: Definitely Missing Font 123"), "FONT_MISSING"),
        (lambda text: text.replace("<table border=", "<video></video><table border="), "HTML_UNSUPPORTED"),
        (lambda text: text.replace('src="assets/images/animals/enemy/leaf.png"', 'src="https://example.invalid/a.png"'), "PATH_INVALID"),
    ],
)
def test_render_errors_are_addressed_and_do_not_publish_output(mutation: Callable[[str], str], code: str, tmp_path: Path) -> None:
    root = demo(tmp_path)
    component = root / "components/forest-card.yaml"
    component.write_text(mutation(component.read_text(encoding="utf-8")), encoding="utf-8")
    output = tmp_path / "must-not-exist.png"
    result = cli("render", str(root), "--component", "forest-card", "--output", str(output))
    assert result.returncode == 2
    assert code in result.stderr
    assert "forest-card.yaml" in result.stderr
    assert not output.exists()


def test_static_variables_are_html_escaped_and_excel_columns_wait_for_stage_six(tmp_path: Path) -> None:
    owner = tmp_path / "component.yaml"
    assert resolve_static('<p>{{ vars["name"] }}</p>', {"name": "<b>A&B</b>"}, html=True, owner=owner, field="html") == "<p>&lt;b&gt;A&amp;B&lt;/b&gt;</p>"
    with pytest.raises(ProjectError) as caught:
        resolve_static("{Название}", {}, html=True, owner=owner, field="html")
    assert caught.value.diagnostic.code == "BINDING_UNRESOLVED"
