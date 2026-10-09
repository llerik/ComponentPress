"""Desktop entry point."""

from pathlib import Path
import json
import sys
import traceback

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from componentpress.bootstrap import project_controller
from componentpress.domain.component import SizeMM
from componentpress.application.contracts import BuildRequest
from componentpress.application.build_service import BuildService
from componentpress.app_assets import load_application_icon
from componentpress.editor import MainWindow


def _run_release_smoke(window: MainWindow, root: Path) -> bool:
    """Exercise stage-six Excel preview plus the preceding editing workflow."""
    window.load_session(window.controller.open(root))
    if window.session is None:
        return False
    component_ids = tuple(window.session.documents)
    if not component_ids:
        return False
    first_id = component_ids[0]
    first_tab = window.open_component(first_id)
    if first_tab is None or not window._refresh_preview(first_tab) or not _wait_for_preview(first_tab):
        return False
    if window.session.documents[first_id].model.data is not None:
        if window.controller.preview is None:
            return False
        data = window.controller.preview.data(window.session.snapshot, first_id)
        if data is None or len(data.rows) < 2:
            return False
        template_before = window.session.documents[first_id].current_text
        # The demo's second card row is intentionally excluded from Prod, so
        # exercise the alternate validated filter to resolve both examples.
        first = window.controller.preview.select_row(window.session.snapshot, first_id, row_number=data.rows[0].row_number, mode="test")
        second = window.controller.preview.select_row(window.session.snapshot, first_id, row_number=data.rows[1].row_number, mode="test")
        if first.component == second.component or window.session.documents[first_id].current_text != template_before:
            return False
        smoke_dir = root / ".componentpress"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        first_png = smoke_dir / "release-smoke-row1.png"
        second_png = smoke_dir / "release-smoke-row2.png"
        window.controller.preview.export_png(window.session.snapshot, first_id, first_png, row_number=data.rows[0].row_number, mode="test", dpi=96)
        window.controller.preview.export_png(window.session.snapshot, first_id, second_png, row_number=data.rows[1].row_number, mode="test", dpi=96)
        if not first_png.is_file() or not second_png.is_file() or first_png.read_bytes() == second_png.read_bytes():
            return False
    smoke_id = "release-smoke"
    if smoke_id in window.session.documents or not window.add_component(smoke_id, "Проверка выпуска"):
        return False
    smoke_tab = window.open_component(smoke_id)
    if smoke_tab is None:
        return False
    if window.open_component(first_id) is not first_tab or window.tabs.count() != 2:
        return False
    window.tabs.setCurrentWidget(smoke_tab)
    window.controller.update_component(
        smoke_id,
        size_mm=SizeMM(width=70.0, height=90.0),
        background="#E8F0FF",
    )
    # The stage-five path is exercised without modal interaction: both visual
    # element types, grouping, text synchronization, undo/redo and YAML are included.
    if not window.add_html("<p><b>Проверка выпуска</b></p>", 4.0, 5.0, node_id="smoke-title"):
        return False
    if not window.add_image(
        "assets/images/animals/enemy/leaf.png", 8.0, 24.0,
        node_id="smoke-image",
    ):
        return False
    window._set_selection(("smoke-title", "smoke-image"), source="tree")
    if not window.group_selected("smoke-group", "Проверка группы"):
        return False
    window.undo_group.undo()
    if "smoke-group" in {node.id for node in window.session.documents[smoke_id].model.elements}:
        return False
    window.undo_group.redo()
    window._request_mode(smoke_tab, "text")
    yaml_text = smoke_tab.text_editor.toPlainText()
    yaml_text = yaml_text.replace("#E8F0FF", "#DDEEFF") + "# Проверка текстового режима\n"
    smoke_tab.text_editor.setPlainText(yaml_text)
    if not window._apply_draft(smoke_id):
        return False
    window._request_mode(smoke_tab, "layout")
    if window.session.documents[smoke_id].model.background != "#DDEEFF":
        return False
    if not window.save_active():
        return False
    window.load_session(window.controller.open(root))
    if window.session is None:
        return False
    saved = window.session.documents.get(smoke_id)
    if saved is None or saved.model.size_mm != SizeMM(width=70.0, height=90.0):
        return False
    if saved.model.background != "#DDEEFF" or "# Проверка текстового режима" not in saved.committed.text:
        return False
    if not any(node.id == "smoke-group" for node in saved.model.elements):
        return False
    first_tab = window.open_component(first_id)
    smoke_tab = window.open_component(smoke_id)
    if first_tab is None or smoke_tab is None or window.tabs.count() != 2:
        return False
    window.tabs.setCurrentWidget(first_tab)
    if not window._refresh_preview(first_tab) or not _wait_for_preview(first_tab):
        return False
    if window.controller.build is None:
        window.controller.build = BuildService(window.controller.service)
    build_result = window.controller.build.run(window.session, BuildRequest(max_render_workers=2))
    if (
        build_result.status != "succeeded"
        or build_result.pdf_path is None
        or not build_result.pdf_path.is_file()
        or build_result.report_path is None
        or not build_result.report_path.is_file()
    ):
        return False
    window.data_mode.setCurrentIndex(1)
    if window.data_mode.currentData() != "test":
        return False
    test_build = window.controller.build.run(window.session, BuildRequest(max_render_workers=2, mode="test"))
    if test_build.status != "succeeded" or test_build.mode != "test" or test_build.report_path is None:
        return False
    test_report = json.loads(test_build.report_path.read_text(encoding="utf-8"))
    if test_report.get("mode") != "test" or test_report.get("counts", {}).get("png", 0) < 1:
        return False
    from componentpress.application.png_archive import export_png_archive
    test_zip = root / ".componentpress" / "release-smoke-test.zip"
    zip_result = export_png_archive(window.session.snapshot, test_zip, mode="test", component_ids=(first_id,))
    expected_test_pngs = 2 if window.session.documents[first_id].model.data is not None else 1
    if zip_result.png_count != expected_test_pngs or not test_zip.is_file():
        return False
    cancelled = []
    loop = QEventLoop()
    cancel_id = window.controller.build.start(
        window.session,
        BuildRequest(component_ids=(first_id,), max_render_workers=2),
        on_finished=lambda result: (cancelled.append(result), loop.quit()),
    )
    window.controller.build.cancel(cancel_id)
    loop.exec()
    jobs = root / ".componentpress" / "jobs"
    return bool(cancelled and cancelled[0].status == "cancelled" and not any(jobs.iterdir()))


