"""Stage-five main window with synchronized visual and YAML editing."""

from __future__ import annotations

from pathlib import Path
import re
from dataclasses import dataclass
from uuid import uuid4
from types import MappingProxyType

from pydantic import ValidationError
from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QSignalBlocker, QSize, QThreadPool, QTimer, Qt, QPoint
from PySide6.QtGui import QAction, QColor, QCloseEvent, QImage, QKeySequence, QPalette, QUndoGroup
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QSplitter,
    QTabWidget,
    QDockWidget,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QToolBar,
    QToolButton,
    QMenu,
    QScrollArea,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QToolTip,
)
from PySide6.QtGui import QHelpEvent

from componentpress.i18n import catalogs, tr
from componentpress.application.contracts import ComponentHistoryEntry, ComponentOperation
from componentpress.i18n.diagnostics import format_diagnostic, format_diagnostic_event
from componentpress.application.sessions import ProjectSession
from componentpress.application.contracts import ArchiveExportResult, BuildRequest, BuildResult, BuildProgress
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.component import DataBinding
from componentpress.application.preview_service import ValidationIssues
from componentpress.execution.cancellation import CancellationToken
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.paths import validate_relative_path
from componentpress.domain.schema import PROJECT_MANIFEST
from componentpress.domain.copy_mode import copies_for_mode
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, ShapeNode
from componentpress.rendering.fonts import ProjectFonts
from componentpress.rendering.fonts import available_font_families
from componentpress.platforms.services import PlatformServices
from componentpress.app_assets import set_themed_icon
from componentpress.domain.tree import (
    TreeOperationError,
    align_nodes,
    effectively_locked_ids,
    contains_locked_node,
    add_node,
    drop_nodes,
    duplicate_nodes,
    group_nodes,
    locations,
    move_nodes,
    remove_nodes,
    reorder_nodes,
    reparent_nodes,
    resize_node,
    rotate_node,
    resize_line_endpoint,
    ungroup_node,
    update_node,
)

from .controllers import ProjectController
from .action_policy import BuildContext, EditingContext, HistoryContext, HistoryTarget, OperationContext, build_actions, editing_actions, history_target, operation_allowed
from .commands import ReplaceComponentCommand
from .dialogs import HtmlAddDialog, ImageAddDialog, ImageManagerDialog, ResourcePickerDialog, format_error
from .document_tab import DocumentTab
from .properties import ComponentProperties, ElementProperties
from componentpress.preview_coordinator import PreviewCoordinator, PreviewJob, PreviewPhase, PreviewRequest
from .preview_worker import (
    FontReadFailed,
    FontReadSucceeded,
    PreviewFontReadTask,
    PreviewRenderFailed,
    PreviewRenderSucceeded,
    PreviewSignals,
    PreviewTask,
    PreviewTaskResult,
)
from .project_history import ComponentHistory
from .version_worker import (
    ExportArchiveRequest,
    ImportImageFolderRequest,
    RestoreArchiveRequest,
    VersionTask,
    VersionTaskCompletion,
    VersionTaskRequest,
)
from .qt_models import ID_ROLE, LayerTreeView, component_model, element_model
from .component_dialog import ComponentDialog
from .ui_state import UiState
from .project_settings_dialog import ProjectSettingsDialog
from .copy_mode_switch import CopyModeSwitch
from .theme import apply_theme
from .main_window_shell import MainWindowShell
from .main_window_dialogs import MainWindowDialogs
from componentpress.user_preferences import load_preferences, save_preferences
from .program_settings_dialog import ProgramSettingsDialog
from .build_dialog import BuildDialog
from .help_dialog import HelpDialog
from componentpress.i18n import language, set_language, tr
from componentpress.diagnostics import logging as technical_log
import zipfile


def _png_archive_status(result) -> str:
    message = tr("ui.png_created", path=result.path, png_count=result.png_count, copies=result.copies, mode=result.mode)
    warning = " " + tr("ui.png_cleanup_warning") if result.diagnostics else ""
    return message + warning


@dataclass
class PngArchiveOperation:
    operation_id: str
    session: ProjectSession
    session_identity: str
    active_tab: DocumentTab | None
    mode: str
    phase: str = "saving"
    snapshot: object | None = None
    component_ids: tuple[str, ...] | None = None
    target: Path | None = None
    cancellation: CancellationToken | None = None
    progress: QProgressDialog | None = None
    close_after: bool = False
    close_confirmed: bool = False


