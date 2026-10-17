"""Desktop entry point."""

from pathlib import Path
import json
import inspect
import sys
import traceback
import atexit
import os
import zipfile

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtCore import QLibraryInfo, QSettings, QTranslator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QTreeView

from componentpress.bootstrap import build_service, project_controller
from componentpress.domain.component import SizeMM
from componentpress.application.contracts import BuildRequest
from componentpress.execution.qt_wait import run_build
from componentpress.app_assets import load_application_icon
from componentpress.editor import MainWindow
from componentpress.diagnostics import logging as technical_log
from componentpress.i18n import language as current_language, set_language, validate_catalogs
from componentpress.version import APPLICATION_VERSION


def _smoke_failure(root: Path, line: int) -> bool:
    diagnostics = root / ".componentpress" / "release-smoke-error.txt"
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.write_text(f"Release smoke failed at gui.py:{line}\n", encoding="utf-8")
    return False


def _run_release_smoke(window: MainWindow, root: Path) -> bool:
    """Exercise stage-six Excel preview plus the preceding editing workflow."""
    from componentpress.editor.dialogs import format_error
    from componentpress.editor.qt_models import ID_ROLE
    from componentpress.domain.tree import locations
    from componentpress.diagnostics import logging as technical_log
    from componentpress.i18n import set_language, tr, validate_catalogs

    validate_catalogs()
    set_language("en")
    english_ok = tr("menu.file") == "File" and tr("about.schema", version=6).startswith("Supported")
    set_language("ru")
    expected_log_root = os.environ.get("COMPONENTPRESS_LOG_DIR")
    log_path = technical_log.session_log()
    if not english_ok or (expected_log_root and (log_path is None or not log_path.is_relative_to(Path(expected_log_root)))):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    report_path = root / ".componentpress" / "release-smoke-support.zip"
    try:
        technical_log.create_report(report_path, status="failed", phase="rendering", mode="test")
        with zipfile.ZipFile(report_path) as report:
            support = json.loads(report.read("report.json"))
            if support["runtime"]["version"] != APPLICATION_VERSION or (expected_log_root and "session.log" not in report.namelist()):
                return _smoke_failure(root, inspect.currentframe().f_lineno)
            if "session.log" in report.namelist() and str(root).encode("utf-8") in report.read("session.log"):
                return _smoke_failure(root, inspect.currentframe().f_lineno)
    except (OSError, ValueError, zipfile.BadZipFile):
        return _smoke_failure(root, inspect.currentframe().f_lineno)

    about_checks = []

    def inspect_about() -> None:
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            about_checks.append(False)
            return
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        about_checks.append(any("GNU GPL" in label for label in labels) and any(APPLICATION_VERSION in label for label in labels))
        dialog.reject()

    QTimer.singleShot(0, inspect_about)
    window._show_about()
    if not about_checks or not about_checks[0]:
        return _smoke_failure(root, inspect.currentframe().f_lineno)

    if window.project_settings_action.isEnabled() or window.delete_component_action.isEnabled() or window.windows_menu.menuAction().isEnabled():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.show()
    QApplication.processEvents()
    window.load_session(window.controller.open(root))
    if window.session is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    expected_docks = {"Рабочие панели", "События"}
    if (
        not window.project_settings_action.isEnabled()
        or window.delete_component_action.isEnabled()
        or not window.windows_menu.menuAction().isEnabled()
        or {action.text() for action in window.windows_menu.actions()} != expected_docks
        or any(not action.isCheckable() for action in window.windows_menu.actions())
        or hasattr(window, "toolbox_dock")
        or hasattr(window, "preview_dock")
        or window.project_settings_action not in window.quick_toolbar.actions()
        or window.project_settings_action in window.icon_toolbar.actions()
        or format_error(Exception()).strip() == ""
        or format_error(Exception("   ")).strip() == ""
    ):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    events_action = next(action for action in window.windows_menu.actions() if action.text() == "События")
    events_action.trigger()
    if window.events_dock.isVisible() or events_action.isChecked():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    events_action.trigger()
    if not window.events_dock.isVisible() or window.dockWidgetArea(window.events_dock) != Qt.DockWidgetArea.BottomDockWidgetArea:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    component_tree = window.right_tabs.widget(0).findChild(QTreeView, "componentTree")
    if (
        component_tree is None
        or component_tree is not window.component_tree
        or not component_tree.isVisible()
        or component_tree.model() is None
        or component_tree.model().rowCount() != len(window.session.documents)
    ):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    component_ids = tuple(window.session.documents)
    if not component_ids:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    data_components = tuple(
        document for document in window.session.snapshot.documents.values()
        if document.model.data is not None
    )
    if data_components and (
        window.controller.preview is None
        or any(
            window.controller.preview.cached_data(window.session.snapshot, document.model.id) is None
            for document in data_components
        )
    ):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    first_id = component_ids[0]
    first_tab = window.open_component(first_id)
    if first_tab is None or not _wait_for_preview(first_tab) or not first_tab.canvas._overlays:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.session.documents[first_id].model.data is not None:
        cached = window.controller.preview.cached_data(window.session.snapshot, first_id)
        if cached is None:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        node_id = window.session.documents[first_id].model.elements[0].id
        window._move_selected((node_id,), 1.0, 0.0)
        if not _wait_for_preview(first_tab) or window.controller.preview.cached_data(window.session.snapshot, first_id) is not cached:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        first_tab.undo_stack.undo()
        if not _wait_for_preview(first_tab) or window.controller.preview.cached_data(window.session.snapshot, first_id) is not cached:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.session.snapshot.model.data_source is not None:
        source_path = root / window.session.snapshot.model.data_source.path
        source_bytes = source_path.read_bytes()
        model = window.session.snapshot.model
        try:
            window.controller.update_project_settings(
                name=model.name,
                version=model.version,
                prod=model.copies_columns.prod,
                test=model.copies_columns.test,
                data_source_file=source_path,
            )
        except Exception:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        if source_path.read_bytes() != source_bytes:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
    first_index = next(
        (component_tree.model().index(row, 0) for row in range(component_tree.model().rowCount())
         if component_tree.model().index(row, 0).data(ID_ROLE) == first_id),
        None,
    )
    if first_index is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    component_tree.setCurrentIndex(first_index)
    component_tree.setFocus()
    QTest.keyClick(component_tree, Qt.Key.Key_Down)
    if len(component_ids) > 1:
        second_id = component_tree.currentIndex().data(ID_ROLE)
        if second_id == first_id or window.active_tab is None or window.active_tab.component_id != second_id:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        first_tab = window.open_component(first_id)
        second_tab = window.open_component(second_id)
        if first_tab is None or second_tab is None:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        window.tabs.setCurrentWidget(first_tab)
        if component_tree.currentIndex().data(ID_ROLE) != first_id:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        window.tabs.setCurrentWidget(second_tab)
        if component_tree.currentIndex().data(ID_ROLE) != second_id or not window.delete_component_action.isEnabled():
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
        try:
            window.delete_component_action.trigger()
        finally:
            QMessageBox.question = original_question
        if second_id in window.session.documents:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        window._undo()
        if second_id not in window.session.documents:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        window.close_tab(window.tabs.indexOf(second_tab))
    else:
        first_tab = None
    first_tab = first_tab or window.open_component(first_id)
    if first_tab is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.session.documents[first_id].model.data is not None:
        window._validate_active()
    if not window._refresh_preview(first_tab) or not _wait_for_preview(first_tab):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.session.documents[first_id].model.data is not None:
        if window.controller.preview is None:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        data = window.controller.preview.data(window.session.snapshot, first_id)
        if data is None or len(data.rows) < 2:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        template_before = window.session.documents[first_id].current_text
        # The demo's second card row is intentionally excluded from Prod, so
        # exercise the alternate validated filter to resolve both examples.
        first = window.controller.preview.select_row(window.session.snapshot, first_id, row_number=data.rows[0].row_number, mode="test")
        second = window.controller.preview.select_row(window.session.snapshot, first_id, row_number=data.rows[1].row_number, mode="test")
        if first.component == second.component or window.session.documents[first_id].current_text != template_before:
            return _smoke_failure(root, inspect.currentframe().f_lineno)
        smoke_dir = root / ".componentpress"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        first_png = smoke_dir / "release-smoke-row1.png"
        second_png = smoke_dir / "release-smoke-row2.png"
        window.controller.preview.export_png(window.session.snapshot, first_id, first_png, row_number=data.rows[0].row_number, mode="test", dpi=96)
        window.controller.preview.export_png(window.session.snapshot, first_id, second_png, row_number=data.rows[1].row_number, mode="test", dpi=96)
        if not first_png.is_file() or not second_png.is_file() or first_png.read_bytes() == second_png.read_bytes():
            return _smoke_failure(root, inspect.currentframe().f_lineno)
    smoke_id = "release-smoke"
    if smoke_id in window.session.documents or not window.add_component(smoke_id, "Проверка выпуска"):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    smoke_tab = window.open_component(smoke_id)
    if smoke_tab is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.open_component(first_id) is not first_tab or window.tabs.count() != 2:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.tabs.setCurrentWidget(smoke_tab)
    window.controller.update_component(
        smoke_id,
        size_mm=SizeMM(width=70.0, height=90.0),
        background="#E8F0FF",
    )
    # The stage-five path is exercised without modal interaction: both visual
    # element types, grouping, text synchronization, undo/redo and YAML are included.
    if not window.add_html("<p><b>Проверка выпуска</b></p>", 4.0, 5.0, node_id="smoke-title"):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if not window.add_image(
        "assets/images/animals/enemy/leaf.png", 8.0, 24.0,
        node_id="smoke-image",
    ):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window._draw_element("rectangle", 40.0, 10.0, 58.0, 18.0)
    window._draw_element("ellipse", 40.0, 22.0, 58.0, 30.0)
    window._draw_element("line", 58.0, 35.0, 40.0, 35.0)
    drawn = window.session.documents[smoke_id].model.elements
    if not {node.type for node in drawn}.issuperset({"rectangle", "ellipse", "line"}):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window._set_selection(("smoke-title", "smoke-image"), source="tree")
    if not window.group_selected("smoke-group", "Проверка группы"):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.undo_group.undo()
    if "smoke-group" in {node.id for node in window.session.documents[smoke_id].model.elements}:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.undo_group.redo()
    window._set_selection(("smoke-group",), source="tree")
    if not window.duplicate_selected():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    copied_group_id = window._selected_ids()[0]
    copied_group = locations(window.session.documents[smoke_id].model)[copied_group_id].node
    copied_ids = (copied_group_id, *(child.id for child in copied_group.children))
    if copied_group.name != "Проверка группы" or [child.name for child in copied_group.children] != ["HTML-текст 2", "Изображение 2"]:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.undo_group.undo()
    window.undo_group.redo()
    if tuple(node_id for node_id in locations(window.session.documents[smoke_id].model) if node_id in copied_ids) != copied_ids:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window._request_mode(smoke_tab, "text")
    yaml_text = smoke_tab.text_editor.toPlainText()
    yaml_text = yaml_text.replace("#E8F0FF", "#DDEEFF") + "# Проверка текстового режима\n"
    smoke_tab.text_editor.setPlainText(yaml_text)
    if not window._apply_draft(smoke_id):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window._request_mode(smoke_tab, "layout")
    if window.session.documents[smoke_id].model.background != "#DDEEFF":
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if not window.save_active():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.load_session(window.controller.open(root))
    if window.session is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    saved = window.session.documents.get(smoke_id)
    if saved is None or saved.model.size_mm != SizeMM(width=70.0, height=90.0):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if saved.model.background != "#DDEEFF" or "# Проверка текстового режима" not in saved.committed.text:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if saved.model.schema_version != 6 or not {node.type for node in saved.model.elements}.issuperset({"rectangle", "ellipse", "line"}):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if not any(node.id == "smoke-group" for node in saved.model.elements):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    first_tab = window.open_component(first_id)
    smoke_tab = window.open_component(smoke_id)
    if first_tab is None or smoke_tab is None or window.tabs.count() != 2:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.tabs.setCurrentWidget(first_tab)
    if window.session.documents[first_id].model.data is not None:
        window._validate_active()
    if not window._refresh_preview(first_tab) or not _wait_for_preview(first_tab):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    if window.controller.build is None:
        window.controller.build = build_service(window.controller.service)
    build_result = run_build(window.controller.build, window.session, BuildRequest(max_render_workers=2))
    if (
        build_result.status != "succeeded"
        or build_result.pdf_path is None
        or not build_result.pdf_path.is_file()
        or build_result.report_path is None
        or not build_result.report_path.is_file()
    ):
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.data_mode.setChecked(True)
    if window.data_mode.mode != "test":
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    test_build = run_build(window.controller.build, window.session, BuildRequest(max_render_workers=2, mode="test"))
    if test_build.status != "succeeded" or test_build.mode != "test" or test_build.report_path is None:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    test_report = json.loads(test_build.report_path.read_text(encoding="utf-8"))
    if test_report.get("mode") != "test" or test_report.get("counts", {}).get("png", 0) < 1:
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    test_zip = root / ".componentpress" / "release-smoke-test.zip"
    zip_result = window.controller.png_archive_exporter(window.session.snapshot, test_zip, mode="test", component_ids=(first_id,))
    expected_test_pngs = 2 if window.session.documents[first_id].model.data is not None else 1
    if zip_result.png_count != expected_test_pngs or not test_zip.is_file():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    from componentpress.application.version_service import import_project_archive
    from componentpress.domain.diagnostics import ProjectError
    from componentpress.execution.cancellation import CancellationToken

    archive_directory = root / "archive"
    archive_directory.mkdir(exist_ok=True)
    version_zip = archive_directory / "release-smoke-0.1.0.zip"
    original_snapshot = window.session.snapshot
    version_result = window.controller.service.versions.export(original_snapshot, version_zip)
    if version_result[1:] != ("0.1.1", True, None) or not version_zip.is_file():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    promoted_snapshot = window.controller.service.projects.open(root)
    if promoted_snapshot.model.version != "0.1.1":
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    window.session.replace_snapshot(promoted_snapshot, saved=set(promoted_snapshot.documents))
    try:
        window.controller.service.versions.export(promoted_snapshot, version_zip)
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    except ProjectError as exc:
        if exc.diagnostic.code != "ARCHIVE_EXISTS":
            return _smoke_failure(root, inspect.currentframe().f_lineno)
    cancellation = CancellationToken()
    cancellation.cancel()
    try:
        window.controller.service.versions.export(
            promoted_snapshot,
            archive_directory / "cancelled.zip",
            cancellation=cancellation,
        )
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    except ProjectError as exc:
        if exc.diagnostic.code != "BUILD_CANCELLED":
            return _smoke_failure(root, inspect.currentframe().f_lineno)
    if (archive_directory / "cancelled.zip").exists():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    restored_root = root.parent / "release-smoke-restored"
    if restored_root.exists():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    import_project_archive(version_zip, restored_root)
    restored_snapshot = window.controller.service.projects.open(restored_root)
    if restored_snapshot.model.version != "0.1.0":
        return _smoke_failure(root, inspect.currentframe().f_lineno)
    from componentpress.application.sessions import ProjectSession

    restored_result = run_build(build_service(window.controller.service), ProjectSession.from_snapshot(restored_snapshot), BuildRequest(max_render_workers=2))
    if restored_result.status != "succeeded" or restored_result.pdf_path is None or not restored_result.pdf_path.is_file():
        return _smoke_failure(root, inspect.currentframe().f_lineno)
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
    smoke = len(arguments) == 2 and arguments[0] == "--release-smoke"
    if smoke:
        settings_root = Path(arguments[1]) / ".componentpress" / "release-smoke-settings"
        settings_root.mkdir(parents=True, exist_ok=True)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings_root))
        os.environ["COMPONENTPRESS_LOG_DIR"] = str(Path(arguments[1]) / ".componentpress" / "release-smoke-logs")
    technical_log.initialize()
    technical_log.install_exception_hooks()
    application.aboutToQuit.connect(technical_log.shutdown)
    atexit.register(technical_log.shutdown)
    validate_catalogs()
    settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ComponentPress", "ComponentPress")
    language = settings.value("language", "ru", type=str)
    set_language(language)
    language = current_language()
    if language != "ru":
        import PySide6

        translator = QTranslator(application)
        translations = str(Path(PySide6.__file__).parent / "translations")
        if not Path(translations).is_dir():
            translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load(f"qtbase_{language}", translations):
            application.installTranslator(translator)
            application._componentpress_translator = translator
    technical_log.log_event("application.language", language=language)
    window = MainWindow(project_controller())
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