def _wait_for_preview(tab, timeout_ms: int = 20000) -> bool:
    expected = tab.preview_generation
    if tab.displayed_generation >= expected and tab.canvas._pixmap_item is not None:
        return True
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if tab.displayed_generation >= expected else None)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timer.start()
    timeout.start(timeout_ms)
    loop.exec()
    timer.stop()
    return tab.displayed_generation >= expected and tab.canvas._pixmap_item is not None


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication([sys.argv[0]])
    application.setApplicationName("ComponentPress")
    application.setOrganizationName("ComponentPress")
    application.setWindowIcon(load_application_icon())
    window = MainWindow(project_controller())
    smoke = len(arguments) == 2 and arguments[0] == "--release-smoke"
    if arguments and not smoke:
        return 2
    if smoke:
        smoke_root = Path(arguments[1])
        try:
            passed = _run_release_smoke(window, smoke_root)
        except Exception:
            diagnostics = smoke_root / ".componentpress" / "release-smoke-error.txt"
            diagnostics.parent.mkdir(parents=True, exist_ok=True)
            diagnostics.write_text(traceback.format_exc(), encoding="utf-8")
            return 4
        if not passed:
            return 3
    window.show()
    if smoke:
        QTimer.singleShot(750, application.quit)
    if not owns_application:
        return 0
    return application.exec()