class MainWindow(QMainWindow, MainWindowShell, MainWindowDialogs):
    def __init__(self, controller: ProjectController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._initialize_document_state()
        self._initialize_build_state()
        self._initialize_preview_state()
        self._initialize_version_state()
        self._initialize_history_state()
        self._initialize_program_state()
        self.setObjectName("mainWindow")
        self._disable_menu_effects()
        self.setWindowTitle(tr("app.name"))
        self.resize(1280, 800)
        self._build_actions()
        self._build_ui()
        self.ui_state.restore(self)
        self._suppress_docks()
        self._connect_signals()
        self._update_actions()

    def _initialize_document_state(self) -> None:
        self._tabs: dict[str, DocumentTab] = {}
        self._selection_sync = False

    def _initialize_build_state(self) -> None:
        self._active_build_job: str | None = None
        self._build_pending_active_only: bool | None = None
        self._build_operation_id: str | None = None
        self._build_session: ProjectSession | None = None
        self._build_tab: DocumentTab | None = None
        self._build_mode: str | None = None
        self._last_build_result: BuildResult | None = None
        self._build_progress: BuildDialog | None = None
        self._last_build_progress: BuildProgress | None = None
        self._build_starting = False
        self._build_progress_revision = -1
        self._build_cancel_requested = False
        self._build_after_action = "none"
        self._close_after_build = False
        self._reserved_build_request: BuildRequest | None = None

    def _initialize_preview_state(self) -> None:
        self._preview_session_identity = uuid4().hex
        self._preview_coordinator = PreviewCoordinator()
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(1)
        self._preview_signals = PreviewSignals(self)
        self._preview_signals.finished.connect(self._preview_task_finished, Qt.ConnectionType.QueuedConnection)
        self._preview_close_pending = False
        self._preview_close_confirmed = False

    def _initialize_version_state(self) -> None:
        self._version_pool = QThreadPool(self)
        self._version_pool.setMaxThreadCount(1)
        self._version_task = None
        self._version_busy_id: str | None = None
        self._version_task_id: str | None = None
        self._pending_restore: tuple[Path, str] | None = None
        self._png_operation: PngArchiveOperation | None = None
        self._save_reservation_id: str | None = None
        self._version_request: VersionTaskRequest | None = None
        self._version_cancellation: CancellationToken | None = None
        self._version_progress: QProgressDialog | None = None

    def _initialize_history_state(self) -> None:
        self.undo_group = QUndoGroup(self)
        self._component_history = ComponentHistory()
        self._history_tabs: dict[str, DocumentTab | None] = {}
        self._history_documents = {}
        self._project_history_focus = False

    def _initialize_program_state(self) -> None:
        self._program_preferences = load_preferences()
        self._theme = self._program_preferences.theme
        apply_theme(self._theme)
        self.ui_state = UiState()

    @staticmethod
    def _disable_menu_effects() -> None:
        application = QApplication.instance()
        if application is not None:
            application.setEffectEnabled(Qt.UIEffect.UI_AnimateMenu, False)
            application.setEffectEnabled(Qt.UIEffect.UI_FadeMenu, False)

    @property
    def session(self) -> ProjectSession | None:
        return self.controller.session

    @property
    def active_tab(self) -> DocumentTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, DocumentTab) else None






    def _show_copy_mode_tooltip(self) -> None:
        project = self.session.snapshot.model if self.session is not None else None
        prod_column = project.copies_columns.prod if project is not None else "Prod"
        test_column = project.copies_columns.test if project is not None else "Test"
        message = tr("ui.copy_mode_info", prod_column=prod_column, test_column=test_column)
        point = self.mode_info_button.mapToGlobal(self.mode_info_button.rect().bottomLeft())
        QToolTip.showText(point, message, self.mode_info_button, self.mode_info_button.rect(), 1500)

    def _mode_info_tooltip_event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            button = self.mode_info_button
            QToolTip.showText(
                event.globalPos(), tr("ui.copy_mode_info_tip"), button, button.rect(), 1500
            )
            return True
        return False

    def _register_file_type(self, parent=None) -> None:
        operation_id = uuid4().hex
        technical_log.log_event("application.file_association", operation="file_association",
                                operation_id=operation_id, phase="preparation", status="started")
        from componentpress.platforms.windows_association import register_current_executable

        succeeded, code = register_current_executable()
        technical_log.log_event("application.file_association", operation="file_association",
                                operation_id=operation_id, phase="finished",
                                status="succeeded" if succeeded else "failed", code=code)
        message = tr(f"error.{code.lower()}")
        if succeeded:
            QMessageBox.information(parent or self, tr("ui.program_settings"), message)
        else:
            QMessageBox.warning(parent or self, tr("ui.program_settings"), message)


    def eventFilter(self, watched, event) -> bool:
        if self._route_text_history_shortcut(watched, event):
            return True
        if watched is getattr(self, "mode_info_button", None) and self._mode_info_tooltip_event(event):
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            tab = getattr(self, "active_tab", None)
            if tab is not None and tab.canvas.active_tool is not None:
                tab.canvas.cancel_tool()
                return True
        return super().eventFilter(watched, event)

    def _route_text_history_shortcut(self, watched, event) -> bool:
        if event.type() != QEvent.Type.KeyPress or not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        tab = self.active_tab
        if tab is None or watched is not tab.text_editor:
            return False
        key, modifiers = event.key(), event.modifiers()
        if key == Qt.Key.Key_Z:
            self._redo() if modifiers & Qt.KeyboardModifier.ShiftModifier else self._undo()
            return True
        if key == Qt.Key.Key_Y:
            self._redo()
            return True
        return False



    def _build_icon_toolbar(self) -> None:
        self.icon_toolbar = QToolBar(tr("panel.edit_tools"), self)
        self.icon_toolbar.setObjectName("editTools")
        self.icon_toolbar.setMovable(False)
        self.icon_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self.icon_toolbar)
        for button in self.drawing_tools.values():
            self.icon_toolbar.addWidget(button)
        self.icon_toolbar.addSeparator()
        for button in (self.layer_up, self.layer_down, self.group_button, self.ungroup_button):
            self.icon_toolbar.addWidget(button)
        self.icon_toolbar.addSeparator()
        for action, icon in ((self.delete_action, "remove.png"),):
            set_themed_icon(action, icon)
            action.setToolTip(action.text())
            action.setProperty("accessibleName", action.text())
        self.delete_button = self._toolbar_button(self.delete_action, "remove.png")
        self.delete_widget_action = self.icon_toolbar.addWidget(self.delete_button)
        self.delete_widget_action.setVisible(False)

    def _update_build_toolbar(self) -> None:
        running = self._active_build_job is not None
        self.build_active_widget_action.setVisible(not running)
        self.build_all_widget_action.setVisible(not running)
        self.cancel_build_widget_action.setVisible(running)




    def _suppress_docks(self) -> None:
        for dock in self.docks:
            dock.hide()
        self.quick_toolbar.hide()
        self.icon_toolbar.hide()

    def _dock_visibility_requested(self, dock: QDockWidget, area: Qt.DockWidgetArea, visible: bool) -> None:
        if not visible:
            return
        dock.setFloating(False)
        self.addDockWidget(area, dock)

    def _component_context_menu(self, point) -> None:
        index = self.component_tree.indexAt(point)
        component_id = index.data(ID_ROLE) if index.isValid() else None
        if not component_id or self.session is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction(tr("ui.open"))
        properties_action = menu.addAction(tr("ui.properties"))
        menu.addSeparator()
        delete_action = menu.addAction(tr("ui.delete"))
        chosen = menu.exec(self.component_tree.viewport().mapToGlobal(point))
        if chosen is open_action:
            self.open_component(component_id)
        elif chosen is properties_action:
            self.edit_component_dialog(component_id)
        elif chosen is delete_action:
            self.delete_component(component_id)

    def _tab_context_menu(self, point) -> None:
        index = self.tabs.tabBar().tabAt(point)
        tab = self.tabs.widget(index) if index >= 0 else None
        if not isinstance(tab, DocumentTab):
            return
        menu = QMenu(self)
        properties_action = menu.addAction(tr("ui.properties"))
        delete_action = menu.addAction(tr("ui.delete_component"))
        chosen = menu.exec(self.tabs.tabBar().mapToGlobal(point))
        if chosen is properties_action:
            self.edit_component_dialog(tab.component_id)
        elif chosen is delete_action:
            self.delete_component(tab.component_id)

    def _select_tool(self) -> None:
        if self.active_tab is not None:
            self.active_tab.canvas.cancel_tool()
            self.active_tab.canvas.set_tool(None)
            self._sync_tool_buttons()


    def _show_help(self, section_key: str | None = None) -> None:
        dialog = getattr(self, "_help_dialog", None)
        if dialog is None:
            dialog = self._help_dialog = HelpDialog(self)
        if section_key:
            title = tr(section_key)
            matches = dialog.sections.findItems(title, Qt.MatchFlag.MatchExactly)
            if matches:
                dialog.sections.setCurrentItem(matches[0])
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def _connect_signals(self) -> None:
        self._connect_project_actions()
        self._connect_build_actions()
        self._connect_edit_actions()
        self._connect_main_tab_signals()
        self._connect_property_signals()
        self._connect_tool_signals()
        self._connect_data_signals()
        self.event_search.textChanged.connect(self._filter_events)

    def _connect_project_actions(self) -> None:
        for action, handler in ((self.new_action, self.new_project_dialog), (self.open_action, self.open_project_dialog),
                                (self.save_action, self.save_active), (self.save_all_action, self.save_all)):
            action.triggered.connect(handler)
        self.close_tab_action.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        self.exit_action.triggered.connect(self.close)
        for action, handler in ((self.import_project_archive_action, self._import_project_archive),
                                (self.export_project_archive_action, self._export_project_archive),
                                (self.export_png_zip_action, self._export_png_archive),
                                (self.add_component_action, self.add_component_dialog),
                                (self.project_settings_action, self.project_settings_dialog),
                                (self.program_settings_action, self._show_program_settings),
                                (self.image_manager_action, self.open_image_manager),
                                (self.about_action, self._show_about),
                                (self.error_report_action, self._create_error_report)):
            action.triggered.connect(handler)
        self.help_action.triggered.connect(lambda: self._show_help())
        self.hotkeys_action.triggered.connect(lambda: self._show_help("help.hotkeys.title"))

    def _connect_build_actions(self) -> None:
        self.validate_active_action.triggered.connect(self._validate_active)
        self.validate_all_action.triggered.connect(lambda: self._validate_all(force_read=True))
        self.build_all_action.triggered.connect(lambda: self._start_build(False))
        self.build_active_action.triggered.connect(lambda: self._start_build(True))
        self.cancel_build_action.triggered.connect(self._cancel_build)
        self.open_build_action.triggered.connect(lambda: self._open_build_result(False))
        self.open_pdf_action.triggered.connect(lambda: self._open_build_result(True))

    def _connect_edit_actions(self) -> None:
        for action, handler in ((self.undo_action, self._undo), (self.redo_action, self._redo),
                                (self.delete_action, self.delete_selected)):
            action.triggered.connect(handler)
        self.delete_component_action.triggered.connect(
            lambda: self.active_tab and self.delete_component(self.active_tab.component_id)
        )
        self.group_action.triggered.connect(lambda: self.group_selected())
        self.ungroup_action.triggered.connect(lambda: self.ungroup_selected())
        self.zoom_in_action.triggered.connect(lambda: self._zoom_active(1.2))
        self.zoom_out_action.triggered.connect(lambda: self._zoom_active(1 / 1.2))
        self.fit_action.triggered.connect(lambda: self.active_tab and self.active_tab.canvas.fit_page())
        self.actual_size_action.triggered.connect(lambda: self.active_tab and self.active_tab.canvas.actual_size())

    def _connect_main_tab_signals(self) -> None:
        self.component_tree.doubleClicked.connect(self._open_component_index)
        self.component_tree.clicked.connect(self._open_component_index)
        self.tabs.currentChanged.connect(self._active_tab_changed)
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)
        self.tabs.tabCloseRequested.connect(self.close_tab)

    def _connect_property_signals(self) -> None:
        self.properties.nameChanged.connect(lambda value: self._edit_active(name=value))
        self.properties.sizeChanged.connect(self._edit_active_size)
        self.properties.backgroundChanged.connect(lambda value: self._edit_active(background=value))
        self.properties.backgroundColorRequested.connect(self._pick_background_color)
        self.element_properties.nodeChanged.connect(self._edit_selected_node)
        self.element_properties.selectImageRequested.connect(self.select_element_image)
        self.element_properties.rotationRequested.connect(self._rotate_selected)
        self.element_properties.insertImageRequested.connect(self.insert_html_image)
        self.element_properties.colorRequested.connect(self._pick_node_color)

    def _connect_tool_signals(self) -> None:
        self.select_tool.clicked.connect(self._select_tool)
        tools = ((self.image_tool, "image"), (self.html_tool, "html"),
                 (self.conditional_group_tool, "conditional_group"), (self.rectangle_tool, "rectangle"),
                 (self.ellipse_tool, "ellipse"), (self.line_tool, "line"))
        for button, tool in tools:
            button.clicked.connect(lambda _checked=False, tool=tool: self._begin_tool(tool))
        self._connect_layer_signals()

    def _connect_layer_signals(self) -> None:
        self.layer_up.clicked.connect(lambda: self.reorder_selected(1))
        self.layer_down.clicked.connect(lambda: self.reorder_selected(-1))
        self.group_button.clicked.connect(lambda: self.group_selected())
        self.ungroup_button.clicked.connect(lambda: self.ungroup_selected())
        self.element_tree.layerDropRequested.connect(self._layer_drop)
        self.element_tree.lockToggleRequested.connect(self._toggle_element_lock)

    def _connect_data_signals(self) -> None:
        self.data_import.clicked.connect(self._import_xlsx)
        self.data_clear.clicked.connect(self._clear_xlsx)
        self.data_refresh.clicked.connect(lambda: self._refresh_data(force=True))
        self.data_validate.clicked.connect(self._validate_active)
        self.data_validate_all.clicked.connect(self._validate_all)
        self.data_export.clicked.connect(self._export_instance)
        self.data_export_zip.clicked.connect(self._export_png_archive)
        self.data_insert.clicked.connect(self._insert_column_binding)
        self.data_row.currentIndexChanged.connect(self._row_changed)
        self.data_sheet.currentIndexChanged.connect(self._data_binding_changed)
        self.data_column.currentIndexChanged.connect(self._update_data_chain)
        self.data_mode.toggled.connect(self._data_mode_changed)


    def load_session(self, session: ProjectSession, *, reservation: str | None = None) -> None:
        """Install an already opened session; useful to bootstrap and to test the UI."""
        if not self._operation_allowed("project_switch") and reservation != self._version_busy_id:
            return
        self._prepare_session_switch(session, reservation)
        self._show_loaded_session(session)

    def _prepare_session_switch(self, session, reservation):
        self._clear_tabs(discard=True, reservation=reservation)
        self._last_build_result = None
        self._preview_session_identity = uuid4().hex
        self.controller.set_session(session)
        self._remember_project(session.snapshot.root)
        self._clear_component_history()
        self._project_history_focus = False
        self._refresh_component_tree()

    def _show_loaded_session(self, session):
        self.setWindowTitle(f"{session.snapshot.model.name} — ComponentPress")
        self.status_project.setText(str(session.snapshot.root))
        self._update_project_visibility()
        if self.controller.recovery_diagnostics:
            details = "; ".join(format_diagnostic(item) for item in self.controller.recovery_diagnostics)
            self.statusBar().showMessage(f"Восстановление операций: {details}", 15000)
        component_diagnostics = tuple(
            diagnostic
            for document in session.documents.values()
            for diagnostic in document.committed.diagnostics
        )
        if component_diagnostics:
            self._add_events(component_diagnostics)
        if self.controller.preview is not None:
            self.controller.preview.invalidate(session.snapshot.root)
            self._validate_all(force_read=True)
        self._update_actions()

    def _remember_project(self, root: Path) -> None:
        resolved = str(root.resolve())
        recent = [item for item in self._program_preferences.recent_projects if item.casefold() != resolved.casefold()]
        recent.insert(0, resolved)
        updated = self._program_preferences.model_copy(update={"recent_projects": recent[:10]})
        self._program_preferences = updated
        save_preferences(updated)
        self._refresh_recent_projects_menu()

    def _refresh_recent_projects_menu(self) -> None:
        if not hasattr(self, "recent_projects_menu"):
            return
        self.recent_projects_menu.clear()
        for stored_path in self._program_preferences.recent_projects:
            path = Path(stored_path)
            action = self.recent_projects_menu.addAction(path.name or stored_path)
            action.setToolTip(stored_path)
            action.setData(stored_path)
            action.triggered.connect(lambda _checked=False, value=stored_path: self._open_recent_project(value))
        self.recent_projects_menu.setEnabled(bool(self._program_preferences.recent_projects))

    def _open_recent_project(self, stored_path: str) -> None:
        path = Path(stored_path)
        if not path.exists():
            updated = self._program_preferences.model_copy(update={
                "recent_projects": [item for item in self._program_preferences.recent_projects if item != stored_path]
            })
            self._program_preferences = updated
            save_preferences(updated)
            self._refresh_recent_projects_menu()
            QMessageBox.warning(self, tr("action.open"), tr("ui.recent_missing", path=path.name))
            return
        self.open_path(path)

    def open_path(self, root: Path) -> bool:
        if not self._operation_allowed("project_switch"):
            return False
        if not self._confirm_all_dirty() or not self._operation_allowed("project_switch"):
            return False
        try:
            session = self.controller.open(root)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        self.load_session(session)
        return True

    def create_path(self, root: Path, name: str) -> bool:
        if not self._operation_allowed("project_switch"):
            return False
        if not self._confirm_all_dirty() or not self._operation_allowed("project_switch"):
            return False
        try:
            session = self.controller.create(root, name)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        self.load_session(session)
        return True

    def new_project_dialog(self) -> None:
        if not self._operation_allowed("project_switch"):
            return
        folder = QFileDialog.getExistingDirectory(self, "Выберите пустую папку проекта")
        if not folder:
            return
        name, accepted = QInputDialog.getText(self, "Новый проект", "Название проекта")
        if accepted:
            self.create_path(Path(folder), name)

    def open_project_dialog(self) -> None:
        if not self._operation_allowed("project_switch"):
            return
        folder = QFileDialog.getExistingDirectory(self, "Открыть проект")
        if folder:
            self.open_path(Path(folder))

    def _import_project_archive(self) -> None:
        request = self._archive_restore_request()
        if request is None or not self.save_all():
            return
        self._start_version_task(
            request,
            lambda cancellation, progress: self.controller.service.versions.restore_path(
                request.archive_path, request.destination, cancellation=cancellation, on_progress=progress),
        )

    def _archive_restore_request(self) -> RestoreArchiveRequest | None:
        if not self._operation_allowed("project_switch"):
            return None
        filename, _ = QFileDialog.getOpenFileName(self, tr("archive.import_title"), "", tr("archive.filter"))
        if not filename:
            return None
        parent = QFileDialog.getExistingDirectory(self, tr("archive.restore_directory"))
        if not parent:
            return None
        folder_name, accepted = QInputDialog.getText(self, tr("archive.restored_name"), tr("archive.restored_folder_name"))
        if not accepted or not folder_name.strip() or not self._valid_archive_folder_name(folder_name):
            return None
        destination = Path(parent) / folder_name.strip()
        if destination.exists():
            QMessageBox.warning(self, tr("archive.import_title"), tr("archive.destination_exists", path=destination))
            return None
        return RestoreArchiveRequest(Path(filename), destination)

    def _valid_archive_folder_name(self, folder_name: str) -> bool:
        try:
            validate_relative_path(folder_name)
        except (ProjectError, ValueError) as exc:
            self._show_error(exc)
            return False
        if "/" in folder_name:
            QMessageBox.warning(self, tr("archive.import_title"), tr("archive.single_folder"))
            return False
        return True

    def _export_project_archive(self) -> None:
        if self.session is None or self._active_build_job is not None or self._version_task is not None:
            return
        if not self.save_all():
            return
        version = self.session.snapshot.model.version
        default_name = f"{self.session.snapshot.model.id}-{version}.zip"
        filename, _ = QFileDialog.getSaveFileName(
            self, tr("archive.export_title"),
            str(self.session.snapshot.root / "archive" / default_name), tr("archive.filter"),
        )
        if not filename:
            return
        request = ExportArchiveRequest(self.session.snapshot, Path(filename))
        self._start_version_task(
            request,
            lambda cancellation, progress: self.controller.service.versions.export(
                request.snapshot, request.target, cancellation=cancellation, on_progress=progress
            ),
        )

    def _start_version_task(self, request: VersionTaskRequest, operation, *, reservation: str | None = None) -> None:
        if self._version_task is not None or not self._operation_allowed("version") and reservation != self._version_busy_id:
            return
        reservation = reservation or uuid4().hex
        self._version_busy_id = reservation
        task_id = uuid4().hex
        cancellation = CancellationToken()
        task = VersionTask(request, operation, cancellation, locale=language())
        self._show_version_task(task, task_id, request, cancellation)

    def _show_version_task(self, task, task_id, request, cancellation) -> None:
        task.signals.progress.connect(self._version_task_progress, Qt.ConnectionType.QueuedConnection)
        task.signals.finished.connect(lambda completion: self._version_task_finished(completion, task_id), Qt.ConnectionType.QueuedConnection)
        self._version_task, self._version_task_id, self._version_request = task, task_id, request
        self._version_cancellation = cancellation
        self._version_progress = QProgressDialog(tr("ui.archive_prepare"), tr("action.cancel_build"), 0, 0, self)
        self._version_progress.setWindowTitle(tr("ui.archive_title"))
        self._version_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._version_progress.setAutoClose(False)
        self._version_progress.setAutoReset(False)
        self._version_progress.canceled.connect(cancellation.cancel)
        self._version_progress.show()
        self._update_actions()
        self._version_pool.start(task)

    def _version_task_progress(self, completed: int, total: int, phase: str) -> None:
        if self._version_progress is None:
            return
        label = tr("ui.import_progress", completed=completed, total=total) if isinstance(self._version_request, ImportImageFolderRequest) else phase
        self._version_progress.setLabelText(label)
        self._version_progress.setRange(0, max(1, total))
        self._version_progress.setValue(min(completed, max(1, total)))
        if isinstance(self._version_request, ImportImageFolderRequest) and getattr(self, "_image_manager_dialog", None) is not None:
            self._image_manager_dialog.set_import_progress(completed, total, label)

    def _version_task_finished(self, completion: VersionTaskCompletion, task_id: str) -> None:
        if self._version_task_id != task_id:
            return
        self._clear_version_task()
        if completion.error is not None:
            self._version_task_failed(completion, self._version_busy_id)
        else:
            self._dispatch_version_result(completion)
        self._release_version_reservation()

    def _clear_version_task(self) -> None:
        if self._version_progress is not None:
            self._version_progress.close()
        self._version_progress = self._version_task = self._version_task_id = None
        self._version_request = self._version_cancellation = None
        self._update_actions()

    def _dispatch_version_result(self, completion: VersionTaskCompletion) -> None:
        request, result, owner = completion.request, completion.result, self._version_busy_id
        if isinstance(request, ExportArchiveRequest) and isinstance(result, ArchiveExportResult):
            self._archive_export_finished(request, result, owner)
        elif isinstance(request, RestoreArchiveRequest) and isinstance(result, Path):
            self._archive_restore_finished(result, owner)
        elif isinstance(request, ImportImageFolderRequest) and isinstance(result, tuple):
            self._image_folder_import_finished(result, owner)

    def _release_version_reservation(self) -> None:
        if self._version_task is None and self._pending_restore is None:
            self._version_busy_id = None
        self._update_actions()
        if self._version_busy_id is None:
            if self.active_tab is not None:
                self._show_selection(self._selected_ids())
            self._resume_deferred_preview()

    def _version_task_failed(self, completion: VersionTaskCompletion, owner: str | None) -> None:
        error = completion.error
        assert error is not None
        request = completion.request
        if isinstance(request, ImportImageFolderRequest):
            dialog = getattr(self, "_image_manager_dialog", None)
            if dialog is not None:
                dialog.finish_import(None, error, completion.error_message)
        elif isinstance(request, ExportArchiveRequest) and error.diagnostic.code in {"ARCHIVE_EXISTS", "ARCHIVE_VERSION_EXISTS"}:
            self._confirm_archive_replacement(request, error, owner)
        elif error.diagnostic.code == "BUILD_CANCELLED":
            self.statusBar().showMessage(tr("archive.cancelled"), 5000)
        elif completion.error_message:
            self._show_prepared_version_error(completion.error_message)
        else:
            self._show_error(error)

    def _confirm_archive_replacement(self, request: ExportArchiveRequest, error: ProjectError, owner: str | None) -> None:
        target = error.diagnostic.path or request.target
        answer = QMessageBox.question(
            self, tr("archive.exists_title"), tr("archive.replace_confirm", path=target),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes or self.session is None or owner != self._version_busy_id or self.session.snapshot.root != request.snapshot.root:
            return
        replacement = ExportArchiveRequest(self.session.snapshot, target, replace=True)
        self._start_version_task(
            replacement,
            lambda cancellation, progress: self.controller.service.versions.export(
                replacement.snapshot, replacement.target, replace=True,
                cancellation=cancellation, on_progress=progress,
            ), reservation=owner,
        )

    def _show_prepared_version_error(self, message: str) -> None:
        QMessageBox.critical(self, tr("error.title"), message)
        self.statusBar().showMessage(message, 7000)

    def _archive_restore_finished(self, root: Path, owner: str | None) -> None:
        if owner is None or owner != self._version_busy_id:
            return
        if self._preview_coordinator.active is not None:
            self._pending_restore = (root, owner)
            return
        self._open_restored_project(root, owner)

    def _open_restored_project(self, root: Path, owner: str) -> None:
        if owner != self._version_busy_id or self._preview_coordinator.active is not None:
            return
        try:
            session = self.controller.open(root)
        except ProjectError as exc:
            self._show_error(exc)
            self.statusBar().showMessage(tr("archive.restored_open_failed", path=root), 12000)
        else:
            self.load_session(session, reservation=owner)
        finally:
            self._version_busy_id = None
            self._update_actions()
            self._resume_deferred_preview()

    def _continue_pending_restore(self) -> None:
        pending = self._pending_restore
        if pending is None or self._preview_coordinator.active is not None:
            return
        self._pending_restore = None
        QTimer.singleShot(0, lambda: self._open_restored_project(*pending))

    def _archive_export_finished(self, request: ExportArchiveRequest, result: ArchiveExportResult, owner: str | None) -> None:
        if owner is None or owner != self._version_busy_id or self.session is None or self.session.snapshot.root != request.snapshot.root:
            return
        try:
            self._refresh_exported_project(request)
        except ProjectError as exc:
            self._show_error(exc)
        self._report_archive_export(result)

    def _refresh_exported_project(self, request: ExportArchiveRequest) -> None:
        snapshot = self.controller.service.open(request.snapshot.root)
        if self.session is not None and self.session.snapshot.root == snapshot.root:
            self.session.replace_snapshot(snapshot, saved=set(snapshot.documents))
        self._refresh_component_tree()
        self._active_tab_changed(self.tabs.currentIndex())

    def _report_archive_export(self, result: ArchiveExportResult) -> None:
        if not result.postprocessing_complete:
            QMessageBox.warning(
                self, tr("archive.published_title"),
                tr("archive.postprocessing_pending", path=result.path, detail=result.detail or ""),
            )
            return
        assert self.session is not None
        self.setWindowTitle(f"{self.session.snapshot.model.name} — ComponentPress")
        self.statusBar().showMessage(tr("archive.export_succeeded", path=result.path, version=result.project_version), 10000)

    def _image_folder_import_finished(self, paths: tuple[str, ...], owner: str | None) -> None:
        if owner is None or owner != self._version_busy_id:
            return
        dialog = getattr(self, "_image_manager_dialog", None)
        if dialog is not None:
            dialog.finish_import(paths, None)
        self._resource_images_changed()

    def project_settings_dialog(self) -> None:
        if self.session is None or not self._user_editing_allowed():
            return
        dialog = ProjectSettingsDialog(self.session, self.controller.preview, self)
        while dialog.exec() == dialog.DialogCode.Accepted:
            values = dialog.values()
            if self._apply_project_settings(values):
                return

    def _apply_project_settings(self, values: dict) -> bool:
        try:
            self.controller.update_project_settings(
                name=values["name"], version=values["version"], prod=values["prod"], test=values["test"],
                data_source_file=values["source_file"],
                clear_data_source=not values["source"] and values["source_file"] is None,
            )
        except ProjectError as exc:
            self._show_error(exc)
            return exc.diagnostic.code not in {"PROJECT_VERSION_INVALID", "PROJECT_SETTINGS_INVALID"}
        except (ValidationError, ValueError, OSError) as exc:
            self._show_error(exc)
            return True
        self.setWindowTitle(f"{values['name']} — ComponentPress")
        self._invalidate_preview_cache()
        self._active_tab_changed(self.tabs.currentIndex())
        return True

    def open_image_manager(self) -> None:
        if self.session is None or not self._operation_allowed("resource_write"):
            return
        root = self.session.snapshot.root
        dialog = ImageManagerDialog(
            root,
            self.session.snapshot.model,
            lambda source, folder: self._import_single_image(root, source, folder),
            lambda relative, delete_file: self._unregister_project_image(root, relative, delete_file),
            lambda name, relative, width, height: self._add_project_image_key(root, name, relative, width, height),
            lambda original, name, width, height: self._edit_project_image_key(root, original, name, width, height),
            lambda name: self._remove_project_image_key(root, name),
            self,
        )
        self._image_manager_dialog = dialog
        dialog.folderImportRequested.connect(lambda source: self._start_image_folder_import(dialog, root, source, ""))
        dialog.finished.connect(lambda _result: setattr(self, "_image_manager_dialog", None))
        dialog.open()

    def _import_single_image(self, root: Path, source: Path, folder: str) -> str:
        self._require_resource_write(root)
        relative = self.controller.service.import_image(root, source, folder)
        self._resource_images_changed()
        return relative

    def _unregister_project_image(self, root: Path, relative: str, delete_file: bool) -> None:
        self._require_resource_write(root)
        assert self.session is not None
        self.controller.service.remove_image(self.session, relative, delete_file=delete_file)
        self._resource_images_changed()

    def _add_project_image_key(self, root: Path, name: str, relative: str, width: int, height: int) -> None:
        self._require_resource_write(root)
        assert self.session is not None
        self.controller.service.add_image_key(self.session, name, relative, width, height)
        self._resource_images_changed()

    def _remove_project_image_key(self, root: Path, name: str) -> None:
        self._require_resource_write(root)
        assert self.session is not None
        self.controller.service.remove_image_key(self.session, name)
        self._resource_images_changed()

    def _edit_project_image_key(self, root: Path, original: str, name: str, width: int, height: int) -> None:
        self._require_resource_write(root)
        assert self.session is not None
        self.controller.service.update_image_key(self.session, original, name, width, height)
        self._resource_images_changed()

    def _resource_images_changed(self) -> None:
        if self.session is None:
            return
        try:
            snapshot = self.controller.service.open(self.session.snapshot.root)
            self.session.replace_snapshot(snapshot)
            manager = getattr(self, "_image_manager_dialog", None)
            if manager is not None and hasattr(manager, "refresh"):
                manager.refresh(snapshot.model)
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.controller.preview.invalidate(self.session.snapshot.root)
        for tab in self._tabs.values():
            self._refresh_preview(tab)

    def _require_resource_write(self, root: Path) -> None:
        if not self._operation_allowed("resource_write") or self.session is None or self.session.snapshot.root != root:
            raise ProjectError(Diagnostic("OPERATION_BUSY", tr("ui.operation_unavailable"), message_key="ui.operation_unavailable"))

    def _start_image_folder_import(self, dialog: ImageManagerDialog, root: Path, source: Path, folder: str) -> None:
        if not self._operation_allowed("resource_write") or self.session is None or self.session.snapshot.root != root:
            return
        dialog.folder_button.setEnabled(False)
        request = ImportImageFolderRequest(root, source, folder)
        self._start_version_task(
            request,
            lambda cancellation, progress: self.controller.service.import_image_folder(
                request.root, request.source, request.folder, cancellation=cancellation,
                on_progress=lambda completed, total: progress(completed, total, ""),
            ),
        )

    def add_component_dialog(self) -> None:
        if self.session is None:
            return
        sheets = self._available_sheets()
        dialog = ComponentDialog(self, sheets=sheets)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._create_component(dialog.definition())

    def _available_sheets(self) -> tuple[str, ...]:
        if self.session is None or self.controller.preview is None or self.session.snapshot.model.data_source is None:
            return ()
        from componentpress.project_io.paths import resolve_project_path
        try:
            return tuple(self.controller.preview.reader.sheet_names(resolve_project_path(
                self.session.snapshot.root, self.session.snapshot.model.data_source.path)))
        except ProjectError as exc:
            self._add_events((exc.diagnostic,))
            return ()

    def _create_component(self, values: dict, component_id: str | None = None) -> bool:
        if self.session is None or not self._user_editing_allowed():
            return False
        try:
            component_id = self.controller.add_component(
                values["name"], component_id, width_mm=values["width"], height_mm=values["height"], data=values["data"]
            )
        except (ProjectError, RuntimeError, ValidationError) as exc:
            self._show_error(exc)
            return False
        self._refresh_component_tree()
        self._update_project_visibility()
        self.open_component(component_id)
        self._finish_component_creation(component_id)
        self._update_actions()
        return True

    def add_component(self, component_id: str | None = None, name: str | None = None) -> bool:
        if not self._user_editing_allowed():
            return False
        if name is None:
            name, component_id = component_id, None
        if not name or not name.strip():
            self._show_error(ValueError("название компонента не может быть пустым"))
            return False
        try:
            component_id = self.controller.add_component(name.strip(), component_id)
        except (ProjectError, RuntimeError) as exc:
            self._show_error(exc)
            return False
        self._refresh_component_tree()
        self._update_project_visibility()
        self.open_component(component_id)
        self._finish_component_creation(component_id)
        self.statusBar().showMessage("Компонент добавлен", 3000)
        return True

    def _finish_component_creation(self, component_id: str) -> None:
        document = self.session.documents[component_id]
        self._push_component_history(ComponentOperation.CREATE, component_id, document)
        self._project_history_focus = True
        self._validate_active()

    def edit_component_dialog(self, component_id: str) -> None:
        if self.session is None or component_id not in self.session.documents:
            return
        document = self.session.documents[component_id]
        if document.has_invalid_draft or not document.model_ready or document.model is None:
            self._show_error(ProjectError(Diagnostic("INVALID_DRAFT", "Исправьте черновик в режиме «Текст» перед изменением свойств компонента", document.source.path)))
            tab = self._tabs.get(component_id)
            if tab is not None:
                self.tabs.setCurrentWidget(tab)
                tab.set_mode("text")
            return
        dialog = ComponentDialog(self, component=document.model, sheets=self._available_sheets())
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        values = dialog.definition(document.model)
        updated = document.model.model_copy(update={
            "name": values["name"], "size_mm": SizeMM(width=values["width"], height=values["height"]),
            "data": values["data"],
        })
        self._commit_component("Изменить свойства компонента", updated)

    def _make_component_history(self, operation, component_id, document) -> ComponentHistoryEntry:
        return self.controller.component_history_entry(operation, component_id, document)

    def _push_component_history(self, operation, component_id, document) -> None:
        if self.session is None:
            return
        entry = self._make_component_history(operation, component_id, document)
        self._append_component_history(entry, self._tabs.get(component_id))
        self._history_documents[entry.entry_id] = document

    def _append_component_history(self, entry, tab=None) -> None:
        discarded = self._component_history.append(entry)
        self._history_tabs[entry.entry_id] = tab
        for old in discarded:
            self._discard_history_entry(old)

    def _discard_history_entry(self, entry) -> None:
        if self.session is not None:
            self.controller.service.discard_component_history_backup(
                self.session.snapshot.root, entry.backup_path
            )
        tab = self._history_tabs.pop(entry.entry_id, None)
        self._history_documents.pop(entry.entry_id, None)
        if tab is not None and tab not in self._tabs.values() and tab not in self._history_tabs.values():
            tab.deleteLater()

    @property
    def _component_history_index(self) -> int:
        return self._component_history.index

    @_component_history_index.setter
    def _component_history_index(self, value: int) -> None:
        self._component_history.index = value

    def _clear_component_history(self) -> None:
        for entry in self._component_history.clear():
            self._discard_history_entry(entry)

    def delete_component(self, component_id: str, *, confirm: bool = True) -> bool:
        if not self._user_editing_allowed():
            return False
        if not self._component_deletion_ready(component_id):
            return False
        document = self.session.documents[component_id]
        if confirm and not self._confirm_component_delete(document.display_name):
            return False
        if document.draft_diagnostics and not self._confirm_draft_delete():
            return False
        return self._delete_component_from_project(component_id, document)

    def _component_deletion_ready(self, component_id: str) -> bool:
        if self.session is None or component_id not in self.session.documents:
            return False
        if (len(self.session.documents) > 1 and self._active_build_job is None
                and self._build_pending_active_only is None and self._version_task is None):
            return True
        message = "Нельзя удалить последний компонент проекта" if len(self.session.documents) <= 1 else "Дождитесь завершения текущей операции"
        self.statusBar().showMessage(message, 5000)
        return False

    def _confirm_component_delete(self, name: str) -> bool:
        answer = QMessageBox.question(
            self, "Удалить компонент", f"Удалить «{name}»? Операцию можно отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_draft_delete(self) -> bool:
        answer = QMessageBox.warning(
            self, "Черновик компонента", "В компоненте есть некорректный черновик. Он сохранится в истории отмены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _delete_component_from_project(self, component_id: str, document) -> bool:
        entry = self._make_component_history(ComponentOperation.REMOVE, component_id, document)
        self._history_documents[entry.entry_id] = document
        try:
            self._apply_component_membership(entry, remove=True)
        except (ProjectError, OSError) as exc:
            self._history_documents.pop(entry.entry_id, None)
            self._show_error(exc)
            return False
        self._append_component_history(entry, self._history_tabs.get(entry.entry_id))
        self._project_history_focus = True
        self.statusBar().showMessage("Компонент удалён; действие можно отменить", 4000)
        return True

    def _apply_component_membership(self, entry: ComponentHistoryEntry, *, remove: bool) -> None:
        assert self.session is not None
        if remove:
            self._remove_history_component(entry)
        else:
            self._restore_history_component(entry)
        self._refresh_membership_ui()

    def _remove_history_component(self, entry: ComponentHistoryEntry) -> None:
        component_id = entry.component_id
        tab = self._tabs.get(component_id)
        result = self.controller.remove_component_for_history(component_id, backup_path=entry.backup_path)
        entry.position, entry.reference_path = result.position, result.reference_path
        entry.component_hash = result.component_hash
        self._history_tabs[entry.entry_id] = tab or self._history_tabs.get(entry.entry_id)
        self._detach_component_tab(component_id, tab)

    def _restore_history_component(self, entry: ComponentHistoryEntry) -> None:
        self.controller.restore_component_from_history(entry, self._history_documents[entry.entry_id])
        tab = self._history_tabs.get(entry.entry_id)
        if tab is not None:
            self._tabs[entry.component_id] = tab
            self.undo_group.addStack(tab.undo_stack)
            self.tabs.addTab(tab, self._history_documents[entry.entry_id].model.name)
            self._update_tab_title(entry.component_id)

    def _refresh_membership_ui(self) -> None:
        self._refresh_component_tree()
        self._update_project_visibility()
        self._active_tab_changed(self.tabs.currentIndex())
        self._update_actions()

    def _detach_component_tab(self, component_id: str, tab) -> None:
        if tab is None or self._tabs.get(component_id) is not tab:
            return
        tab.preview_timer.stop()
        tab.preview_generation += 1
        tab.canvas.release_preview()
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.removeTab(index)
        self.undo_group.removeStack(tab.undo_stack)
        self._tabs.pop(component_id, None)
        self._preview_coordinator.discard_pending(tab.identity)

    def _undo(self) -> None:
        if not self._user_editing_allowed():
            return
        target = self._history_target(redo=False)
        if target is HistoryTarget.TEXT and self.active_tab is not None:
            self.active_tab.text_editor.undo()
        elif target is HistoryTarget.PROJECT:
            self._undo_component_history()
        elif target is HistoryTarget.DOCUMENT and self.active_tab is not None:
            self.active_tab.undo_stack.undo()
            self._project_history_focus = False
        self._update_actions()

    def _history_target(self, *, redo: bool) -> HistoryTarget:
        tab = self.active_tab
        text_available = bool(tab and (tab.text_editor.document().isRedoAvailable() if redo else tab.text_editor.document().isUndoAvailable()))
        document_available = bool(tab and (tab.undo_stack.canRedo() if redo else tab.undo_stack.canUndo()))
        project_available = bool(self._component_history.redo_entry() if redo else self._component_history.undo_entry())
        return history_target(HistoryContext(bool(tab and tab.mode == "text"), self._project_history_focus,
                                             text_available, document_available, project_available))

    def _undo_component_history(self) -> None:
        entry = self._component_history.undo_entry()
        if self._apply_history_entry(entry, remove=entry.operation is ComponentOperation.CREATE):
            self._component_history.accept_undo()
            self._project_history_focus = True

    def _redo(self) -> None:
        if not self._user_editing_allowed():
            return
        target = self._history_target(redo=True)
        if target is HistoryTarget.TEXT and self.active_tab is not None:
            self.active_tab.text_editor.redo()
        elif target is HistoryTarget.PROJECT:
            self._redo_component_history()
        elif target is HistoryTarget.DOCUMENT and self.active_tab is not None:
            self.active_tab.undo_stack.redo()
            self._project_history_focus = False
        self._update_actions()

    def _redo_component_history(self) -> None:
        entry = self._component_history.redo_entry()
        if self._apply_history_entry(entry, remove=entry.operation is ComponentOperation.REMOVE):
            self._component_history.accept_redo()
            self._project_history_focus = True

    def _apply_history_entry(self, entry, *, remove: bool) -> bool:
        try:
            self._apply_component_membership(entry, remove=remove)
        except (ProjectError, OSError) as exc:
            self._show_error(exc)
            return False
        return True

    def _open_component_index(self, index: QModelIndex) -> None:
        component_id = index.data(ID_ROLE)
        if component_id and self.session is not None and component_id in self.session.documents:
            self.open_component(component_id)

    def _component_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        component_id = current.data(ID_ROLE)
        if component_id and self.session is not None and component_id in self.session.documents:
            self.open_component(component_id)

    def open_component(self, component_id: str) -> DocumentTab | None:
        if self.session is None or component_id not in self.session.documents:
            return None
        existing = self._tabs.get(component_id)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
            return existing
        tab = DocumentTab(component_id)
        self._connect_tab_signals(tab)
        self.undo_group.addStack(tab.undo_stack)
        self._tabs[component_id] = tab
        self._install_component_tab(tab, component_id)
        return tab

    def _connect_tab_signals(self, tab: DocumentTab) -> None:
        self._connect_tab_preview(tab)
        self._connect_canvas_signals(tab)

    def _connect_tab_preview(self, tab: DocumentTab) -> None:
        tab.zoomChanged.connect(self._zoom_changed)
        tab.previewResolutionRequested.connect(self._request_visible_preview_resolution)
        tab.previewRequested.connect(lambda item=tab: self._preview_requested(item))
        tab.preview_first.clicked.connect(lambda: self._step_preview(-1, endpoint="first"))
        tab.preview_previous.clicked.connect(lambda: self._step_preview(-1))
        tab.preview_next.clicked.connect(lambda: self._step_preview(1))
        tab.preview_last.clicked.connect(lambda: self._step_preview(1, endpoint="last"))
        tab.previewError.connect(lambda message: (self.statusBar().showMessage(message, 7000), self._add_events((Diagnostic("PREVIEW_ERROR", message),))))
        tab.modeRequested.connect(lambda mode, item=tab: self._request_mode(item, mode))

    def _connect_canvas_signals(self, tab: DocumentTab) -> None:
        tab.canvas.selectionChanged.connect(self._canvas_selection_changed)
        tab.canvas.nodeContextMenuRequested.connect(self._canvas_context_menu)
        tab.canvas.moveRequested.connect(lambda ids, dx, dy, item=tab: self._move_canvas_nodes(item, item.canvas.dispatch_context, ids, dx, dy))
        tab.canvas.nudgeRequested.connect(lambda ids, dx, dy, item=tab: self._move_canvas_nodes(item, item.canvas.dispatch_context, ids, dx, dy))
        tab.canvas.resizeRequested.connect(lambda node_id, width, height, ax, ay, item=tab: self._resize_canvas_node(item, item.canvas.dispatch_context, node_id, width, height, ax, ay))
        tab.canvas.lineResizeRequested.connect(self._resize_line_selected)
        tab.canvas.placementRequested.connect(self._place_element)
        tab.canvas.drawRequested.connect(lambda kind, x1, y1, x2, y2, item=tab: self._draw_canvas_element(item, item.canvas.dispatch_context, kind, x1, y1, x2, y2))
        tab.canvas.toolCancelled.connect(self._tool_cancelled)
        tab.canvas.deleteRequested.connect(self.delete_selected)

    def _install_component_tab(self, tab: DocumentTab, component_id: str) -> None:
        # Seed the editor before adding it to the tab widget: palette events can
        # rehighlight it synchronously, and must not turn an empty editor into a draft.
        document = self.session.documents[component_id]
        tab.set_text(document.current_text)
        self._show_document_diagnostics(tab, document.current_diagnostics)
        if not document.model_ready:
            tab.set_mode("text")
            tab.layout_mode.setEnabled(False)
        tab.text_editor.installEventFilter(self)
        tab.draftChanged.connect(lambda text, item=tab: self._draft_changed(item, text))
        index = self.tabs.addTab(tab, "")
        self._update_tab_title(component_id)
        self.tabs.setCurrentIndex(index)
        self._refresh_preview(tab, fit=True)

    def _active_tab_changed(self, _index: int) -> None:
        for candidate in self._tabs.values():
            if candidate is not self.active_tab:
                candidate.canvas.cancel_tool()
                candidate.canvas.cancel_gesture()
                candidate.canvas.release_preview()
        tab = self.active_tab
        self._sync_component_tree_selection()
        if tab is None or self.session is None:
            self._clear_active_tab_ui()
        else:
            self._show_active_tab(tab)
        self._update_actions()

    def _clear_active_tab_ui(self) -> None:
        self.undo_group.setActiveStack(None)
        self.element_tree.setModel(None)
        self.properties.show_component(None)
        self.element_properties.show_node(None)
        self.component_properties_group.show()
        self.element_properties_group.hide()
        self.status_zoom.setText("")

    def _show_active_tab(self, tab: DocumentTab) -> None:
        document = self.session.documents[tab.component_id]
        self.undo_group.setActiveStack(tab.undo_stack)
        tab.set_text(document.current_text)
        self._show_document_diagnostics(tab, document.current_diagnostics)
        tab.layout_mode.setEnabled(document.model_ready)
        if not document.model_ready:
            self._show_invalid_component_tab(tab)
            return
        valid_ids = locations(document.model)
        selected = tuple(node_id for node_id in tab.canvas.selected_ids if node_id in valid_ids)
        self._install_element_model(document.model, selected)
        self.properties.show_component(document.model)
        self.status_zoom.setText(tr("ui.zoom_status", percent=tab.canvas.zoom_percent))
        self._populate_data_panel()
        self._project_history_focus = False
        self._refresh_preview(tab)

    def _show_invalid_component_tab(self, tab: DocumentTab) -> None:
        tab.set_mode("text")
        self.element_tree.setModel(None)
        self.properties.show_component(None)
        self.element_properties.show_node(None)
        self.element_properties_group.hide()
        self.component_properties_group.show()
        tab.resolved_component = None
        self._clear_preview(tab, "", tr("ui.component_text_only"))
        self._populate_data_panel()
        self.status_zoom.clear()
        self._project_history_focus = False

    @staticmethod
    def _fill_combo(combo: QComboBox, values: list[tuple[str, object]], current: object = None) -> None:
        blocked = combo.blockSignals(True)
        combo.clear()
        selected = 0
        for index, (label, value) in enumerate(values):
            combo.addItem(label, value)
            if value == current:
                selected = index
        combo.setCurrentIndex(selected if values else -1)
        combo.blockSignals(blocked)

    def _populate_data_panel(self) -> None:
        tab = self.active_tab
        if self.session is None or tab is None:
            return
        document = self.session.documents[tab.component_id]
        if not document.model_ready or document.model is None:
            self._disable_data_panel()
            return
        binding = document.model.data
        source = self.session.snapshot.model.data_source
        sources = [(tr("ui.data_no_xlsx"), None)] + ([(source.path, "main")] if source else [])
        self._fill_combo(self.data_source, sources, "main" if source else None)
        sheets = self._data_sheet_options(source)
        self._fill_combo(self.data_sheet, sheets, binding.sheet if binding else None)
        self.properties.set_sheets(tuple(str(value) for _, value in sheets if value))
        self.properties.show_component(document.model)
        data = self._refresh_data(force=False, render=False) if binding is not None else None
        headers = list(data.headers) if data is not None else []
        self._fill_combo(self.data_column, [(header, header) for header in headers], self.data_column.currentData())
        self._update_data_panel_controls(data, binding is not None)

    def _disable_data_panel(self) -> None:
        controls = (self.data_source, self.data_sheet, self.data_row, self.data_column,
                    self.data_refresh, self.data_validate, self.data_validate_all,
                    self.data_insert, self.data_export, self.data_export_zip)
        for control in controls:
            control.setEnabled(False)

    def _data_sheet_options(self, source) -> list[tuple[str, object]]:
        if source is None or self.controller.preview is None:
            return []
        try:
            from componentpress.project_io.paths import resolve_project_path
            path = resolve_project_path(self.session.snapshot.root, source.path)
            return [(name, name) for name in self.controller.preview.reader.sheet_names(path)]
        except ProjectError as exc:
            self.statusBar().showMessage(format_diagnostic(exc.diagnostic), 7000)
            return []

    def _update_data_panel_controls(self, data, has_binding: bool) -> None:
        self._populate_rows(data)
        self._update_data_chain()
        enabled = has_binding and data is not None
        for control in (self.data_row, self.data_column, self.data_insert, self.data_export):
            control.setEnabled(enabled)

    def _populate_rows(self, data) -> None:
        tab = self.active_tab
        current = tab.preview_row_number if tab is not None else None
        mode = self.data_mode.mode
        values = [] if data is None else [
            (tr("ui.row_copy_count", id=row.instance_id, row=row.row_number, copies=copies_for_mode(row, mode)), row.row_number)
            for row in data.rows if copies_for_mode(row, mode) > 0
        ]
        self._fill_combo(self.data_row, values, current)
        if tab is not None and values:
            tab.preview_row_number = int(self.data_row.currentData())

    def _data_mode_changed(self, _index: int) -> None:
        data = None
        if self.session is not None and self.active_tab is not None and self.controller.preview is not None:
            data = self.controller.preview.cached_data(self.session.snapshot, self.active_tab.component_id)
        self._populate_rows(data)
        if self.active_tab is not None:
            self._refresh_preview(self.active_tab)
            self._update_preview_navigation()

    def _add_events(self, diagnostics) -> None:
        reveal = False
        for diagnostic in diagnostics:
            technical_log.log_event("project.diagnostic", code=getattr(diagnostic, "code", "UNKNOWN"), status=getattr(diagnostic, "severity", "error"))
            item = QListWidgetItem()
            root = self.session.snapshot.root if self.session is not None else None
            item.setData(Qt.ItemDataRole.UserRole, (diagnostic, root))
            item.setToolTip(format_diagnostic(diagnostic))
            self.event_list.addItem(item)
            if getattr(diagnostic, "severity", "error") != "info":
                reveal = True
        self._format_event_items()
        self._filter_events(self.event_search.text())
        if reveal:
            self.events_dock.show()

    @staticmethod
    def _show_document_diagnostics(tab: DocumentTab, diagnostics) -> None:
        items = tuple(diagnostics)
        message = "\n".join(format_diagnostic(item) for item in items)
        tab.set_diagnostics(items, message)

    def _format_event_items(self) -> None:
        for index in range(self.event_list.count()):
            item = self.event_list.item(index)
            stored = item.data(Qt.ItemDataRole.UserRole)
            if not stored:
                continue
            diagnostic, root = stored
            item.setText(format_diagnostic_event(
                diagnostic, project_root=root,
                hide_project_path=self.hide_event_project_path.isChecked(),
            ))
            item.setToolTip(format_diagnostic(diagnostic))
        self._update_latest_event_status()
        self._filter_events(self.event_search.text())

    def _update_latest_event_status(self) -> None:
        latest = self.event_list.item(self.event_list.count() - 1)
        text = latest.text() if latest is not None else ""
        self.status_event.setText(self.status_event.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideMiddle, self.status_event.maximumWidth(),
        ))
        self.status_event.setToolTip(latest.toolTip() if latest is not None else "")

    def _copy_selected_event(self) -> None:
        item = self.event_list.currentItem()
        if item is None or item.isHidden():
            self._update_copy_event_enabled()
            return
        QApplication.clipboard().setText(item.text())
        point = self.copy_event.mapToGlobal(QPoint(0, self.copy_event.height()))
        QToolTip.showText(point, tr("event.copied"), self.copy_event)

    def _clear_events(self) -> None:
        self.event_list.clear()
        self.status_event.clear()
        self.status_event.setToolTip("")
        self._update_copy_event_enabled()

    def _update_copy_event_enabled(self) -> None:
        item = self.event_list.currentItem()
        self.copy_event.setEnabled(item is not None and not item.isHidden())

    def _snapshot_for_current_documents(self):
        if self.session is None:
            return None
        documents = {component_id: document.committed for component_id, document in self.session.documents.items()}
        from componentpress.application.contracts import ProjectSnapshot
        base = self.session.snapshot
        return ProjectSnapshot(base.root, base.text, base.disk_hash, base.model, MappingProxyType(documents))

    def _filter_events(self, query: str) -> None:
        needle = query.casefold().strip()
        for index in range(self.event_list.count()):
            item = self.event_list.item(index)
            item.setHidden(needle not in item.text().casefold())
        self._update_copy_event_enabled()

    def _validate_active(self) -> None:
        if self.session is None or self.active_tab is None or self.controller.preview is None:
            return
        component_id = self.active_tab.component_id
        if not self._apply_draft(component_id):
            return
        document = self.session.documents[component_id]
        if not document.model_ready or document.model is None:
            return
        component = document.model
        result = self._active_validation_result(component_id, component)
        if result is None:
            return
        snapshot, data = result
        self._record_active_validation(snapshot, component_id, component, data)
        self._populate_data_panel()
        self._refresh_preview(self.active_tab)

    def _active_validation_result(self, component_id, component):
        try:
            snapshot = self._snapshot_for_current_documents()
            if snapshot is None:
                return None
            diagnostics = self.controller.preview.validate_all(snapshot, manual=True, component_ids=(component_id,))
            if self._show_active_validation_diagnostics(snapshot, component_id, diagnostics):
                return None
            data = self.controller.preview.cached_data(snapshot, component_id, component=component)
        except ValidationIssues as exc:
            self._add_events(exc.diagnostics)
            self.statusBar().showMessage(tr("ui.error_count", count=len(exc.diagnostics)), 7000)
            return None
        except ProjectError as exc:
            self._add_events((exc.diagnostic,))
            self.statusBar().showMessage(tr("ui.validation_failed"), 7000)
            return None
        return snapshot, data

    def _record_active_validation(self, snapshot, component_id, component, data):
        if data is None:
            self._add_events((Diagnostic("VALIDATION_OK", "", snapshot.documents[component_id].path, source="main", severity="info", message_key="ui.validation_component_ok", params={"name": component.name}),))
        else:
            self._add_events((Diagnostic("VALIDATION_OK", "", data.path, source="main", sheet=data.sheet, severity="info", message_key="ui.validation_sheet_ok", params={"sheet": data.sheet, "area": data.area, "count": len(data.rows)}),))

    def _show_active_validation_diagnostics(self, snapshot, component_id, diagnostics) -> bool:
        if not diagnostics:
            return False
        other_paths = {document.path for key, document in snapshot.documents.items() if key != component_id}
        current_failed = any(item.path == snapshot.documents[component_id].path for item in diagnostics)
        self._add_events(diagnostics)
        if current_failed or not all(item.path in other_paths for item in diagnostics):
            self.statusBar().showMessage(tr("ui.error_count", count=len(diagnostics)), 7000)
            return True
        self.statusBar().showMessage(tr("ui.validation_other_components"), 7000)
        return False

    def _validate_all(self, *, force_read: bool = False) -> None:
        if self.session is None or self.controller.preview is None:
            return
        for component_id in tuple(self.session.documents):
            if not self._apply_draft(component_id):
                return
        snapshot = self._snapshot_for_current_documents()
        if snapshot is None:
            return
        diagnostics = self.controller.preview.validate_all(snapshot, force_read=force_read, manual=True)
        self._add_events(diagnostics or (Diagnostic("VALIDATION_OK", "", self.session.snapshot.root, severity="info", message_key="ui.validation_all_ok"),))
        if diagnostics:
            self.statusBar().showMessage(tr("ui.error_count", count=len(diagnostics)), 7000)
        else:
            self.statusBar().showMessage(tr("ui.validation_all_ok"), 5000)
        if self.active_tab is not None:
            self._populate_data_panel()
            self._refresh_preview(self.active_tab)

    def _refresh_data(self, *, force: bool, render: bool = True):
        tab = self.active_tab
        if self.session is None or tab is None or self.controller.preview is None:
            return None
        document = self.session.documents[tab.component_id]
        if not document.model_ready or document.model is None or document.model.data is None:
            return None
        try:
            data = self._read_data_snapshot(tab, document.model, force)
        except ProjectError as exc:
            self._add_events(exc.diagnostics if isinstance(exc, ValidationIssues) else (exc.diagnostic,))
            self.statusBar().showMessage(format_diagnostic(exc.diagnostic), 10000)
            return None
        if data is not None:
            self._show_refreshed_data(tab, data, render)
        return data

    def _show_refreshed_data(self, tab, data, render):
        self._populate_rows(data)
        self._fill_combo(self.data_column, [(header, header) for header in data.headers], self.data_column.currentData())
        if render:
            self._refresh_preview(tab)
        self.statusBar().showMessage(tr("ui.data_updated", sheet=data.sheet, version=data.version), 3500)

    def _read_data_snapshot(self, tab: DocumentTab, component: ComponentDefinition, force: bool):
        if force:
            diagnostics = self.controller.preview.validate_all(
                self._snapshot_for_current_documents(), manual=True, component_ids=(tab.component_id,),
            )
            if diagnostics:
                self._add_events(diagnostics)
                self.statusBar().showMessage(format_diagnostic(diagnostics[0]), 10000)
                return None
        return self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)

    def _refresh_preview(self, tab: DocumentTab, *, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        if self.session is None:
            return False
        document = self.session.documents[tab.component_id]
        if not document.model_ready: return self._clear_invalid_component_preview(tab)
        if document.model.data is None or self.controller.preview is None:
            active_component, source_label, bindings_resolved = self._static_preview(tab, document.model)
        else:
            result = self._bound_preview(tab, document.model)
            if result is None:
                return False
            active_component, source_label, bindings_resolved = result
        dpi = self._preview_dpi_for_visible_views(tab, active_component)
        tab.queue_preview(
            self.session, component=active_component, dpi=dpi, fit=fit,
            selected=selected, bindings_resolved=bindings_resolved,
            source_label=source_label,
        )
        self._update_preview_navigation(tab)
        return True

    def _clear_invalid_component_preview(self, tab: DocumentTab) -> bool:
        tab.resolved_component = None
        self._clear_preview(tab, "", tr("ui.component_text_only"))
        return False

    def _static_preview(self, tab: DocumentTab, component: ComponentDefinition):
        tab.resolved_component = None
        self._update_preview_navigation(tab)
        return component, tr("ui.preview_source_static"), False

    def _bound_preview(self, tab: DocumentTab, component: ComponentDefinition):
        source = self.session.snapshot.model.data_source
        source_path = source.path if source is not None else ""
        label = tr("ui.preview_source_excel", path=source_path, sheet=component.data.sheet, row="—")
        tab.resolved_component = None
        cached = self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)
        if cached is None:
            self._clear_preview(tab, label, tr("ui.preview_source_no_data"))
            return None
        eligible = [row for row in cached.rows if copies_for_mode(row, self.data_mode.mode) > 0]
        if not eligible:
            tab.preview_row_number = None
            self._clear_preview(tab, label, tr("ui.preview_source_no_instances"))
            return None
        self._select_preview_row(tab, component, eligible)
        return self._resolve_bound_preview(tab, component, cached, label)

    def _select_preview_row(self, tab: DocumentTab, component: ComponentDefinition, eligible) -> None:
        if tab.preview_row_number in {row.row_number for row in eligible}:
            return
        tab.preview_row_number = eligible[0].row_number

    def _resolve_bound_preview(self, tab: DocumentTab, component: ComponentDefinition, cached, label: str):
        try:
            resolved = self.controller.preview.select_row(
                self.session.snapshot, tab.component_id, row_number=tab.preview_row_number,
                mode=self.data_mode.mode, component=component, validate_resources=False,
            )
        except ProjectError as exc:
            tab.fail_preview_source(label)
            tab.previewError.emit(format_diagnostic(exc.diagnostic))
            if self.active_tab is tab and len(tab.canvas.selected_ids) == 1:
                self._show_selection(tab.canvas.selected_ids)
            return None
        tab.preview_row_number = resolved.row.row_number if resolved.row else None
        label = self._resolved_preview_label(cached, resolved, label)
        tab.resolved_component = resolved.component
        return resolved.component, label, True

    def _resolved_preview_label(self, cached, resolved, fallback: str) -> str:
        if resolved.row is None:
            return fallback
        try:
            path = cached.path.resolve().relative_to(self.session.snapshot.root.resolve()).as_posix()
        except (OSError, ValueError):
            path = cached.path.as_posix()
        return tr("ui.preview_source_excel", path=path, sheet=cached.sheet, row=resolved.row.row_number)

    def _clear_preview(self, tab: DocumentTab, source_label: str = "", status: str = "") -> None:
        tab.invalidate_preview()
        tab.set_preview_source(source_label, status)
        tab.canvas.clear_document()
        self._update_preview_navigation(tab)

    def _step_preview(self, direction: int, *, endpoint: str | None = None) -> None:
        tab = self.active_tab
        if self.session is None or tab is None or tab.resolved_component is None or self.controller.preview is None:
            return
        document = self.session.documents[tab.component_id]
        component = document.model
        if not document.model_ready or component is None or component.data is None:
            return
        if (data := self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)) is None: return
        eligible = [row for row in data.rows if copies_for_mode(row, self.data_mode.mode) > 0]
        if not eligible:
            return
        current = next((i for i, row in enumerate(eligible) if row.row_number == tab.preview_row_number), 0)
        next_index = 0 if endpoint == "first" else len(eligible) - 1 if endpoint == "last" else current + direction
        if not 0 <= next_index < len(eligible):
            return
        tab.preview_row_number = eligible[next_index].row_number
        self._populate_rows(data)
        self._refresh_preview(tab)

    def _preview_dpi_for_visible_views(self, tab: DocumentTab, component: ComponentDefinition) -> int:
        demand = tab.canvas.transform().m11() * tab.canvas.viewport().devicePixelRatioF() if tab.mode == "layout" else 1.0
        return tab._preview_dpi(component, demand, 1.0)

    def _request_visible_preview_resolution(self) -> None:
        tab = self.active_tab
        if tab is None or tab.preview_arguments is None or self.session is None:
            return
        component = tab.preview_arguments.component
        target_dpi = self._preview_dpi_for_visible_views(tab, component)
        tab.update_preview_resolution(target_dpi)

    def _invalidate_preview_cache(self) -> None:
        if self.controller.preview is not None and self.session is not None:
            self.controller.preview.invalidate(self.session.snapshot.root)
        self._validate_all()

    def _update_preview_navigation(self, tab: DocumentTab | None = None) -> None:
        tab = tab or self.active_tab
        if tab is None or self.session is None:
            return
        document = self.session.documents.get(tab.component_id)
        data = None
        if self.controller.preview is not None:
            data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id)
        if data is None:
            position = tr("ui.instance_position", current=1, total=1) if document and document.model is not None and document.model.data is None else tr("ui.instance_position_empty")
            tab.instance_position.setText(position)
            tab.instance_position.setAccessibleName(position)
            for button in (tab.preview_first, tab.preview_previous, tab.preview_next, tab.preview_last):
                button.setEnabled(False)
            return
        eligible = [row for row in data.rows if copies_for_mode(row, self.data_mode.mode) > 0]
        index = next((i for i, row in enumerate(eligible) if row.row_number == tab.preview_row_number), -1)
        self._set_preview_navigation_state(tab, index, len(eligible))

    @staticmethod
    def _set_preview_navigation_state(tab: DocumentTab, index: int, total: int) -> None:
        position = tr("ui.instance_position", current=index + 1, total=total) if index >= 0 else tr("ui.instance_position_empty")
        tab.instance_position.setText(position)
        tab.instance_position.setAccessibleName(position)
        tab.preview_previous.setEnabled(index > 0)
        tab.preview_next.setEnabled(0 <= index < total - 1)
        tab.preview_first.setEnabled(index > 0)
        tab.preview_last.setEnabled(0 <= index < total - 1)

    @staticmethod
    def _preview_font_families(component: ComponentDefinition) -> set[str]:
        families: set[str] = set()

        def visit(nodes) -> None:
            for node in nodes:
                if isinstance(node, HtmlNode):
                    families.add(node.font_family)
                elif isinstance(node, GroupNode):
                    visit(node.children)

        visit(component.elements)
        return families

    def _preview_requested(self, tab: DocumentTab) -> None:
        if self._tabs.get(tab.component_id) is not tab or self.session is None:
            return
        if self._build_pending_active_only is not None or self._png_operation is not None or self._version_busy_id is not None:
            tab.preview_deferred = True
            return
        if self.active_tab is not tab or self._active_build_job is not None:
            tab.preview_deferred = True
            return
        if self._preview_coordinator.closed:
            return
        request = tab.preview_request_data(self._preview_session_identity)
        if request is None:
            return
        fonts = ProjectFonts(request.snapshot.root, self._preview_font_families(request.component))
        job = PreviewJob(request, tab, fonts)
        self._start_preview_job(self._preview_coordinator.submit(job))

    def _start_preview_job(self, job: PreviewJob | None) -> None:
        if job is None:
            return
        request = job.request
        if self.active_tab is not job.tab or self._active_build_job is not None or self._build_pending_active_only is not None or not self._preview_is_current(job):
            self._skip_preview_job(job)
            return
        try:
            self._preview_pool.start(PreviewFontReadTask(request, self._preview_signals))
        except Exception as exc:
            self._finish_preview_failure(job, PreviewPhase.FONTS, exc)

    def _preview_task_finished(self, result: PreviewTaskResult) -> None:
        if self._preview_coordinator.closed:
            self._finish_preview_on_close(result)
            return
        if isinstance(result, (FontReadSucceeded, FontReadFailed)):
            self._preview_fonts_finished(result)
        elif isinstance(result, (PreviewRenderSucceeded, PreviewRenderFailed)):
            self._preview_render_finished(result)

    def _finish_preview_on_close(self, result) -> None:
        phase = PreviewPhase.FONTS if isinstance(result, (FontReadSucceeded, FontReadFailed)) else PreviewPhase.RENDER
        identity = result.identity
        if not self._preview_coordinator.accepts(identity.request, phase):
            return
        job = self._preview_coordinator.active
        assert job is not None
        self._preview_coordinator.finish(identity.request, phase)
        self._release_preview_fonts(job)
        if self._preview_close_pending:
            self._preview_close_pending = False
            QTimer.singleShot(0, self.close)

    def _preview_fonts_finished(self, result: FontReadSucceeded | FontReadFailed) -> None:
        request_id = result.identity.request
        if not self._preview_coordinator.accepts(request_id, PreviewPhase.FONTS):
            return
        job = self._preview_coordinator.active
        assert job is not None
        if not self._preview_is_current(job) or self.active_tab is not job.tab or self._active_build_job is not None or self._build_pending_active_only is not None:
            self._skip_preview_job(job)
            return
        if isinstance(result, FontReadFailed):
            self._finish_preview_failure(job, PreviewPhase.FONTS, result.error)
            return
        try:
            job.fonts.register_captured(result.files)
            self._require_preview_fonts(job)
        except Exception as exc:
            self._finish_preview_failure(job, PreviewPhase.FONTS, exc)
            return
        self._start_preview_render(self._preview_coordinator.begin_render(request_id))

    def _require_preview_fonts(self, job: PreviewJob) -> None:
        for family in self._preview_font_families(job.request.component):
            job.fonts.require(family, owner=job.request.snapshot.root / PROJECT_MANIFEST, field="font_family")

    def _start_preview_render(self, job: PreviewJob | None) -> None:
        if job is None:
            return
        try:
            self._preview_pool.start(PreviewTask(job.request, self._preview_signals))
        except Exception as exc:
            self._finish_preview_failure(job, PreviewPhase.RENDER, exc)

    def _preview_render_finished(self, result: PreviewRenderSucceeded | PreviewRenderFailed) -> None:
        phase, request_id = PreviewPhase.RENDER, result.identity.request
        if not self._preview_coordinator.accepts(request_id, phase):
            return
        job = self._preview_coordinator.active
        assert job is not None
        transition = self._preview_coordinator.finish(request_id, phase)
        self._release_preview_fonts(job)
        if isinstance(result, PreviewRenderSucceeded):
            self._apply_preview_result(job, result)
        elif self._preview_is_current(job) and self.active_tab is job.tab:
            self._show_preview_failure(job, result.error)
        self._start_preview_job(transition.start)
        self._continue_build_after_preview()
        self._continue_png_after_preview()
        self._continue_pending_restore()

    def _finish_preview_failure(self, job: PreviewJob, phase: PreviewPhase, error: Exception) -> None:
        transition = self._preview_coordinator.finish(job.request.identity.request, phase)
        if transition.finished is None:
            return
        self._release_preview_fonts(job)
        if self._preview_is_current(job) and self.active_tab is job.tab:
            self._show_preview_failure(job, error)
        self._start_preview_job(transition.start)
        self._continue_build_after_preview()
        self._continue_png_after_preview()
        self._continue_pending_restore()

    def _skip_preview_job(self, job: PreviewJob) -> None:
        transition = self._preview_coordinator.finish(job.request.identity.request, PreviewPhase.FONTS)
        self._release_preview_fonts(job)
        if self._tabs.get(job.request.identity.component_id) is job.tab and (
            self.active_tab is not job.tab or self._active_build_job is not None
        ):
            job.tab.preview_deferred = True
        self._start_preview_job(transition.start)
        self._continue_build_after_preview()
        self._continue_png_after_preview()
        self._continue_pending_restore()

    def _release_preview_fonts(self, job: PreviewJob) -> None:
        job.fonts.close()

    def _continue_build_after_preview(self) -> None:
        if self._build_pending_active_only is not None and self._preview_coordinator.active is None:
            self._launch_reserved_build(self._build_pending_active_only)

    def _show_preview_failure(self, job: PreviewJob, error: Exception) -> None:
        message = format_diagnostic(error.diagnostic) if isinstance(error, ProjectError) else str(error)
        if job.tab.fail_preview(message, generation=job.request.identity.generation):
            self.event_list.setCurrentRow(self.event_list.count() - 1)
            self.event_list.setFocus()

    def _preview_is_current(self, job: PreviewJob) -> bool:
        identity, tab = job.request.identity, job.tab
        if self.session is None or identity.project != self._preview_session_identity:
            return False
        if self._tabs.get(identity.component_id) is not tab or tab.identity != identity.tab:
            return False
        return self.session.documents[identity.component_id].revision == identity.revision and tab.preview_generation == identity.generation

    def _apply_preview_result(self, job: PreviewJob, result: PreviewRenderSucceeded) -> None:
        if not self._preview_is_current(job):
            return
        if self.active_tab is not job.tab:
            job.tab.preview_deferred = True
            return
        if not job.tab.apply_preview(result.image, result.quality, generation=job.request.identity.generation):
            return
        self._update_preview_navigation(job.tab)
        self._update_selected_image_quality(job.tab)
        self._show_preview_quality(job, result.quality)

    def _show_preview_quality(self, job: PreviewJob, quality) -> None:
        diagnostics = self._preview_quality_diagnostics(job.request, quality)
        fingerprint = tuple((item.node_id, item.source, item.message, item.cell) for item in diagnostics)
        if fingerprint == job.tab.quality_warning_fingerprint:
            return
        job.tab.quality_warning_fingerprint = fingerprint
        if diagnostics:
            self._add_events(diagnostics)
            self.statusBar().showMessage(tr("ui.preview_quality_count", count=len(diagnostics)), 6000)

    def _preview_quality_diagnostics(self, request: PreviewRequest, quality) -> tuple[Diagnostic, ...]:
        document = request.snapshot.documents[request.identity.component_id]
        if document.model is None:
            return ()
        binding = document.model.data
        data = None
        row = None
        if binding is not None and self.controller.preview is not None:
            data = self.controller.preview.cached_data(request.snapshot, request.identity.component_id, component=document.model)
            if data is not None:
                row = next((item for item in data.rows if item.row_number == request.row_number), None)
        source = binding.source if binding is not None else None
        sheet = binding.sheet if binding is not None else None
        indexed = locations(document.model)
        return tuple(
            warning for item in quality
            if (warning := self._quality_warning(item, document.path, source, sheet, indexed, row)) is not None
        )

    def _quality_warning(self, item, path, source, sheet, indexed, row):
        warning = item.diagnostic(path, source=source, sheet=sheet)
        found = indexed.get(item.node_id)
        if warning is None or row is None or found is None:
            return warning
        column = self._quality_source_column(found.node)
        cell = self._quality_cell(row, column)
        message = tr(warning.message_key, **(warning.params or {})) if warning.message_key else warning.message
        return Diagnostic(
            warning.code, "", warning.path, warning.field, warning.line, warning.severity,
            warning.source, warning.sheet, cell, warning.node_id,
            message_key="ui.excel_row_prefix", params={"row": row.row_number, "message": message},
        )

    @staticmethod
    def _quality_source_column(node):
        if isinstance(node, ImageNode) and node.source_mode == "column":
            return node.source_column
        if isinstance(node, HtmlNode) and node.content_mode == "column":
            return node.content_column
        return None

    @staticmethod
    def _quality_cell(row, column):
        return next((value.coordinate for name, value in row.values.items() if column and name.strip().casefold() == column.strip().casefold()), None)

    def _row_changed(self, _index: int) -> None:
        tab = self.active_tab
        if tab is None:
            return
        value = self.data_row.currentData()
        tab.preview_row_number = int(value) if value is not None else None
        self._refresh_preview(tab)
        self._update_preview_navigation()
        self._update_data_chain()

    def _data_binding_changed(self, _index: int) -> None:
        if self.session is None or self.active_tab is None or not self._user_editing_allowed():
            return
        sheet = self.data_sheet.currentData()
        if not sheet:
            binding = None
        else:
            binding = DataBinding(sheet=str(sheet))
        try:
            self.controller.update_component(self.active_tab.component_id, data=binding)
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.active_tab.preview_row_number = None
        self._populate_data_panel()
        self._refresh_data(force=True)

    def _update_data_chain(self, _index: int = -1) -> None:
        source = self.data_source.currentData()
        sheet = self.data_sheet.currentData()
        column = self.data_column.currentData()
        self.data_chain.setText(" → ".join(str(item) for item in (source, sheet, column) if item) or tr("ui.no_binding"))

    def _insert_column_binding(self) -> None:
        if self.session is None or self.active_tab is None:
            return
        column = self.data_column.currentData()
        selected = self._selected_ids()
        if not column or len(selected) != 1:
            self.statusBar().showMessage(tr("ui.select_html_column"), 5000)
            return
        item = locations(self.session.documents[self.active_tab.component_id].model).get(selected[0])
        if item is None or not isinstance(item.node, HtmlNode):
            self.statusBar().showMessage(tr("ui.column_binding_html"), 5000)
            return
        self._edit_selected_node({"content_mode": "column", "content_column": str(column)})

    def _import_xlsx(self) -> None:
        if self.session is None or not self._operation_allowed("resource_write"):
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт XLSX", "", "Excel (*.xlsx)")
        if not filename or not self._operation_allowed("resource_write"):
            return
        try:
            self.controller.import_data(Path(filename))
        except ProjectError as exc:
            self._show_error(exc)
            return
        self._populate_data_panel()
        self._refresh_component_tree()

    def _clear_xlsx(self) -> None:
        if self.session is None or not self._operation_allowed("resource_write"):
            return
        choice = QMessageBox.question(self, tr("ui.remove_xlsx"), "Отвязать все листы компонентов и убрать источник Excel из проекта?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
            return
        if not self._operation_allowed("resource_write"):
            return
        try:
            for component_id, document in tuple(self.session.documents.items()):
                if document.model is not None and document.model.data is not None:
                    self.controller.update_component(component_id, data=None)
            self.controller.save_all()
            self.controller.service.clear_data_source(self.session)
            self._populate_data_panel()
        except ProjectError as exc:
            self._show_error(exc)

    def _export_instance(self) -> None:
        if self.session is None or self.active_tab is None or self.controller.preview is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Экспорт экземпляра", f"{self.active_tab.component_id}.png", "PNG (*.png)")
        if not filename:
            return
        try:
            result = self.controller.preview.export_png(
                self.session.snapshot, self.active_tab.component_id, Path(filename),
                row_number=self.active_tab.preview_row_number,
                mode=self.data_mode.mode,
                component=self.session.documents[self.active_tab.component_id].model,
            )
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.statusBar().showMessage(f"Экспортировано: {result.png}", 5000)

    def _export_png_archive(self) -> None:
        if self.session is None or not self._operation_allowed("png_export"):
            return
        operation = PngArchiveOperation(uuid4().hex, self.session, self._preview_session_identity, self.active_tab,
            self.data_mode.mode, cancellation=CancellationToken())
        self._png_operation = operation
        technical_log.log_event("project.png_export.admission", operation="png_export", operation_id=operation.operation_id, status="started", phase="preparing", mode=operation.mode)
        self._update_actions()
        if (not self._save_all_for_reservation(operation.operation_id) or not self._png_operation_is_current(operation)
                or operation.cancellation.cancelled):
            self._finish_png_operation(operation, "cancelled")
            return
        operation.snapshot = operation.session.snapshot
        operation.phase = "waiting_preview"
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_deferred = tab.preview_arguments is not None
        self._preview_coordinator.discard_pending()
        self._continue_png_after_preview()

    def _continue_png_after_preview(self) -> None:
        operation = self._png_operation
        if operation is None or self._preview_coordinator.active is not None:
            return
        if operation.cancellation.cancelled:
            self._finish_png_operation(operation, "cancelled")
            return
        if self._png_operation_is_current(operation):
            QTimer.singleShot(0, lambda item=operation: self._choose_png_archive(item))

    def _choose_png_archive(self, operation: PngArchiveOperation) -> None:
        if not self._png_operation_is_current(operation):
            return
        operation.phase = "choosing"
        choice, active_button, project_button = self._png_choice_dialog(operation)
        choice.exec()
        self._accept_png_choice(operation, choice, active_button, project_button)

    def _png_choice_dialog(self, operation: PngArchiveOperation):
        choice = QMessageBox(self)
        choice.setWindowTitle(tr("ui.png_export_title"))
        choice.setText(tr("ui.png_export_choice"))
        active = choice.addButton(tr("ui.png_active"), QMessageBox.ButtonRole.AcceptRole) if operation.active_tab is not None else None
        project = choice.addButton(tr("ui.png_all"), QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        return choice, active, project

    def _accept_png_choice(self, operation, choice, active_button, project_button) -> None:
        if not self._png_operation_is_current(operation) or operation.cancellation.cancelled:
            self._finish_png_operation(operation, "cancelled")
            return
        clicked = choice.clickedButton()
        if clicked not in (active_button, project_button):
            self._finish_png_operation(operation, "cancelled")
            return
        operation.component_ids = (operation.active_tab.component_id,) if clicked is active_button else None
        self._select_png_target(operation, self._png_target_name(operation))

    @staticmethod
    def _png_target_name(operation: PngArchiveOperation) -> str:
        component_id = operation.component_ids[0] if operation.component_ids else operation.session.snapshot.model.id
        return f"{component_id}-{operation.mode}.zip"

    def _select_png_target(self, operation: PngArchiveOperation, target_name: str) -> None:
        if not self._png_operation_is_current(operation) or operation.cancellation.cancelled:
            self._finish_png_operation(operation, "cancelled")
            return
        operation.phase = "selecting_target"
        filename, _ = QFileDialog.getSaveFileName(self, tr("ui.png_save"), target_name, "ZIP (*.zip)")
        if not filename or not self._png_operation_is_current(operation) or operation.cancellation.cancelled:
            self._finish_png_operation(operation, "cancelled")
            return
        operation.target = Path(filename)
        self._run_png_archive(operation)

    def _run_png_archive(self, operation: PngArchiveOperation) -> None:
        operation.phase = "running"
        self._show_png_progress(operation)
        try:
            result = self._call_png_exporter(operation)
        except Exception as exc:
            result = exc
        self._report_png_result(result)
        self._finish_png_operation(operation, self._png_result_status(result))

    def _show_png_progress(self, operation: PngArchiveOperation) -> None:
        progress = QProgressDialog(tr("ui.archive_prepare"), tr("action.cancel_build"), 0, 0, self)
        operation.progress = progress
        progress.setWindowTitle(tr("ui.png_export_title"))
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(operation.cancellation.cancel)
        progress.show()

    def _call_png_exporter(self, operation: PngArchiveOperation):
        technical_log.log_event("project.png_export.admission", operation="png_export", operation_id=operation.operation_id, status="started", phase="export", mode=operation.mode)
        return self.controller.png_archive_exporter(operation.snapshot, operation.target, mode=operation.mode,
            component_ids=operation.component_ids, cancellation=operation.cancellation,
            on_progress=lambda value: self._update_png_progress(operation, value))

    @staticmethod
    def _png_result_status(result) -> str:
        if isinstance(result, Exception):
            return "cancelled" if isinstance(result, ProjectError) and result.diagnostic.code == "BUILD_CANCELLED" else "failed"
        return "succeeded"

    def _report_png_result(self, result) -> None:
        if isinstance(result, Exception):
            if isinstance(result, ProjectError) and result.diagnostic.code == "BUILD_CANCELLED":
                self.statusBar().showMessage(tr("ui.png_cancelled"), 5000)
            else:
                self._show_error(result)
        else:
            self.statusBar().showMessage(_png_archive_status(result), 7000)

    def _update_png_progress(self, operation: PngArchiveOperation, value) -> None:
        if not self._png_operation_is_current(operation) or operation.progress is None:
            return
        operation.progress.setRange(0, max(value.total, 1))
        operation.progress.setValue(value.completed)
        key = "ui.png_progress_render" if value.phase == "render" else "ui.png_progress_pack"
        operation.progress.setLabelText(tr(key))
        QApplication.processEvents()

    def _finish_png_operation(self, operation: PngArchiveOperation, status: str) -> None:
        if self._png_operation is not operation:
            return
        technical_log.log_event("project.png_export.admission", operation="png_export", operation_id=operation.operation_id,
            status=status, phase="completion", mode=operation.mode)
        self._png_operation = None
        if operation.progress is not None:
            operation.progress.close()
        self._update_actions()
        if self.active_tab is not None:
            self._show_selection(self._selected_ids())
        if operation.close_after:
            QTimer.singleShot(0, self.close)
        else:
            self._resume_deferred_preview()

    def _png_operation_is_current(self, operation: PngArchiveOperation) -> bool:
        tab = operation.active_tab
        tab_is_current = tab is None or self._tabs.get(tab.component_id) is tab
        return (self._png_operation is operation and self.session is operation.session
                and self._preview_session_identity == operation.session_identity and tab_is_current
                and self.active_tab is tab)

    def _start_build(self, active_only: bool) -> None:
        if self.session is None or self.controller.build is None or not self._operation_allowed("build"): return
        if self._build_progress is not None:
            self._build_progress.raise_()
            self._build_progress.activateWindow()
            return
        tab = self.active_tab if active_only else None
        if active_only and tab is None:
            return
        if not self._build_component_valid(tab):
            return
        scope = (self.session.documents[tab.component_id].model.name if tab is not None
                 else tr("ui.build_scope_project"))
        dialog = BuildDialog(self._program_preferences.print_settings, self.data_mode.mode, scope, self)
        dialog.startRequested.connect(lambda: self._confirm_build_start(dialog, active_only, tab))
        dialog.cancelRequested.connect(self._cancel_build)
        dialog.settingsRequested.connect(lambda: self._edit_build_settings(dialog))
        dialog.finished.connect(lambda _result: self._build_dialog_closed(dialog))
        self._build_progress = dialog
        dialog.open()

    def _build_component_valid(self, tab) -> bool:
        if tab is None or self.session.documents[tab.component_id].model_ready:
            return True
        self._add_events(self.session.documents[tab.component_id].current_diagnostics)
        return False

    def _edit_build_settings(self, dialog: BuildDialog) -> None:
        if self._show_program_settings(dialog):
            dialog.update_settings(self._program_preferences.print_settings)

    def _build_dialog_closed(self, dialog: BuildDialog) -> None:
        if self._build_progress is dialog and self._active_build_job is None and not self._build_starting:
            self._build_progress = None

    def _confirm_build_start(self, dialog: BuildDialog, active_only: bool, tab: DocumentTab | None) -> None:
        if (self._build_progress is not dialog or self.session is None or self.controller.build is None
                or not self._operation_allowed("build") or active_only and self.active_tab is not tab):
            return
        dialog.begin()
        self._reserve_build(active_only, tab, dialog)

    def _reserve_build(self, active_only: bool, tab: DocumentTab | None, dialog: BuildDialog) -> None:
        self._build_pending_active_only = active_only
        self._build_operation_id = uuid4().hex
        self._build_session = self.session
        self._build_tab = tab
        self._build_mode = self.data_mode.mode
        self._build_after_action = dialog.after_action
        self._build_cancel_requested = False
        self._reserved_build_request = self._make_build_request(active_only, tab)
        self._save_and_launch_build(active_only, dialog)

    def _make_build_request(self, active_only: bool, tab: DocumentTab | None) -> BuildRequest:
        component_ids = (tab.component_id,) if active_only and tab is not None else None
        return BuildRequest(
            component_ids=component_ids,
            mode=self._build_mode,
            print_settings=self._program_preferences.print_settings,
            max_render_workers=self._program_preferences.png_workers,
        )

    def _save_and_launch_build(self, active_only: bool, dialog: BuildDialog) -> None:
        component_ids = self._reserved_build_request.component_ids if self._reserved_build_request else None
        if not self._save_all_for_reservation(self._build_operation_id, component_ids):
            self._cancel_pending_build()
            self._finish_build_dialog()
            self.statusBar().showMessage(tr("ui.build_cancelled"), 7000)
            return
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_deferred = tab.preview_arguments is not None
        self._preview_coordinator.discard_pending()
        self._update_actions()
        if self._preview_coordinator.active is not None:
            dialog.set_status(tr("ui.build_waiting_preview"))
            return
        self._launch_reserved_build(active_only)

    def _launch_reserved_build(self, active_only: bool) -> None:
        if self._build_pending_active_only is None or self.session is None:
            return
        if self._build_session is not self.session or active_only and self._build_tab is not self.active_tab:
            self._cancel_pending_build()
            return
        if (self._build_pending_active_only is None or self.session is None
                or self._build_session is not self.session or active_only and self._build_tab is not self.active_tab):
            self._cancel_pending_build()
            return
        if self._reserved_build_request is None:
            self._cancel_pending_build()
            return
        self._start_build_request(self._reserved_build_request)

    def _start_build_request(self, request: BuildRequest) -> None:
        self._build_starting = True
        self._build_progress_revision = -1
        try:
            job_id = self._start_build_worker(request)
        except Exception as exc:
            self._build_starting = False
            self._reject_build_start(exc)
            return
        self._active_build_job = job_id
        self._build_starting = False
        self._release_build_reservation()
        self._update_actions()

    def _start_build_worker(self, request: BuildRequest) -> str:
        self._last_build_progress = None
        return self.controller.start_build(request, on_progress=self._on_build_progress,
                                           on_finished=self._queue_build_finished)

    def _reject_build_start(self, error: Exception) -> None:
        self._cancel_pending_build()
        detail = format_error(error.diagnostic) if isinstance(error, ProjectError) else str(error)
        self.statusBar().showMessage(f"{tr('ui.build_failed')} {detail}", 15000)
        self._finish_build_dialog()

    def _release_build_reservation(self) -> None:
        self._build_pending_active_only = self._build_operation_id = None
        self._build_session = self._build_tab = self._build_mode = None
        self._reserved_build_request = None

    def _cancel_pending_build(self, *, resume_preview: bool = True) -> None:
        if self._build_pending_active_only is None:
            return
        self._build_pending_active_only = None
        self._build_operation_id = None
        self._build_session = None
        self._build_tab = None
        self._build_mode = None
        self._reserved_build_request = None
        if resume_preview:
            self._resume_deferred_preview()
        self._update_actions()

    def _resume_deferred_preview(self) -> None:
        for tab in self._tabs.values():
            if tab is not self.active_tab:
                tab.preview_deferred = False
        if self.active_tab is not None and self.active_tab.preview_deferred:
            self.active_tab.preview_deferred = False
            self._refresh_preview(self.active_tab)

    def _on_build_progress(self, progress: BuildProgress) -> None:
        if ((progress.job_id != self._active_build_job and not self._build_starting)
                or self._build_progress is None):
            return
        if progress.revision:
            if progress.revision <= self._build_progress_revision:
                return
        elif not self._accept_build_progress(progress):
            return
        self._last_build_progress = progress
        self._build_progress_revision = progress.revision
        message = self._format_build_progress(progress)
        self._build_progress.update_progress(progress.__class__(
            progress.job_id, progress.phase, progress.completed, progress.total, message,
            progress.component_names, progress.revision,
        ))

    @staticmethod
    def _format_build_progress(progress: BuildProgress) -> str:
        message = progress.message or tr(f"ui.build_phase_{progress.phase}")
        return tr("ui.build_progress_components", components=", ".join(progress.component_names), message=message) if progress.component_names else message

    def _accept_build_progress(self, progress: BuildProgress) -> bool:
        previous = self._last_build_progress
        order = {"preparing": 0, "rendering": 1, "assembling_pdf": 2, "publishing": 3}
        return previous is None or (
            order.get(progress.phase, 0) >= order.get(previous.phase, 0)
            and (progress.phase != previous.phase or progress.completed >= previous.completed)
        )

    def _on_build_finished(self, result: BuildResult) -> None:
        if result.job_id != self._active_build_job:
            return
        self._active_build_job = None
        self._last_build_result = result if result.status == "succeeded" else self._last_build_result
        self._update_actions()
        if result.status == "succeeded":
            self._complete_successful_build(result)
        else:
            self._complete_unsuccessful_build(result)
        self._refresh_preview_after_build()
        if self._close_after_build:
            self._close_after_build = False
            self.close()

    def _complete_successful_build(self, result: BuildResult) -> None:
        self.statusBar().showMessage(tr("ui.build_succeeded"), 12000)
        if result.cleanup_state == "cleanup_pending" or result.diagnostics:
            self._show_build_diagnostics(result)
        action = self._build_after_action
        target = result.pdf_path if action == "pdf" else result.output_directory if action == "folder" else None
        self._finish_build_dialog()
        if target is not None:
            QTimer.singleShot(0, lambda: self._open_build_target(target, result.job_id, auto=True))

    def _complete_unsuccessful_build(self, result: BuildResult) -> None:
        message = tr("ui.build_cancelled") if result.status == "cancelled" else tr("ui.build_failed")
        self.statusBar().showMessage(message, 12000)
        self._show_build_diagnostics(result)
        self._finish_build_dialog()

    def _refresh_preview_after_build(self) -> None:
        if self.active_tab is not None and self.active_tab.canvas._pixmap_item is None:
            self._refresh_preview(self.active_tab)
        if self.active_tab is not None and self.active_tab.preview_deferred:
            self.active_tab.preview_deferred = False
            self._refresh_preview(self.active_tab)

    def _queue_build_finished(self, result: BuildResult) -> None:
        QTimer.singleShot(0, lambda: self._deliver_build_finished(result))

    def _deliver_build_finished(self, result: BuildResult) -> None:
        if result.job_id == self._active_build_job:
            self._on_build_finished(result)

    def _cancel_build(self) -> None:
        self._build_cancel_requested = True
        if self._build_pending_active_only is not None:
            self._cancel_pending_build()
            self._finish_build_dialog()
            self.statusBar().showMessage(tr("ui.build_cancelled"), 7000)
        elif self._active_build_job:
            self.controller.cancel_build(self._active_build_job)
            if self._build_progress is not None:
                self._build_progress.cancelling(tr("ui.build_cancelling"))

    def _finish_build_dialog(self) -> None:
        dialog = self._build_progress
        if dialog is not None:
            dialog.finish()
            self._build_progress = None

    def _show_build_diagnostics(self, result: BuildResult) -> None:
        details = "; ".join(format_diagnostic(item) for item in result.diagnostics)
        if details:
            self.statusBar().showMessage(details, 15000)

    def _open_build_target(self, target: Path, job_id: str, *, auto: bool = False) -> None:
        kind = "PDF" if target.name == "print.pdf" else "FOLDER"
        code = "BUILD_RESULT_MISSING" if not target.exists() else "BUILD_RESULT_TYPE"
        try:
            valid_target = target.is_file() if kind == "PDF" else target.is_dir()
        except OSError:
            valid_target = False
        if auto and self._build_cancel_requested:
            return
        technical_log.log_event("build.open_result", operation="build", operation_id=job_id,
                                phase="opening", status="started", code=kind)
        try:
            opened = valid_target and PlatformServices.open_path(target)
        except OSError:
            opened = False
        technical_log.log_event("build.open_result", operation="build", operation_id=job_id,
                                phase="finished", status="succeeded" if opened else "failed",
                                code=kind if valid_target else code)
        if not opened:
            self.statusBar().showMessage(tr("ui.build_open_failed"), 7000)

    def _open_build_result(self, pdf: bool) -> None:
        result = self._last_build_result
        if result is None:
            return
        target = result.pdf_path if pdf else result.output_directory
        if target is not None:
            self._open_build_target(target, result.job_id)

    def _request_mode(self, tab: DocumentTab, mode: str) -> None:
        if self.session is None or not self._user_editing_allowed():
            return
        if mode == "text":
            tab.canvas.cancel_tool()
            tab.set_text(self.session.documents[tab.component_id].current_text)
            tab.set_mode("text")
            tab.text_editor.setFocus()
        elif self.session.documents[tab.component_id].model_ready and self._apply_draft(tab.component_id):
            tab.set_mode("layout")
            self._refresh_preview(tab)
        else:
            tab.set_mode("text")
        self._active_tab_changed(self.tabs.currentIndex())

    def _draft_changed(self, tab: DocumentTab, text: str) -> None:
        if self.session is None or not self._user_editing_allowed():
            return
        document = self.session.documents[tab.component_id]
        snapshot = self.controller.prepare_text(tab.component_id, text)
        document.set_draft(text, snapshot.diagnostics, snapshot)
        self._show_document_diagnostics(tab, snapshot.diagnostics)
        if not document.model_ready:
            tab.invalidate_preview()
            tab.canvas.clear_document()
        self._update_tab_title(tab.component_id)
        self._update_actions()

    def _apply_draft(self, component_id: str, *, reservation: str | None = None) -> bool:
        if self.session is None or not self._user_editing_allowed(reservation):
            return False
        document = self.session.documents[component_id]
        if document.draft_text is None:
            return True
        tab = self._tabs.get(component_id)
        after = document.draft_snapshot or self.controller.prepare_text(component_id, document.draft_text)
        if after.text == document.committed.text:
            document.clear_draft()
            return True
        return self._commit_draft(component_id, document, tab, after)

    def _commit_draft(self, component_id, document, tab, after) -> bool:
        before = document.committed
        command = ReplaceComponentCommand(
            "Применить YAML",
            document,
            before,
            after,
            lambda: self._document_changed(component_id),
        )
        if tab is not None:
            tab.undo_stack.push(command)
            tab.set_text(after.text)
            self._show_document_diagnostics(tab, after.diagnostics)
            if not document.model_ready:
                self._clear_invalid_component_tab(tab)
        else:
            document.replace_snapshot(after)
            self._document_changed(component_id)
        return True

    @staticmethod
    def _clear_invalid_component_tab(tab: DocumentTab) -> None:
        tab.set_mode("text")
        tab.invalidate_preview()
        tab.canvas.clear_document()

    def _install_element_model(self, component, selected: tuple[str, ...] = ()) -> None:
        model = element_model(component)
        self.element_tree.setModel(model)
        self.element_tree.expandAll()
        selection = self.element_tree.selectionModel()
        selection.selectionChanged.connect(self._tree_selection_changed)
        self._selection_sync = True
        for node_id in selected:
            index = self._index_for_id(node_id)
            if index.isValid():
                selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._selection_sync = False
        self._show_selection(selected)

    def _index_for_id(self, node_id: str, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        model = self.element_tree.model()
        if model is None:
            return QModelIndex()
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            if index.data(ID_ROLE) == node_id:
                return index
            nested = self._index_for_id(node_id, index)
            if nested.isValid():
                return nested
        return QModelIndex()

    def _tree_selection_changed(self, *_args) -> None:
        if self._selection_sync or self.element_tree.selectionModel() is None:
            return
        ids = tuple(index.data(ID_ROLE) for index in self.element_tree.selectionModel().selectedRows(0))
        self._set_selection(ids, source="tree")

    def _canvas_selection_changed(self, ids: object) -> None:
        if self.sender() is not getattr(self.active_tab, "canvas", None):
            return
        self._set_selection(tuple(ids), source="canvas")

    def _set_selection(self, ids: tuple[str, ...], *, source: str) -> None:
        tab = self.active_tab
        if tab is None or self.session is None or self._selection_sync:
            return
        document = self.session.documents[tab.component_id]
        if document.model is None or not document.model_ready:
            return
        component = document.model
        selected = self._normalize_selection(ids, locations(component))
        self._selection_sync = True
        tab.canvas.select_ids(selected)
        if self.element_tree.selectionModel() is not None:
            selection = self.element_tree.selectionModel()
            selection.clearSelection()
            for node_id in selected:
                item = self._index_for_id(node_id)
                if item.isValid():
                    selection.select(item, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._selection_sync = False
        self._show_selection(selected)

    @staticmethod
    def _normalize_selection(ids, index) -> tuple[str, ...]:
        valid = tuple(dict.fromkeys(node_id for node_id in ids if node_id in index))
        chosen = set(valid)
        normalized: list[str] = []
        for node_id in valid:
            parent = index[node_id].parent_id
            has_selected_ancestor = False
            while parent is not None:
                if parent in chosen:
                    has_selected_ancestor = True
                    break
                parent = index[parent].parent_id
            if not has_selected_ancestor:
                normalized.append(node_id)
        return tuple(normalized)

    def _show_selection(self, ids: tuple[str, ...]) -> None:
        tab = self.active_tab
        if tab is None or self.session is None:
            return
        if len(ids) == 1 and self._show_selected_node(tab, ids[0]):
            self._update_actions()
            return
        if len(ids) == 1:
            ids = ()
        self._show_component_or_multiple_selection(tab, len(ids))
        self._update_actions()

    def _show_selected_node(self, tab, node_id) -> bool:
        document = self.session.documents[tab.component_id]
        if not document.model_ready or document.model is None:
            return False
        found = locations(document.model).get(node_id)
        if found is None:
            return False
        self._show_single_selection(tab, node_id, found.node)
        return True

    def _show_component_or_multiple_selection(self, tab, count):
        self.element_properties.show_node(None)
        self.element_properties_group.hide()
        self.component_properties_group.setVisible(count == 0)
        self.multi_properties_group.setVisible(count > 1)
        if count == 0:
            self.properties.show_component(self.session.documents[tab.component_id].model)

    def _show_single_selection(self, tab, node_id, node) -> None:
        component = self.session.documents[tab.component_id].model
        columns = self._selected_data_columns(tab)
        matches = self._selected_condition_matches(tab, node)
        fonts, error = self._selected_font_families(node)
        self.element_properties.show_node(node, columns, component, matches, fonts)
        self.element_properties.show_image_quality(item for item in tab.image_quality if item.node_id == node.id)
        self.component_properties_group.hide()
        self.multi_properties_group.hide()
        self.element_properties_group.show()
        self.element_properties.set_edit_locked(node_id in effectively_locked_ids(component))
        message = format_diagnostic(error.diagnostic) if error else f"Выбран элемент: {node.id}"
        self.statusBar().showMessage(message, 7000 if error else 2500)

    def _selected_data_columns(self, tab):
        if self.controller.preview is None:
            return ()
        try:
            data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id)
            return data.headers if data is not None else ()
        except ProjectError:
            return ()

    def _selected_condition_matches(self, tab, node):
        if not isinstance(node, ConditionalGroupNode) or tab.resolved_component is None:
            return None
        found = locations(tab.resolved_component).get(node.id)
        return found.node.condition_matches if found and isinstance(found.node, ConditionalGroupNode) else None

    def _selected_font_families(self, node):
        if not isinstance(node, HtmlNode) or not self._operation_allowed("resource_write"):
            return (), None
        try:
            return available_font_families(self.session.snapshot.root), None
        except ProjectError as exc:
            return (), exc

    def _update_selected_image_quality(self, tab: DocumentTab) -> None:
        if tab is self.active_tab and len(tab.canvas.selected_ids) == 1:
            node_id = tab.canvas.selected_ids[0]
            self.element_properties.show_image_quality(
                item for item in tab.image_quality if item.node_id == node_id
            )

    def _edit_active_size(self, width: float, height: float) -> None:
        try:
            size = SizeMM.model_validate({"width": width, "height": height})
        except ValidationError as exc:
            self._show_error(exc)
            return
        self._edit_active(size_mm=size)

    def _pick_background_color(self) -> None:
        if self.session is None or self.active_tab is None:
            return
        token = self._selection_token()
        current = self.session.documents[self.active_tab.component_id].model.background
        color = QColorDialog.getColor(QColor(current), self, tr("ui.dialog_background_color"))
        if color.isValid() and self._selection_token_is_current(token):
            self._edit_active(background=color.name(QColor.NameFormat.HexRgb).upper())

    def _pick_node_color(self, current: str) -> None:
        ids = self._selected_ids()
        token = self._selection_token()
        node = locations(self.session.documents[self.active_tab.component_id].model).get(ids[0]).node if ids and self.session and self.active_tab else None
        field = {"shapeFill": "fill", "shapeStroke": "stroke"}.get(current, "color")
        value = getattr(node, field, "#111111") if node is not None else current
        initial = QColor(value or "#FFFFFF")
        if not initial.isValid():
            initial = QColor("#111111")
        title = tr("ui.dialog_text_color") if field == "color" else tr("ui.dialog_fill_color") if field == "fill" else tr("ui.dialog_stroke_color")
        color = QColorDialog.getColor(initial, self, title, QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid() and self._selection_token_is_current(token):
            self._edit_selected_node({field: color.name(QColor.NameFormat.HexArgb).upper() if color.alpha() < 255 else color.name(QColor.NameFormat.HexRgb).upper()})

    def _component_property_sheet_changed(self, sheet: object) -> None:
        if self.active_tab is None:
            return
        binding = DataBinding(sheet=str(sheet)) if sheet else None
        self._edit_active(data=binding)

    def _align_selected(self, edge: str) -> None:
        selected = self._selected_ids()
        self._apply_tree("Выровнять элементы", align_nodes, selected, edge, selected=selected)

    def _edit_active(self, **changes: object) -> None:
        tab = self.active_tab
        if tab is None or self.session is None or not self._visual_editing_allowed():
            return
        if "background" in changes and not QColor(str(changes["background"])).isValid():
            QMessageBox.warning(self, "Некорректное значение", "Фон должен иметь вид #RRGGBB")
            self.properties.show_component(self.session.documents[tab.component_id].model)
            return
        try:
            values = self.session.documents[tab.component_id].require_model().model_dump(mode="python")
            values.update(changes)
            updated = ComponentDefinition.model_validate(values)
        except (ValidationError, ValueError) as exc:
            self._show_error(exc)
            self.properties.show_component(self.session.documents[tab.component_id].model)
            return
        self._commit_component("Изменить свойства компонента", updated)

    def _commit_component(self, label: str, updated: ComponentDefinition, *, selected: tuple[str, ...] | None = None) -> bool:
        tab = self.active_tab
        if tab is None or self.session is None or not self._visual_editing_allowed():
            return False
        document = self.session.documents[tab.component_id]
        if updated == document.model:
            return False
        return self._push_component_command(tab, document, label, updated, selected)

    def _push_component_command(self, tab, document, label, updated, selected) -> bool:
        component_id = tab.component_id
        try:
            after = self.controller.prepare_model(component_id, updated)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        command = ReplaceComponentCommand(
            label,
            document,
            document.committed,
            after,
            lambda: self._document_changed(component_id, selected),
        )
        tab.undo_stack.push(command)
        return True

    def _document_changed(self, component_id: str, selected: tuple[str, ...] | None = None) -> None:
        if self.session is None:
            return
        document = self.session.documents[component_id]
        self._validate_changed_document(component_id)
        tab = self._tabs.get(component_id)
        if tab is not None:
            selected = self._current_tab_selection(tab, component_id, selected)
            self._refresh_preview(tab, selected=selected)
            tab.set_text(self.session.documents[component_id].current_text)
            self._show_document_diagnostics(tab, document.current_diagnostics)
            tab.layout_mode.setEnabled(document.model_ready)
        if tab is self.active_tab:
            self._refresh_active_document_properties(component_id, selected)
        self._update_tab_title(component_id)
        self._refresh_component_tree()
        self._update_actions()

    def _validate_changed_document(self, component_id):
        if self.controller.preview is None or not self.session.documents[component_id].model_ready:
            return
        snapshot = self._snapshot_for_current_documents()
        if snapshot is None:
            return
        diagnostics = self.controller.preview.validate_all(snapshot, manual=False, component_ids=(component_id,))
        if diagnostics:
            self._add_events(diagnostics)

    def _current_tab_selection(self, tab, component_id, selected):
        document = self.session.documents[component_id]
        current_ids = locations(document.model) if document.model_ready and document.model is not None else {}
        values = tab.canvas.selected_ids if selected is None else selected
        return tuple(node_id for node_id in values if node_id in current_ids)

    def _refresh_active_document_properties(self, component_id, selected):
        document = self.session.documents[component_id]
        if not document.model_ready or document.model is None:
            self.element_tree.setModel(None)
            self.properties.show_component(None)
            self.element_properties.show_node(None)
            return
        self._install_element_model(document.model, selected or ())
        self.properties.show_component(document.model)
        self._populate_data_panel()

    def _after_undo_redo(self) -> None:
        self._update_actions()

    def _selected_ids(self) -> tuple[str, ...]:
        if self.active_tab is None or self.session is None or not self.session.documents[self.active_tab.component_id].model_ready:
            return ()
        selected = self.active_tab.canvas.selected_ids
        if selected:
            return selected
        model = self.element_tree.selectionModel()
        if model is None:
            return ()
        return tuple(
            str(index.data(ID_ROLE))
            for index in model.selectedRows()
            if index.data(ID_ROLE) is not None
        )

    def _selection_token(self) -> tuple | None:
        if self.session is None or self.active_tab is None:
            return None
        document = self.session.documents.get(self.active_tab.component_id)
        return None if document is None else (
            id(self.session), self.active_tab.identity, document.component_id,
            document.revision, self._selected_ids(),
        )

    def _selection_token_is_current(self, token: tuple | None) -> bool:
        return token is not None and self._selection_token() == token and self._user_editing_allowed()

    def _apply_tree(self, label: str, operation, *args, selected: tuple[str, ...] | None = None, **kwargs) -> bool:
        tab = self.active_tab
        if tab is None or self.session is None or not self._visual_editing_allowed():
            return False
        try:
            updated = operation(self.session.documents[tab.component_id].model, *args, **kwargs)
        except (TreeOperationError, ValidationError, ValueError) as exc:
            QMessageBox.warning(self, tr("ui.operation_unavailable"), format_error(exc))
            return False
        if callable(selected):
            selected = selected()
        return self._commit_component(label, updated, selected=selected)

    def _move_selected(self, ids: object, dx_mm: float, dy_mm: float) -> None:
        selected = tuple(ids)
        self._apply_tree("Переместить элементы", move_nodes, selected, dx_mm, dy_mm, selected=selected)

    def _canvas_context_is_current(self, tab, context) -> bool:
        if tab is not self.active_tab or self.session is None or context is None:
            return False
        document = self.session.documents.get(tab.component_id)
        return document is not None and context == (id(self.session), tab.identity, tab.component_id, document.revision)

    def _move_canvas_nodes(self, tab, context, ids, dx, dy) -> None:
        accepted = False
        if self._canvas_context_is_current(tab, context):
            selected = tuple(ids)
            accepted = self._apply_tree("Переместить элементы", move_nodes, selected, dx, dy, selected=selected)
        tab.canvas.report_gesture_outcome(accepted)

    def _resize_canvas_node(self, tab, context, node_id, width, height, anchor_x, anchor_y) -> None:
        accepted = False
        if self._canvas_context_is_current(tab, context):
            accepted = self._resize_selected(node_id, width, height, anchor_x, anchor_y)
        tab.canvas.report_gesture_outcome(accepted)

    def _draw_canvas_element(self, tab, context, kind, x1, y1, x2, y2) -> None:
        accepted = False
        if self._canvas_context_is_current(tab, context):
            accepted = self._draw_element(kind, x1, y1, x2, y2)
        tab.canvas.report_gesture_outcome(accepted)

    @staticmethod
    def _default_name_key(node_type: str) -> str | None:
        return {
            "image": "ui.image",
            "html": "ui.html_text",
            "rectangle": "ui.rectangle",
            "ellipse": "ui.ellipse",
            "line": "ui.line",
            "group": "ui.group_name",
            "conditional_group": "ui.conditional_group",
        }.get(node_type)

    def _next_default_name(self, node_type: str, occupied: set[str] | None = None) -> str:
        key = self._default_name_key(node_type)
        base = tr(key) if key else tr("ui.element")
        if occupied is None:
            occupied = {item.node.name for item in locations(self.session.documents[self.active_tab.component_id].model).values()} if self.session and self.active_tab else set()
        used = set()
        for name in occupied:
            match = re.fullmatch(re.escape(base) + r" ([1-9]\d*)", name)
            if name == base:
                used.add(1)
            elif match:
                used.add(int(match.group(1)))
        number = 1
        while number in used:
            number += 1
        name = f"{base} {number}"
        occupied.add(name)
        return name

    @classmethod
    def _is_default_name(cls, node: Node) -> bool:
        key = cls._default_name_key(node.type)
        if key is None:
            return False
        bases = {value[key] for value in catalogs().values()}
        if node.type == "group":
            bases.update(value["ui.component_type_group"] for value in catalogs().values())
        return any(node.name == base or re.fullmatch(re.escape(base) + r" [1-9]\d*", node.name) for base in bases)

    def duplicate_selected(self) -> bool:
        if self.session is None or self.active_tab is None:
            return False
        component_id = self.active_tab.component_id
        component = self.session.documents[component_id].model
        if component is None:
            return
        selected = self._selected_ids()
        if not selected:
            return False
        used_ids, used_names = self._existing_node_identifiers(component)
        created_ids: dict[str, str] = {}
        new_id, new_name = self._duplicate_callbacks(component_id, used_ids, used_names, created_ids)
        return self._apply_tree(
            tr("ui.duplicate"), duplicate_nodes, selected, new_id, new_name,
            selected=lambda: tuple(created_ids[node_id] for node_id in selected),
        )

    def _duplicate_callbacks(self, component_id, used_ids, used_names, created_ids):
        def new_id(node: Node, name: str) -> str:
            candidate = self.controller.node_id(component_id, name, node.type)
            while candidate in used_ids:
                candidate = self.controller.node_id(component_id, name, node.type)
            used_ids.add(candidate)
            created_ids[node.id] = candidate
            return candidate

        def new_name(node: Node) -> str | None:
            return self._next_default_name(node.type, used_names) if self._is_default_name(node) else None
        return new_id, new_name

    @staticmethod
    def _existing_node_identifiers(component):
        indexed = locations(component)
        return set(indexed), {item.node.name for item in indexed.values()}

    def _canvas_context_menu(self, _node_id: str, point) -> None:
        if self.active_tab is not None:
            self._show_element_context_menu(self.active_tab.canvas.mapToGlobal(point))

    def _element_tree_context_menu(self, _node_id: str, point) -> None:
        self._show_element_context_menu(self.element_tree.viewport().mapToGlobal(point))

    def _show_element_context_menu(self, global_point) -> None:
        ids = self._selected_ids()
        if not ids or self.active_tab is None or self.session is None:
            return
        index = locations(self.session.documents[self.active_tab.component_id].model)
        selected = [index[node_id].node for node_id in ids]
        self._open_element_context_menu(global_point, ids, selected, editing_actions(self._editing_context()))

    def _open_element_context_menu(self, point, ids, selected, actions) -> None:
        menu = QMenu(self)
        duplicate = menu.addAction(tr("ui.duplicate"))
        duplicate.setEnabled(actions.layout and actions.duplicate)
        duplicate.triggered.connect(lambda: self.duplicate_selected())
        delete = menu.addAction(tr("ui.context_delete"))
        delete.setEnabled(actions.layout and actions.delete)
        delete.triggered.connect(lambda: self.delete_selected())
        if len(ids) > 1:
            group = menu.addAction(tr("ui.group_create"))
            group.setEnabled(actions.layout and actions.group)
            group.triggered.connect(lambda: self.group_selected())
        elif isinstance(selected[0], GroupNode):
            ungroup = menu.addAction(tr("ui.ungroup"))
            ungroup.setEnabled(actions.layout and actions.ungroup)
            ungroup.triggered.connect(lambda: self.ungroup_selected())
        menu.exec(point)

    def _resize_selected(self, node_id: str, width_mm: float, height_mm: float, anchor_x: float = 0.0, anchor_y: float = 0.0) -> bool:
        return self._apply_tree("Изменить размер элемента", resize_node, node_id, width_mm, height_mm, (anchor_x, anchor_y), selected=(node_id,))

    def _rotate_selected(self, degrees: float) -> None:
        ids = self._selected_ids()
        if len(ids) == 1:
            self._apply_tree("Повернуть элемент", rotate_node, ids[0], degrees, selected=ids)

    def _resize_line_selected(self, node_id: str, dx_mm: float, dy_mm: float) -> None:
        self._apply_tree("Изменить линию", resize_line_endpoint, node_id, dx_mm, dy_mm, selected=(node_id,))

    def _edit_selected_node(self, changes: dict) -> None:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None:
            return
        if any(value is not None and not QColor(str(value)).isValid() for key, value in changes.items() if key in ("color", "fill", "stroke")):
            QMessageBox.warning(self, "Некорректное значение", "Выберите допустимый цвет из палитры")
            self._show_selection(ids)
            return
        self._apply_tree("Изменить элемент", self._apply_node_changes, ids[0], changes, selected=ids)

    @staticmethod
    def _apply_node_changes(component: ComponentDefinition, node_id: str, changes: dict) -> ComponentDefinition:
        values = dict(changes)
        result = component
        node = locations(component)[node_id].node
        if isinstance(node, GroupNode) and "width_mm" in values and "height_mm" in values:
            result = resize_node(result, node_id, values.pop("width_mm"), values.pop("height_mm"))
        if values:
            result = update_node(result, node_id, **values)
        return result

    def delete_selected(self) -> bool:
        ids = self._selected_ids()
        if not ids or not editing_actions(self._editing_context()).delete:
            return False
        if self.active_tab is None or self.session is None:
            return False
        token = self._selection_token()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr("ui.delete_confirm_title"))
        box.setText(tr("ui.delete_confirm_message", count=len(ids)))
        delete_button = box.addButton(tr("ui.delete_confirm_yes"), QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton(tr("ui.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        if box.clickedButton() is not delete_button or not self._selection_token_is_current(token):
            return False
        return self._apply_tree(tr("action.delete"), remove_nodes, ids, selected=())

    def reorder_selected(self, direction: int) -> bool:
        ids = self._selected_ids()
        if not ids:
            return False
        return self._apply_tree("Изменить порядок слоёв", reorder_nodes, ids, direction, selected=ids)

    def _toggle_element_lock(self, node_id: str) -> None:
        if self.active_tab is None or self.session is None:
            return
        node = locations(self.session.documents[self.active_tab.component_id].model).get(node_id)
        if node is not None:
            self._apply_tree("Изменить блокировку элемента", update_node, node_id, locked=not node.node.locked, selected=(node_id,))

    def _layer_drop(self, model, ids: object, target_id: str | None, position) -> None:
        tab = self.active_tab
        if tab is None or model is not self.element_tree.model() or self.session is None:
            return
        selected = tuple(ids)
        destination = self._layer_drop_destination(tab, target_id, position)
        if destination is None:
            return
        parent_id, sibling_id, before = destination
        self._apply_tree(
            "Перетащить слой",
            drop_nodes,
            selected,
            parent_id,
            sibling_id,
            before=before,
            selected=selected,
        )

    def _layer_drop_destination(self, tab, target_id, position):
        if target_id is None:
            return None, None, False
        index = locations(self.session.documents[tab.component_id].model).get(target_id)
        if index is None:
            return None
        if position == LayerTreeView.DropIndicatorPosition.OnItem and isinstance(index.node, GroupNode):
            return target_id, None, False
        return index.parent_id, target_id, position == LayerTreeView.DropIndicatorPosition.BelowItem

    def group_selected(self, group_id: str | None = None, name: str | None = None) -> bool:
        ids = self._selected_ids()
        if len(ids) < 2 or self.session is None or self.active_tab is None:
            return False
        if not editing_actions(self._editing_context()).group:
            return False
        token = self._selection_token()
        component = self.session.documents[self.active_tab.component_id].model
        if name is None:
            name, accepted = QInputDialog.getText(self, tr("ui.group_create"), tr("ui.name"), text=self._next_default_name("group"))
            if not accepted or not self._selection_token_is_current(token):
                return False
        if not name.strip():
            return False
        group_id = group_id or self.controller.node_id(self.active_tab.component_id, name, "group")
        return self._apply_tree(tr("ui.group_create"), group_nodes, ids, group_id, name, selected=(group_id,))

    def ungroup_selected(self) -> bool:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None:
            return False
        found = locations(self.session.documents[self.active_tab.component_id].model).get(ids[0])
        if found is None or not isinstance(found.node, GroupNode):
            return False
        children = tuple(child.id for child in found.node.children)
        return self._apply_tree(tr("ui.ungroup"), ungroup_node, ids[0], selected=children)

    def reparent_selected(self, parent_id: str | None) -> bool:
        ids = self._selected_ids()
        if not ids or self.session is None or self.active_tab is None:
            return False
        index = locations(self.session.documents[self.active_tab.component_id].model)
        if all(index[node_id].parent_id == parent_id for node_id in ids):
            destination = "корне" if parent_id is None else f"группе {parent_id!r}"
            self.statusBar().showMessage(f"Выбранные элементы уже находятся в {destination}", 3500)
            return False
        return self._apply_tree("Перенести в группу", reparent_nodes, ids, parent_id, selected=ids)

    def _begin_tool(self, tool: str) -> None:
        tab = self.active_tab
        if tab is None:
            return
        current = tab.canvas.active_tool
        tab.canvas.set_tool(None if current == tool else tool)
        self._sync_tool_buttons()
        if tab.canvas.active_tool:
            prompt = "Протяните по листу для рисования; Esc отменяет" if tool in ("rectangle", "ellipse", "line") else "Щёлкните внутри листа; Esc отменяет"
            self.statusBar().showMessage(prompt)

    def _tool_cancelled(self) -> None:
        self._sync_tool_buttons()
        self.statusBar().showMessage("Размещение отменено", 2000)

    def _sync_tool_buttons(self) -> None:
        active = self.active_tab.canvas.active_tool if self.active_tab is not None else None
        for name, button in self.drawing_tools.items():
            button.setChecked(active == name if name != "select" else active is None)

    def _import_image(self, source: Path, folder: str) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        self._require_resource_write(self.session.snapshot.root)
        return self.controller.service.import_image(self.session.snapshot.root, source, folder)

    def _cached_columns(self) -> tuple[str, ...]:
        if self.session is None or self.controller.preview is None or self.active_tab is None:
            return ()
        try:
            data = self.controller.preview.cached_data(self.session.snapshot, self.active_tab.component_id)
        except ProjectError:
            return ()
        return data.headers if data is not None else ()

    def _initial_excel_image_size(self, column: str) -> tuple[float | None, float | None]:
        """Capture the selected preview row's image size once when creating a node."""
        tab = self.active_tab
        if self.session is None or tab is None or self.controller.preview is None or tab.preview_row_number is None:
            self._show_error(ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "", message_key="error.image_excel_row_required")))
            return None, None
        try:
            data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id)
            row = next(row for row in data.rows if row.row_number == tab.preview_row_number)
            cell = next(value for name, value in row.values.items() if name.strip().casefold() == column.strip().casefold())
            filename = str(cell.value).strip()
            if not filename or Path(filename).name != filename:
                raise ValueError(tr("error.image_excel_filename"))
            image = QImage(str(self.session.snapshot.root / "assets" / "images" / filename))
            if image.isNull():
                raise ValueError(tr("error.image_source_invalid"))
            return image.width() * 25.4 / 96, image.height() * 25.4 / 96
        except (AttributeError, ProjectError, StopIteration, ValueError) as exc:
            self._show_error(exc if isinstance(exc, ProjectError) else ProjectError(Diagnostic("IMAGE_SOURCE_INVALID", str(exc), message_key="error.image_source_invalid")))
            return None, None

    def _place_element(self, tool: str, x_mm: float, y_mm: float) -> None:
        if self.session is None or not self._visual_editing_allowed() or not self._tool_write_allowed(tool):
            return
        accepted = True
        if tool == "conditional_group":
            accepted = self._place_conditional_group(x_mm, y_mm)
        elif tool == "image":
            accepted = self._place_image(x_mm, y_mm)
        elif tool == "html":
            accepted = self._place_html(x_mm, y_mm)
        if self.active_tab is not None:
            self.active_tab.canvas.cancel_tool()
        if not accepted:
            self.statusBar().showMessage("Операция отменена", 2000)

    def _place_conditional_group(self, x_mm, y_mm) -> bool:
        columns = self._cached_columns()
        if not columns:
            self._show_error(ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "Для условной группы нужен столбец Excel с данными или результатами")))
            return False
        name, accepted = QInputDialog.getText(self, tr("ui.conditional_group"), tr("ui.name"), text=self._next_default_name("conditional_group"))
        if accepted and name.strip():
            component_id = self.active_tab.component_id
            node_id = self.controller.node_id(component_id, name, "condition")
            node = ConditionalGroupNode(id=node_id, type="conditional_group", name=name, x_mm=x_mm, y_mm=y_mm, children=(), condition_column=columns[0], condition_value="")
            self._apply_tree(tr("ui.add_conditional_group_action"), add_node, node, selected=(node_id,))
        return accepted

    def _place_image(self, x_mm, y_mm) -> bool:
        dialog = ImageAddDialog(self.session.snapshot.root, self._import_image, self, columns=self._cached_columns(), images=self.session.snapshot.model.images)
        dialog.name.setText(self._next_default_name("image"))
        return dialog.exec() == dialog.DialogCode.Accepted and self._add_image_dialog(dialog, x_mm, y_mm)

    def _add_image_dialog(self, dialog, x_mm, y_mm) -> bool:
        source = dialog.source.text().strip()
        if dialog.content_mode.currentData() == "column":
            width, height = self._initial_excel_image_size(dialog.content_column.currentText())
            if width is None:
                return False
        else:
            image = QImage(str(self.session.snapshot.root / source))
            if image.isNull():
                self._show_error(ProjectError(Diagnostic("IMAGE_SOURCE_INVALID", "", message_key="error.image_source_invalid")))
                return False
            width, height = image.width() * 25.4 / 96, image.height() * 25.4 / 96
        self.add_image(source, x_mm, y_mm, name=dialog.name.text().strip(), fit=dialog.fit.currentText(),
                       source_mode=dialog.content_mode.currentData(), source_column=dialog.content_column.currentText() or None,
                       width_mm=width, height_mm=height)
        return True

    def _place_html(self, x_mm, y_mm) -> bool:
        try:
            dialog = HtmlAddDialog(self.session.snapshot.root, self._import_image, self, columns=self._cached_columns(), images=self.session.snapshot.model.images)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        dialog.name.setText(self._next_default_name("html"))
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        return self.add_html(dialog.html.toPlainText(), x_mm, y_mm, name=dialog.name.text().strip(), font_family=dialog.font.currentText(),
                             font_size_pt=dialog.font_size.value(), color=dialog.color.text(), content_mode=dialog.content_mode.currentData(),
                             content_column=dialog.content_column.currentText() or None)

    def _tool_write_allowed(self, tool: str) -> bool:
        return tool != "html" or self._operation_allowed("resource_write")

    def _draw_element(self, tool: str, x1: float, y1: float, x2: float, y2: float) -> bool:
        if self.session is None or self.active_tab is None:
            return False
        node = self._drawn_node(tool, x1, y1, x2, y2)
        if node is None:
            return False
        node_id = node.id
        return self._apply_tree(tr("ui.draw_element"), add_node, node, selected=(node_id,))

    def _drawn_node(self, tool, x1, y1, x2, y2):
        component_id = self.active_tab.component_id
        if tool == "line":
            if abs(x2 - x1) < 0.01 and abs(y2 - y1) < 0.01:
                return None
            name = self._next_default_name(tool)
            return LineNode(id=self.controller.node_id(component_id, name, tool), type=tool, name=name,
                            x_mm=x1, y_mm=y1, dx_mm=x2-x1, dy_mm=y2-y1)
        if tool not in ("rectangle", "ellipse"):
            return None
        x, y, width, height = min(x1, x2), min(y1, y2), abs(x2-x1), abs(y2-y1)
        if min(width, height) < 0.01:
            return None
        name = self._next_default_name(tool)
        return ShapeNode(id=self.controller.node_id(component_id, name, tool), type=tool, name=name,
                         x_mm=x, y_mm=y, width_mm=width, height_mm=height)

    def add_image(self, source: str, x_mm: float, y_mm: float, *, name: str | None = None, fit: str = "contain", source_mode: str = "manual", source_column: str | None = None, width_mm: float | None = None, height_mm: float | None = None, node_id: str | None = None) -> bool:
        if self.session is None or self.active_tab is None or not self._visual_editing_allowed():
            return False
        component = self.session.documents[self.active_tab.component_id].model
        name = name or self._next_default_name("image")
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "image")
        if width_mm is None or height_mm is None:
            width_mm = max(0.001, min(30.0, component.size_mm.width - x_mm))
            height_mm = max(0.001, min(30.0, component.size_mm.height - y_mm))
        node = self._image_node(node_id, name, source, x_mm, y_mm, width_mm, height_mm, source_mode, source_column, fit)
        if node is None:
            return False
        return self._apply_tree(tr("ui.image_add_title"), add_node, node, selected=(node_id,))

    def _image_node(self, node_id, name, source, x_mm, y_mm, width, height, source_mode, source_column, fit):
        try:
            return ImageNode(id=node_id, name=name, type="image", x_mm=x_mm, y_mm=y_mm, width_mm=width, height_mm=height,
                             source=source, source_mode=source_mode, source_column=source_column, fit=fit)
        except ValidationError as exc:
            self._show_error(exc)
            return None

    def add_html(self, html: str, x_mm: float, y_mm: float, *, name: str | None = None, font_family: str = "Arial", font_size_pt: float = 10.0, color: str = "#111111", content_mode: str = "manual", content_column: str | None = None, node_id: str | None = None) -> bool:
        if self.session is None or self.active_tab is None or not self._visual_editing_allowed():
            return False
        component = self.session.documents[self.active_tab.component_id].model
        name = name or self._next_default_name("html")
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "html")
        width = max(0.001, min(40.0, component.size_mm.width - x_mm))
        height = max(0.001, min(15.0, component.size_mm.height - y_mm))
        node = self._html_node(node_id, name, html, x_mm, y_mm, width, height, font_family, font_size_pt, color, content_mode, content_column)
        if node is None:
            return False
        return self._apply_tree(tr("ui.html_text"), add_node, node, selected=(node_id,))

    def _html_node(self, node_id, name, html, x_mm, y_mm, width, height, font_family, font_size, color, content_mode, content_column):
        try:
            return HtmlNode(id=node_id, name=name, type="html", x_mm=x_mm, y_mm=y_mm, width_mm=width, height_mm=height,
                            font_family=font_family, font_size_pt=font_size, color=color, html=html,
                            content_mode=content_mode, content_column=content_column)
        except ValidationError as exc:
            self._show_error(exc)
            return None

    def insert_html_image(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None or not self._user_editing_allowed():
            return
        found = locations(self.session.documents[self.active_tab.component_id].model)[ids[0]].node
        if not isinstance(found, HtmlNode):
            return
        MainWindow._insert_html_image_dialog(self)

    def _insert_html_image_dialog(self):
        picker = ResourcePickerDialog(self.session.snapshot.root, self._import_image, self, images=self.session.snapshot.model.images)
        if picker.exec() != picker.DialogCode.Accepted:
            return
        dimensions = MainWindow._html_image_dimensions(self)
        if dimensions is None:
            return
        width, height, align = dimensions
        self.element_properties.insert_html_image(picker.selected_path, width, height, align)
        self._edit_selected_node({"html": self.element_properties.html_edit.toPlainText()})

    def _html_image_dimensions(self):
        width, accepted = QInputDialog.getInt(self, tr("ui.image_size"), tr("ui.width_px"), 16, 1, 4096)
        if not accepted:
            return None
        height, accepted = QInputDialog.getInt(self, tr("ui.image_size"), tr("ui.height_px"), 16, 1, 4096)
        if not accepted:
            return None
        align, accepted = QInputDialog.getItem(self, tr("ui.align"), tr("ui.position"), ["", "middle", "top", "bottom"], 0, False)
        return (width, height, align) if accepted else None

    def select_element_image(self) -> None:
        if self.session is None or self.active_tab is None or not self._user_editing_allowed():
            return
        picker = ResourcePickerDialog(
            self.session.snapshot.root, self._import_image, self,
            images=self.session.snapshot.model.images,
        )
        if picker.exec() == picker.DialogCode.Accepted:
            self.element_properties.source_edit.setText(picker.selected_path)
            self._edit_selected_node({"source": picker.selected_path})

    def save_active(self) -> bool:
        tab = self.active_tab
        if tab is None:
            return True
        return self._save_component(tab.component_id)

    def _save_component(self, component_id: str) -> bool:
        if not self._user_editing_allowed():
            return False
        if self.session is None or not self.session.documents[component_id].dirty:
            return True
        if not self._apply_draft(component_id):
            return False
        try:
            self.controller.save_document(component_id)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        self._update_tab_title(component_id)
        self._refresh_component_tree()
        saved = self.session.documents[component_id]
        message = tr("ui.saved_with_errors") if saved.current_diagnostics else tr("ui.saved")
        self.statusBar().showMessage(message, 7000 if saved.current_diagnostics else 3000)
        self._update_actions()
        return True

    def save_all(self, *, _reservation: str | None = None) -> bool:
        reservation = _reservation or self._save_reservation_id
        if not self._user_editing_allowed(reservation):
            return False
        if self.session is None:
            return True
        return self._save_all_documents(reservation)

    def _save_all_for_reservation(self, reservation: str | None, component_ids: tuple[str, ...] | None = None) -> bool:
        self._save_reservation_id = reservation
        try:
            if component_ids is None:
                return self.save_all()
            return self._save_all_documents(reservation, component_ids)
        finally:
            self._save_reservation_id = None

    def _save_all_documents(self, reservation: str | None = None, component_ids: tuple[str, ...] | None = None) -> bool:
        if self.session is None:
            return True
        selected = component_ids if component_ids is not None else tuple(self.session.documents)
        if not self._apply_selected_drafts(selected, reservation):
            return False
        try:
            if component_ids is None:
                self.controller.save_all()
            else:
                self.controller.save_components(component_ids)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        self._finish_save_all(selected)
        return True

    def _apply_selected_drafts(self, component_ids, reservation) -> bool:
        for component_id in component_ids:
            if self._apply_draft(component_id, reservation=reservation):
                continue
            tab = self._tabs.get(component_id)
            if tab is not None:
                self.tabs.setCurrentWidget(tab)
                tab.set_mode("text")
            return False
        return True

    def _finish_save_all(self, selected) -> None:
        for component_id in self._tabs:
            self._update_tab_title(component_id)
        self._refresh_component_tree()
        invalid_count = sum(bool(self.session.documents[item].current_diagnostics) for item in selected)
        message = tr("ui.saved_all_with_errors", count=invalid_count) if invalid_count else tr("ui.saved_all")
        self.statusBar().showMessage(message, 7000 if invalid_count else 3000)
        self._update_actions()

    def close_tab(self, index: int) -> bool:
        if not self._operation_allowed("close_tab"):
            return False
        if index < 0:
            return True
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab):
            return True
        active = self._preview_coordinator.active
        if active is not None and active.tab is tab:
            return False
        if not self._confirm_document(tab.component_id) or not self._operation_allowed("close_tab"):
            return False
        if self.tabs.widget(index) is not tab:
            return False
        self._remove_tab(index, tab)
        return True

    def _remove_tab(self, index: int, tab: DocumentTab) -> None:
        tab.preview_timer.stop()
        tab.preview_generation += 1
        self._preview_coordinator.discard_pending(tab.identity)
        self._tabs.pop(tab.component_id, None)
        for entry_id, retained in self._history_tabs.items():
            if retained is tab:
                self._history_tabs[entry_id] = None
        self.undo_group.removeStack(tab.undo_stack)
        self.tabs.removeTab(index)
        tab.deleteLater()

    def _confirm_document(self, component_id: str) -> bool:
        if self.session is None or not self.session.documents[component_id].dirty:
            return True
        name = self.session.documents[component_id].display_name
        choice = self._ask_unsaved_choice(tr("ui.unsaved_component_message", name=name))
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self._save_component(component_id)
        self.controller.revert_document(component_id)
        return True

    def _confirm_all_dirty(self) -> bool:
        if self.session is None or not self.session.dirty:
            return True
        choice = self._ask_unsaved_choice(tr("ui.unsaved_project_message"))
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self.save_all()
        for component_id in tuple(self.session.documents):
            self.controller.revert_document(component_id)
        return True

    def _ask_unsaved_choice(self, message: str) -> QMessageBox.StandardButton:
        owner = self._build_progress if self._build_pending_active_only is not None else self
        box = QMessageBox(owner)
        box.setWindowTitle(tr("ui.unsaved_title"))
        box.setText(message)
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.button(QMessageBox.StandardButton.Save).setText(tr("ui.save"))
        box.button(QMessageBox.StandardButton.Discard).setText(tr("ui.discard"))
        box.button(QMessageBox.StandardButton.Cancel).setText(tr("ui.cancel"))
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        return QMessageBox.StandardButton(box.exec())

    def _clear_tabs(self, *, discard: bool = False, reservation: str | None = None) -> None:
        if not self._operation_allowed("project_switch") and reservation != self._version_busy_id:
            return
        self._clear_component_history()
        self._project_history_focus = False
        if discard and self.session is not None:
            for component_id in tuple(self.session.documents):
                self.controller.revert_document(component_id)
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_generation += 1
            self.undo_group.removeStack(tab.undo_stack)
            tab.deleteLater()
        self.tabs.clear()
        self._tabs.clear()
        self._preview_coordinator.discard_pending()

    def _refresh_component_tree(self) -> None:
        if self.session is None:
            self.component_tree.setModel(None)
            return
        model = component_model(self.session)
        self.component_tree.setModel(model)
        self._sync_component_tree_selection()
        self.component_tree.selectionModel().currentChanged.connect(self._component_selection_changed)
        self.component_tree.resizeColumnToContents(0)

    def _sync_component_tree_selection(self) -> None:
        selection = self.component_tree.selectionModel()
        model = self.component_tree.model()
        if selection is None or model is None:
            return
        selected_id = self.active_tab.component_id if self.active_tab is not None else None
        index = next((
            model.index(row, 0)
            for row in range(model.rowCount())
            if model.index(row, 0).data(ID_ROLE) == selected_id
        ), QModelIndex())
        with QSignalBlocker(selection):
            if index.isValid():
                selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
                self.component_tree.scrollTo(index)
            else:
                selection.clearSelection()
                selection.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.Clear)

    def _update_tab_title(self, component_id: str) -> None:
        if self.session is None:
            return
        tab = self._tabs.get(component_id)
        if tab is None:
            return
        document = self.session.documents[component_id]
        title = document.display_name + (" *" if document.dirty else "")
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, title)
            self.tabs.setTabToolTip(index, str(document.source.path))

    def _zoom_active(self, factor: float) -> None:
        if self.active_tab is not None:
            self.active_tab.canvas.zoom_by(factor)

    def _zoom_changed(self, percent: int) -> None:
        if self.sender() is self.active_tab:
            self.status_zoom.setText(tr("ui.zoom_status", percent=percent))

    def _editing_context(self) -> EditingContext:
        tab = self.active_tab
        selected = self._selected_ids()
        model = self.session.documents[tab.component_id].model if tab is not None and self.session else None
        index = locations(model) if model is not None else {}
        found = [index.get(node_id) for node_id in selected]
        valid = bool(selected) and all(item is not None for item in found)
        locks = effectively_locked_ids(model) if model is not None else frozenset()
        nodes = [item.node for item in found if item is not None]
        return EditingContext(
            tab is not None, bool(tab and tab.mode == "text"),
            self._build_pending_active_only is not None or self._png_operation is not None or self.controller.is_build_preparing(self._active_build_job),
            self._version_busy_id is not None, len(selected) if valid else 0,
            valid and all(node_id not in locks for node_id in selected),
            valid and all(not contains_locked_node(node) for node in nodes),
            valid and len({item.parent_id for item in found if item is not None}) == 1,
            len(nodes) == 1 and isinstance(nodes[0], GroupNode),
        )

    def _user_editing_allowed(self, reservation: str | None = None) -> bool:
        if reservation is not None and self._owns_operation_reservation(reservation):
            return True
        context = self._editing_context()
        return not context.reserved and not context.version_busy

    def _owns_operation_reservation(self, reservation: str) -> bool:
        png = self._png_operation
        return bool(
            png and png.operation_id == reservation and png.phase == "saving"
            or self._build_operation_id == reservation and self._build_pending_active_only is not None
        )

    def _operation_allowed(self, operation: str) -> bool:
        context = OperationContext(
            self._preview_coordinator.active is not None,
            self._build_pending_active_only is not None,
            self._active_build_job is not None,
            self._png_operation is not None,
            self._version_busy_id is not None,
        )
        return operation_allowed(context, operation)

    def _visual_editing_allowed(self) -> bool:
        return bool(self.active_tab and self.session and self.session.documents[self.active_tab.component_id].model_ready
                    and editing_actions(self._editing_context()).layout)

    def _update_actions(self) -> None:
        self._update_project_visibility()
        context = self._editing_context()
        decisions = editing_actions(context)
        build = build_actions(BuildContext(
            self.session is not None, context.has_tab, self._active_build_job is not None,
            context.version_busy or self._png_operation is not None, context.reserved,
        ))
        self.tabs.tabBar().setEnabled(self._png_operation is None)
        self.data_mode.setEnabled(self._png_operation is None and self._version_busy_id is None)
        self.undo_action.setText(tr("action.undo"))
        self.redo_action.setText(tr("action.redo"))
        self.undo_action.setEnabled(self._history_target(redo=False) is not HistoryTarget.NONE and self._user_editing_allowed())
        self.redo_action.setEnabled(self._history_target(redo=True) is not HistoryTarget.NONE and self._user_editing_allowed())
        self._apply_project_actions(context, build)
        self._update_build_toolbar()
        self._apply_editing_actions(decisions)

    def _apply_project_actions(self, context: EditingContext, build) -> None:
        has_project = self.session is not None
        self.add_component_action.setEnabled(has_project and build.idle)
        self.project_settings_action.setEnabled(has_project and build.idle)
        self.image_manager_action.setEnabled(has_project and build.idle)
        self.delete_component_action.setEnabled(has_project and context.has_tab and len(self.session.documents) > 1 and build.idle)
        self.build_all_action.setEnabled(build.build_all)
        self.build_active_action.setEnabled(build.build_active)
        self.validate_all_action.setEnabled(build.validate_all)
        self.validate_active_action.setEnabled(build.validate_active)
        self.data_validate_all.setEnabled(build.validate_all)
        self.data_validate.setEnabled(build.validate_active)
        self.cancel_build_action.setEnabled(self._active_build_job is not None)
        self._update_file_actions(has_project, context.has_tab, build.idle, context, not context.reserved and not context.version_busy)

    def _apply_editing_actions(self, actions) -> None:
        selected = self._selected_ids()
        self.delete_widget_action.setVisible(bool(actions.layout and selected))
        for tab in self._tabs.values():
            self._update_tab_editing(tab, actions)
        self.element_tree.setEnabled(actions.layout)
        self.properties.setEnabled(actions.layout)
        self.element_properties.setEnabled(actions.layout and bool(selected))
        self.delete_action.setEnabled(actions.layout and actions.delete)
        for button in (self.layer_up, self.layer_down):
            button.setEnabled(actions.layout and actions.layer_order)
        for button in self.align_buttons.values():
            button.setEnabled(actions.layout and actions.align)
        self._apply_group_actions(actions)
        self._sync_tool_buttons()

    def _update_tab_editing(self, tab, actions) -> None:
        document = self.session.documents.get(tab.component_id)
        if document is None:
            tab.text_editor.setReadOnly(True)
            tab.layout_mode.setEnabled(False)
            tab.text_mode.setEnabled(False)
            return
        tab.text_editor.setReadOnly(not actions.text if tab is self.active_tab else not self._user_editing_allowed())
        tab.layout_mode.setEnabled(self._user_editing_allowed() and document.model_ready)
        tab.text_mode.setEnabled(self._user_editing_allowed())

    def _apply_group_actions(self, actions) -> None:
        self.group_action.setEnabled(actions.layout and actions.group)
        self.group_button.setEnabled(actions.layout and actions.group)
        self.ungroup_action.setEnabled(actions.layout and actions.ungroup)
        self.ungroup_button.setEnabled(actions.layout and actions.ungroup)

    def _update_file_actions(self, has_project: bool, has_tab: bool, idle: bool, context: EditingContext, save_allowed: bool) -> None:
        ready = self._last_build_result is not None
        self.open_build_action.setEnabled(ready)
        self.open_pdf_action.setEnabled(ready)
        self.save_all_action.setEnabled(has_project and save_allowed and bool(self.session and self.session.dirty))
        self.save_action.setEnabled(has_tab and save_allowed and bool(self.session and self.active_tab)
                                    and self.session.documents[self.active_tab.component_id].dirty)
        self.close_tab_action.setEnabled(has_tab and idle)
        for action in (self.zoom_in_action, self.zoom_out_action, self.fit_action, self.actual_size_action):
            action.setEnabled(context.has_tab and not context.text_mode)
        for button in self.drawing_tools.values():
            button.setEnabled(context.has_tab and not context.text_mode and not context.reserved and not context.version_busy)
        self.export_png_zip_action.setEnabled(has_project and idle)
        self.export_project_archive_action.setEnabled(has_project and idle)
        self.import_project_archive_action.setEnabled(idle)
        can_switch_project = self._operation_allowed("project_switch")
        self.new_action.setEnabled(can_switch_project)
        self.open_action.setEnabled(can_switch_project)
        self.recent_projects_menu.setEnabled(can_switch_project)

    def _show_error(self, error: Exception) -> None:
        if isinstance(error, ProjectError):
            message = format_error(error.diagnostic)
        else:
            incident_id = technical_log.log_exception("gui.operation", error, operation="dialog", status="failed")
            message = tr("error.unexpected", incident_id=incident_id)
        owner = self._build_progress if self._build_pending_active_only is not None else self
        QMessageBox.critical(owner, tr("error.title"), message)
        self.statusBar().showMessage(message, 7000)

    def closeEvent(self, event: QCloseEvent) -> None:
        had_pending_build = self._build_pending_active_only is not None
        self._cancel_pending_build(resume_preview=False)
        if self._defer_close_for_operation(event):
            return
        if not self._prepare_preview_close(event):
            if had_pending_build and not self._preview_coordinator.closed:
                self._resume_deferred_preview()
            return
        self.ui_state.save(self)
        event.accept()

    def _defer_close_for_operation(self, event: QCloseEvent) -> bool:
        if self._png_operation is not None:
            self._defer_close_for_png(event)
            return True
        if self._preview_close_pending:
            event.ignore()
            return True
        if self._version_busy_id is not None:
            QMessageBox.information(self, tr("ui.archive_title"), "Дождитесь завершения операции или отмените её.")
            event.ignore()
            return True
        if self._active_build_job is not None:
            self._defer_close_for_build(event)
            return True
        return False

    def _defer_close_for_build(self, event: QCloseEvent) -> None:
        choice = QMessageBox.question(
            self, "Выполняется сборка",
            "Отменить сборку и закрыть окно после освобождения ресурсов?\n"
            "Выберите «Нет», чтобы дождаться текущей сборки, или «Отмена», чтобы вернуться в редактор.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Cancel:
            self._close_after_build = True
            if choice == QMessageBox.StandardButton.Yes:
                self._cancel_build()
        event.ignore()

    def _defer_close_for_png(self, event: QCloseEvent) -> None:
        operation = self._png_operation
        if operation is None or operation.close_confirmed:
            event.ignore()
            return
        operation.close_confirmed = True
        choice, cancel_export, stay = self._png_close_dialog()
        choice.exec()
        if choice.clickedButton() is stay:
            operation.close_confirmed = False
            event.ignore()
            return
        operation.close_after = True
        if choice.clickedButton() is cancel_export:
            operation.cancellation.cancel()
        event.ignore()

    def _png_close_dialog(self):
        choice = QMessageBox(self)
        choice.setWindowTitle(tr("ui.png_export_title"))
        choice.setText(tr("ui.png_close_prompt"))
        cancel = choice.addButton(tr("ui.png_close_cancel_export"), QMessageBox.ButtonRole.DestructiveRole)
        choice.addButton(tr("ui.png_close_wait"), QMessageBox.ButtonRole.AcceptRole)
        stay = choice.addButton(tr("ui.cancel"), QMessageBox.ButtonRole.RejectRole)
        return choice, cancel, stay

    def _prepare_preview_close(self, event: QCloseEvent) -> bool:
        if not self._preview_close_confirmed and not self._confirm_all_dirty():
            event.ignore()
            return False
        self._preview_close_confirmed = True
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_generation += 1
        self._preview_coordinator.shutdown()
        if not self._preview_pool.waitForDone(10000):
            self._preview_close_pending = True
            QTimer.singleShot(25, self._retry_preview_close)
            event.ignore()
            return False
        job = self._preview_coordinator.active
        if job is not None:
            assert self._preview_coordinator.phase is not None
            self._preview_coordinator.finish(job.request.identity.request, self._preview_coordinator.phase)
            self._release_preview_fonts(job)
        return True

    def _retry_preview_close(self) -> None:
        self._preview_close_pending = False
        self.close()
