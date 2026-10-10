"""Behavioural checks for high-detail preview and image print quality."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading
import time

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtPdf import QPdfDocument

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest
from componentpress.bootstrap import project_controller, project_service
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.nodes import HtmlNode, ImageNode
from componentpress.domain.project import ProjectDefinition
from componentpress.editor.main_window import MainWindow
from componentpress.project_io.repository import FileProjectRepository
from componentpress.rendering.exporter import ensure_gui_application, render_component
from componentpress.rendering.geometry import effective_image_dpi
from componentpress.rendering.image_quality import measure_component_images
from componentpress.rendering.resources import ProjectResourceLoader


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-game"


@pytest.mark.parametrize(
    ("fit", "expected"),
    [
        ("contain", (300.0, 300.0)),
        ("cover", (150.0, 150.0)),
        ("stretch", (300.0, 150.0)),
    ],
)
def test_effective_dpi_matches_crop_and_print_size(fit: str, expected: tuple[float, float]) -> None:
    assert effective_image_dpi(600, 300, 50.8, 50.8, fit) == pytest.approx(expected)


def test_quality_inspection_lists_each_inline_image_at_its_effective_size(tmp_path: Path) -> None:
    image = QImage(300, 150, QImage.Format.Format_RGB32)
    image.fill(QColor("#2266AA"))
    folder = tmp_path / "assets" / "images"
    folder.mkdir(parents=True)
    assert image.save(str(folder / "detail.png"), "PNG")
    project = ProjectDefinition.model_validate({
        "schema_version": 5, "id": "test", "name": "Test", "version": "1.0.0",
    })
    component = ComponentDefinition.model_validate({
        "schema_version": 5, "id": "card", "name": "Card",
        "size_mm": {"width": 50.8, "height": 50.8},
        "elements": [{
            "id": "text", "name": "Text", "type": "html", "x_mm": 0, "y_mm": 0,
            "width_mm": 50.8, "height_mm": 50.8, "font_family": "Arial", "font_size_pt": 10,
            "html": '<p><img src="assets/images/detail.png" width="96" height="48" />'
                    '<img src="assets/images/detail.png" /></p>',
        }],
    })
    items = measure_component_images(component, project, ProjectResourceLoader(tmp_path), tmp_path / "card.yaml")
    assert len(items) == 2
    assert items[0].pixel_width == 300 and items[0].width_mm == pytest.approx(25.4)
    assert items[0].dpi_x == pytest.approx(300)
    assert items[1].dpi_x == pytest.approx(96)
    assert not items[0].warning and items[1].warning
    warning = items[1].diagnostic(tmp_path / "card.yaml")
    assert warning is not None and warning.code == "IMAGE_DPI_LOW" and warning.severity == "warning"


def test_pdf_images_use_lossless_encoding_and_keep_page_geometry(tmp_path: Path) -> None:
    ensure_gui_application()
    root = tmp_path / "jpeg-project"
    (root / "components").mkdir(parents=True)
    (root / "assets" / "images").mkdir(parents=True)
    source = QImage(20, 10, QImage.Format.Format_RGB32)
    for y in range(10):
        for x in range(20):
            source.setPixelColor(x, y, QColor(255 if x < 10 else 0, 0, 255 if x >= 10 else 0))
    jpeg_path = root / "assets" / "images" / "tile.jpg"
    assert source.save(str(jpeg_path), "JPEG", 85)
    image_node = ImageNode.model_validate({
        "id": "tile", "name": "Tile", "type": "image", "x_mm": 0, "y_mm": 0,
        "width_mm": 50.8, "height_mm": 25.4, "source": "assets/images/tile.jpg", "fit": "stretch",
    })
    component = ComponentDefinition(
        schema_version=5, id="card", name="Card",
        size_mm=SizeMM(width=50.8, height=25.4), elements=(image_node,),
    )
    project = ProjectDefinition.model_validate({
        "schema_version": 5, "id": "jpeg", "name": "JPEG", "version": "1.0.0",
    })
    document = DocumentSnapshot(root / "components" / "card.yaml", "", "", component)
    snapshot = ProjectSnapshot(root, "", "", project, {"card": document})
    png, pdf = tmp_path / "card.png", tmp_path / "card.pdf"
    render_component(snapshot, "card", png, dpi=300, pdf=pdf)
    assert QImage(str(png)).size() == QSize(600, 300)
    content = pdf.read_bytes()
    assert b"/DCTDecode" not in content
    reader = QPdfDocument()
    assert reader.load(str(pdf)) == QPdfDocument.Error.None_
    assert reader.pageCount() == 1
    assert reader.pagePointSize(0).width() == pytest.approx(50.8 / 25.4 * 72, abs=0.51)


def test_low_resolution_image_is_reported_without_blocking_build(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    shutil.copytree(DEMO, root)
    service = project_service()
    result = BuildService(service).run(
        service.open_session(root),
        BuildRequest(component_ids=("forest-card",), max_render_workers=1),
    )
    assert result.status == "succeeded", result.diagnostics
    assert result.report_path is not None
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["application_version"] == "0.14"
    warnings = [item for item in report["warnings"] if item["code"] == "IMAGE_DPI_LOW"]
    assert warnings and all(item["severity"] == "warning" for item in warnings)
    assert all("эффективное разрешение" in item["message"] for item in warnings)


def test_preview_runs_off_ui_thread_and_keeps_scene_coordinates(tmp_path: Path, qtbot, monkeypatch) -> None:
    root = tmp_path / "demo"
    import shutil

    shutil.copytree(DEMO, root)
    window = MainWindow(project_controller())
    qtbot.addWidget(window)
    window.load_session(project_service().open_session(root))
    window.show()
    from componentpress.editor import preview_worker

    original = preview_worker.render_component_image
    worker_threads: list[int] = []

    def slow_render(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        time.sleep(0.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(preview_worker, "render_component_image", slow_render)
    tab = window.open_component("forest-card")
    assert tab is not None
    heartbeat: list[bool] = []
    from PySide6.QtCore import QTimer

    QTimer.singleShot(20, lambda: heartbeat.append(True))
    qtbot.waitUntil(lambda: tab.displayed_generation == tab.preview_generation, timeout=15000)
    assert heartbeat
    assert worker_threads and worker_threads[0] != threading.get_ident()
    assert tab.canvas._pixmap_item is not None
    logical = tab.canvas._pixmap_item.boundingRect().size()
    assert logical.width() == pytest.approx(63 * 96 / 25.4, abs=0.2)
    assert logical.height() == pytest.approx(88 * 96 / 25.4, abs=0.5)
    from componentpress.domain.tree import node_bounds

    component = window.session.documents["forest-card"].model
    heading_width_mm = node_bounds(component, "heading")[2]
    assert tab.canvas._overlays["heading"].rect().width() == pytest.approx(
        heading_width_mm * logical.width() / component.size_mm.width,
    )


def test_preview_resolution_is_bounded_for_zoom_and_high_dpi(qtbot) -> None:
    snapshot = FileProjectRepository().open(DEMO)
    component = snapshot.documents["forest-card"].model
    from componentpress.editor.document_tab import DocumentTab

    assert DocumentTab._preview_dpi(component, 4, 1.5) == 576
    assert DocumentTab._preview_dpi(component.model_copy(update={"size_mm": SizeMM(width=500, height=700)}), 8, 2) < 1536
