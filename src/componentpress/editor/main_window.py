"""Stage-five main window with synchronized visual and YAML editing."""

from __future__ import annotations

from pathlib import Path
import re
from dataclasses import dataclass
from uuid import uuid4
from types import MappingProxyType

from pydantic import ValidationError
from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QSignalBlocker, QSize, QThreadPool, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QCloseEvent, QImage, QKeySequence, QUndoGroup
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
)

from componentpress.i18n import catalogs, tr
from componentpress.application.contracts import ComponentHistoryEntry, ComponentOperation
from componentpress.i18n.diagnostics import format_diagnostic
from componentpress.application.sessions import ProjectSession
from componentpress.application.contracts import ArchiveExportResult, BuildRequest, BuildResult, BuildProgress
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.component import DataBinding
from componentpress.application.preview_service import ValidationIssues
from componentpress.execution.cancellation import CancellationToken
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.paths import validate_relative_path
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
from .build_dialog import BuildOptionsDialog
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
from .theme import THEMES, apply_theme, saved_theme, theme_settings
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


class MainWindow(QMainWindow):
    def __init__(self, controller: ProjectController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._tabs: dict[str, DocumentTab] = {}
        self._selection_sync = False
        self._active_build_job: str | None = None; self._build_pending_active_only: bool | None = None
        self._build_operation_id: str | None = None; self._build_session: ProjectSession | None = None; self._build_tab: DocumentTab | None = None; self._build_mode: str | None = None
        self._last_build_result: BuildResult | None = None
        self._build_progress: QProgressDialog | None = None; self._last_build_progress: BuildProgress | None = None
        self._close_after_build = False
        self._preview_session_identity, self._preview_coordinator = uuid4().hex, PreviewCoordinator()
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(1)
        self._preview_signals = PreviewSignals(self)
        self._preview_signals.finished.connect(self._preview_task_finished, Qt.ConnectionType.QueuedConnection)
        self._preview_close_pending = False; self._preview_close_confirmed = False
        self._version_pool = QThreadPool(self)
        self._version_pool.setMaxThreadCount(1)
        self._version_task = None; self._version_busy_id: str | None = None; self._version_task_id: str | None = None; self._pending_restore: tuple[Path, str] | None = None; self._png_operation: PngArchiveOperation | None = None; self._save_reservation_id: str | None = None
        self._version_request: VersionTaskRequest | None = None
        self._version_cancellation: CancellationToken | None = None
        self._version_progress: QProgressDialog | None = None
        self.undo_group = QUndoGroup(self)
        self._component_history = ComponentHistory()
        self._history_tabs: dict[str, DocumentTab | None] = {}
        self._history_documents = {}
        self._project_history_focus = False
        self._theme_settings = theme_settings()
        self._theme = saved_theme(self._theme_settings)
        apply_theme(self._theme)
        self.ui_state = UiState()
        self.setObjectName("mainWindow")
        application = QApplication.instance()
        if application is not None:
            application.setEffectEnabled(Qt.UIEffect.UI_AnimateMenu, False)
            application.setEffectEnabled(Qt.UIEffect.UI_FadeMenu, False)
        self.setWindowTitle(tr("app.name"))
        self.resize(1280, 800)
        self._build_actions()
        self._build_ui()
        self.ui_state.restore(self)
        self._suppress_docks()
        self._connect_signals()
        self._update_actions()

    @property
    def session(self) -> ProjectSession | None:
        return self.controller.session

    @property
    def active_tab(self) -> DocumentTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, DocumentTab) else None

    def _action(self, text: str, shortcut: QKeySequence.StandardKey | str | None = None) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        return action

    @staticmethod
    def _set_action_text(action: QAction, text: str) -> None:
        action.setText(text)
        action.setToolTip(text)
        action.setProperty("accessibleName", text)

    def _toolbar_button(self, action: QAction, icon: str) -> QToolButton:
        set_themed_icon(action, icon)
        self._set_action_text(action, action.text())
        button = QToolButton()
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(action.text())
        button.setAccessibleName(action.text())
        return button

    def _build_actions(self) -> None:
        self.new_action = self._action(tr("action.new"), QKeySequence.StandardKey.New)
        self.open_action = self._action(tr("action.open"), QKeySequence.StandardKey.Open)
        self.save_action = self._action(tr("action.save"), QKeySequence.StandardKey.Save)
        self.save_all_action = self._action(tr("action.save_all"), "Ctrl+Shift+S")
        self.close_tab_action = self._action(tr("action.close_tab"), QKeySequence.StandardKey.Close)
        self.exit_action = self._action(tr("action.exit"), QKeySequence.StandardKey.Quit)
        self.import_project_archive_action = self._action(tr("action.import_archive"))
        self.export_project_archive_action = self._action(tr("action.export_archive"))
        self.export_png_zip_action = self._action(tr("action.export_png"))
        self.add_component_action = self._action(tr("action.add_component"), "Ctrl+Shift+N")
        self.project_settings_action = self._action(tr("action.project_settings"))
        self.image_manager_action = self._action(tr("ui.image_manager"))
        self.about_action = self._action(tr("menu.about"))
        self.undo_action = self._action(tr("action.undo"), QKeySequence.StandardKey.Undo)
        self.redo_action = self._action(tr("action.redo"), QKeySequence.StandardKey.Redo)
        self.delete_action = self._action(tr("action.delete"), QKeySequence.StandardKey.Delete)
        self.delete_component_action = self._action(tr("action.delete_component"))
        self.group_action = self._action(tr("action.group"), "Ctrl+G")
        self.ungroup_action = self._action(tr("action.ungroup"), "Ctrl+Shift+G")
        self.zoom_in_action = self._action(tr("action.zoom_in"), QKeySequence.StandardKey.ZoomIn)
        self.zoom_out_action = self._action(tr("action.zoom_out"), QKeySequence.StandardKey.ZoomOut)
        self.fit_action = self._action(tr("action.fit"), "Ctrl+0")
        self.actual_size_action = self._action(tr("ui.zoom_actual"), "Ctrl+1")
        self.addAction(self.actual_size_action)
        self.build_all_action = self._action(tr("action.build_all"), "Ctrl+B")
        self.build_active_action = self._action(tr("action.build_active"), "Ctrl+Shift+B")
        self.cancel_build_action = self._action(tr("action.cancel_build"))
        self.open_build_action = self._action(tr("action.open_result"))
        self.open_pdf_action = self._action(tr("action.open_pdf"))
        self.validate_active_action = self._action(tr("action.validate_active"))
        self.validate_all_action = self._action(tr("action.validate_all"))
        self.file_menu = self.menuBar().addMenu(tr("menu.file"))
        self.theme_menu = self.file_menu.addMenu(tr("ui.theme"))
        self.theme_actions = {}
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        for mode in THEMES:
            action = QAction(tr(f"ui.theme.{mode}"), self)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == self._theme)
            action.triggered.connect(
                lambda checked, selected=mode: self._select_theme(selected) if checked else None
            )
            self.theme_action_group.addAction(action)
            self.theme_menu.addAction(action)
            self.theme_actions[mode] = action
        self.file_menu.addActions([self.new_action, self.open_action])
        self.file_menu.addSeparator()
        self.import_menu = self.file_menu.addMenu(tr("menu.import"))
        self.import_menu.addAction(self.import_project_archive_action)
        self.export_menu = self.file_menu.addMenu(tr("menu.export"))
        self.export_menu.addActions([self.export_png_zip_action, self.export_project_archive_action])
        self.file_menu.addSeparator()
        self.file_menu.addActions([self.project_settings_action, self.image_manager_action, self.add_component_action])
        self.windows_menu = self.file_menu.addMenu(tr("menu.windows"))
        self.file_menu.addSeparator()
        self.file_menu.addActions([self.save_action, self.save_all_action, self.close_tab_action])
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)
        self.edit_menu = self.menuBar().addMenu(tr("menu.edit"))
        self.edit_menu.addActions([self.undo_action, self.redo_action])
        self.edit_menu.addSeparator()
        self.edit_menu.addActions([self.delete_action, self.delete_component_action, self.group_action, self.ungroup_action])
        self.build_menu = self.menuBar().addMenu(tr("menu.build"))
        self.build_menu.addActions([
            self.build_all_action,
            self.build_active_action,
            self.cancel_build_action,
            self.open_build_action,
            self.open_pdf_action,
        ])
        self.about_menu = self.menuBar().addMenu(tr("menu.about"))
        self.about_menu.addAction(self.about_action)

    def _select_theme(self, mode: str) -> None:
        if mode not in THEMES:
            return
        self._theme = mode
        self._theme_settings.setValue("theme", mode)
        self._theme_settings.sync()
        apply_theme(mode)
        for value, action in self.theme_actions.items():
            action.setChecked(value == mode)

    def _build_ui(self) -> None:
        self.data_mode = CopyModeSwitch()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("documentTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(0)
        self.setCentralWidget(self.tabs)
        self.right_panel = self._right_panel()
        self.right_dock = QDockWidget(tr("panel.workspace"), self)
        self.right_dock.setObjectName("rightDock")
        self.right_dock.setWidget(self.right_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.right_dock)
        self.left_panel = self._left_panel()
        self.events_dock = QDockWidget(tr("panel.events"), self)
        self.events_dock.setObjectName("eventsDock")
        self.events_dock.setWidget(self._events_panel())
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.events_dock)
        self.docks = (self.right_dock, self.events_dock)
        self.resizeDocks([self.right_dock], [300], Qt.Orientation.Horizontal)
        self._saved_dock_visibility = {dock.objectName(): True for dock in self.docks}
        self._project_ui_visible = False
        for dock, area in zip(self.docks, (Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea)):
            action = dock.toggleViewAction()
            self.windows_menu.addAction(action)
            action.triggered.connect(lambda visible, item=dock, default=area: self._dock_visibility_requested(item, default, visible))
        self.quick_toolbar = QToolBar(tr("ui.quick_actions"), self)
        self.quick_toolbar.setObjectName("quickActions")
        self.quick_toolbar.setMovable(False)
        self.quick_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.quick_toolbar)
        self.quick_toolbar.addWidget(self.data_mode)
        self.quick_toolbar.addSeparator()
        for action in (self.add_component_action, self.project_settings_action, self.save_action, self.undo_action, self.redo_action):
            self.quick_toolbar.addAction(action)
        self.quick_toolbar.addSeparator()
        self.validate_all_button = self._toolbar_button(self.validate_all_action, "validate.png")
        self.quick_toolbar.addWidget(self.validate_all_button)
        self.build_active_button = self._toolbar_button(self.build_active_action, "build.png")
        self.build_all_button = self._toolbar_button(self.build_all_action, "build_all.png")
        self.cancel_build_button = self._toolbar_button(self.cancel_build_action, "stop.png")
        self.build_active_widget_action = self.quick_toolbar.addWidget(self.build_active_button)
        self.build_all_widget_action = self.quick_toolbar.addWidget(self.build_all_button)
        self.cancel_build_widget_action = self.quick_toolbar.addWidget(self.cancel_build_button)
        self.cancel_build_widget_action.setVisible(False)
        quick_icons = {
            self.add_component_action: "add.png",
            self.project_settings_action: "settings.png",
            self.save_action: "save.png",
            self.undo_action: "undo.png",
            self.redo_action: "redo.png",
        }
        for action, filename in quick_icons.items():
            set_themed_icon(action, filename)
            action.setToolTip(action.text())
            action.setProperty("accessibleName", action.text())
        self._set_action_text(self.validate_all_action, tr("action.validate_all"))
        self.validate_all_button.setToolTip(self.validate_all_action.text())
        self.validate_all_button.setAccessibleName(self.validate_all_action.text())
        self._update_build_toolbar()
        self.theme_button = QToolButton()
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setText(tr("ui.theme"))
        self.theme_button.setToolTip(tr("ui.theme"))
        self.theme_button.setAccessibleName(tr("ui.theme"))
        self.theme_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.theme_button.setMenu(self.theme_menu)
        self.quick_toolbar.addSeparator()
        self.quick_toolbar.addWidget(self.theme_button)
        self._build_icon_toolbar()
        self.status_project = QLabel(tr("ui.project_closed"))
        self.status_zoom = QLabel("")
        self.statusBar().addWidget(self.status_project, 1)
        self.statusBar().addPermanentWidget(self.status_zoom)
        self._update_project_visibility()
        for panel in (self.right_panel,):
            panel.installEventFilter(self)
            for child in panel.findChildren(QWidget):
                child.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if self._route_text_history_shortcut(watched, event):
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

    def _left_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.hide()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(3, 5, 3, 5)
        # Kept as a child for compatibility with existing data-selection handlers;
        # project source settings now live in the single project dialog.
        data = QGroupBox(tr("ui.excel_data"))
        data_layout = QVBoxLayout(data)
        self.data_source = QComboBox()
        self.data_source.setObjectName("dataSource")
        self.data_sheet = QComboBox()
        self.data_sheet.setObjectName("dataSheet")
        self.data_row = QComboBox()
        self.data_row.setObjectName("dataRow")
        self.data_column = QComboBox()
        self.data_column.setObjectName("dataColumn")
        self.data_chain = QLabel(tr("ui.no_binding"))
        self.data_chain.setObjectName("dataChain")
        self.data_chain.setWordWrap(True)
        for label, widget in (
            (tr("ui.project_source"), self.data_source), (tr("ui.component_sheet"), self.data_sheet),
            (tr("ui.row"), self.data_row), (tr("ui.column"), self.data_column),
        ):
            data_layout.addWidget(QLabel(label))
            data_layout.addWidget(widget)
        data_layout.addWidget(self.data_chain)
        data_buttons = QHBoxLayout()
        self.data_import = QPushButton(tr("ui.import_xlsx"))
        self.data_refresh = QPushButton(tr("ui.refresh"))
        self.data_insert = QPushButton(tr("ui.insert_column", column=tr("ui.column")))
        self.data_export = QPushButton(tr("ui.export_png"))
        self.data_export_zip = QPushButton(tr("ui.png_zip"))
        self.data_validate = QPushButton(tr("ui.validate"))
        self.data_validate_all = QPushButton(tr("ui.validate_all"))
        self.data_clear = QPushButton(tr("ui.remove_xlsx"))
        for button in (self.data_import, self.data_refresh, self.data_insert, self.data_export, self.data_export_zip):
            data_buttons.addWidget(button)
        data_layout.addLayout(data_buttons)
        validation_buttons = QHBoxLayout()
        validation_buttons.addWidget(self.data_validate)
        validation_buttons.addWidget(self.data_validate_all)
        validation_buttons.addWidget(self.data_clear)
        data_layout.addLayout(validation_buttons)
        self.compat_data_panel = data
        data.hide()
        self.image_tool = QPushButton(tr("ui.image"))
        self.image_tool.setObjectName("imageTool")
        self.html_tool = QPushButton(tr("ui.html_text"))
        self.html_tool.setObjectName("htmlTool")
        self.conditional_group_tool = QPushButton(tr("ui.conditional_group"))
        self.conditional_group_tool.setObjectName("conditionalGroupTool")
        self.rectangle_tool = QPushButton(tr("ui.rectangle"))
        self.rectangle_tool.setObjectName("rectangleTool")
        self.ellipse_tool = QPushButton(tr("ui.ellipse"))
        self.ellipse_tool.setObjectName("ellipseTool")
        self.line_tool = QPushButton(tr("ui.line"))
        self.line_tool.setObjectName("lineTool")
        self.select_tool = QPushButton(tr("ui.select"))
        tool_icons = {
            self.select_tool: "cursor.png", self.image_tool: "image.png", self.html_tool: "text.png",
            self.conditional_group_tool: "condition.png", self.rectangle_tool: "rectangle.png",
            self.ellipse_tool: "ellipse.png", self.line_tool: "line.png",
        }
        self.drawing_tools = {
            "select": self.select_tool, "image": self.image_tool, "html": self.html_tool,
            "conditional_group": self.conditional_group_tool,
            "rectangle": self.rectangle_tool, "ellipse": self.ellipse_tool, "line": self.line_tool,
        }
        for button in self.drawing_tools.values():
            button.setCheckable(True)
            button.setEnabled(False)
            button.setToolTip(button.text())
            button.setAccessibleName(button.text())
            set_themed_icon(button, tool_icons[button])
            button.setText("")
            button.setFixedSize(34, 34)
        for button, icon in ((self.layer_up, "up.png"), (self.layer_down, "down.png")):
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
            button.setText("")
            set_themed_icon(button, icon)
        layer_icons = {self.group_button: "group.png", self.ungroup_button: "ungroup.png"}
        for button, icon in layer_icons.items():
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
            set_themed_icon(button, icon)
            button.setText("")
        for button in (self.layer_up, self.layer_down, *layer_icons):
            button.setFixedSize(34, 34)
            button.setIconSize(QSize(22, 22))
        layout.addStretch(1)
        return panel

    def _events_panel(self) -> QWidget:
        panel = QWidget()
        events_layout = QVBoxLayout(panel)
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText(tr("event.search"))
        self.event_list = QListWidget()
        self.clear_events = QPushButton(tr("event.clear"))
        set_themed_icon(self.clear_events, "delete.png")
        self.clear_events.clicked.connect(self.event_list.clear)
        events_layout.addWidget(self.event_search)
        events_layout.addWidget(self.event_list, 1)
        events_layout.addWidget(self.clear_events)
        return panel

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


    def _right_panel(self) -> QWidget:
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("rightPanelTabs")
        components = QWidget()
        component_layout = QVBoxLayout(components)
        component_header = QHBoxLayout()
        component_header.addWidget(QLabel(tr("panel.components")), 1)
        self.add_component_button = QToolButton()
        self.add_component_button.setText("+")
        self.add_component_button.setToolTip(tr("ui.add_component"))
        self.add_component_button.setAccessibleName(tr("ui.add_component"))
        set_themed_icon(self.add_component_button, "add.png")
        self.add_component_button.setText("")
        self.add_component_button.clicked.connect(self.add_component_dialog)
        component_header.addWidget(self.add_component_button)
        component_layout.addLayout(component_header)
        self.component_tree = QTreeView()
        self.component_tree.setObjectName("componentTree")
        self.component_tree.setHeaderHidden(True)
        component_layout.addWidget(self.component_tree)
        self.component_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.component_tree.customContextMenuRequested.connect(self._component_context_menu)
        self.right_tabs.addTab(components, tr("panel.components"))
        elements = QGroupBox(tr("panel.element_order"))
        element_layout = QVBoxLayout(elements)
        self.element_tree = LayerTreeView()
        self.element_tree.setObjectName("elementTree")
        self.element_tree.nodeContextMenuRequested.connect(self._element_tree_context_menu)
        element_layout.addWidget(self.element_tree)
        self.layer_up = QPushButton(tr("ui.layer_up"))
        self.layer_up.setObjectName("layerUp")
        self.layer_down = QPushButton(tr("ui.layer_down"))
        self.layer_down.setObjectName("layerDown")
        self.group_button = QPushButton(tr("ui.group_create"))
        self.ungroup_button = QPushButton(tr("ui.ungroup"))
        self.right_tabs.addTab(elements, tr("panel.elements"))
        self.multi_properties_group = QGroupBox(tr("ui.align_group"))
        multi_layout = QHBoxLayout(self.multi_properties_group)
        self.align_buttons: dict[str, QPushButton] = {}
        for edge, label in (("left", tr("ui.align_left")), ("right", tr("ui.align_right")), ("top", tr("ui.align_top")), ("bottom", tr("ui.align_bottom"))):
            button = QPushButton(label)
            button.setObjectName(f"align{edge.title()}")
            button.clicked.connect(lambda _checked=False, edge=edge: self._align_selected(edge))
            self.align_buttons[edge] = button
            multi_layout.addWidget(button)
        self.multi_properties_group.hide()
        self.component_properties_group = QGroupBox(tr("ui.component_properties"))
        property_layout = QVBoxLayout(self.component_properties_group)
        self.properties = ComponentProperties()
        self.properties.sheetChanged.connect(self._component_property_sheet_changed)
        property_layout.addWidget(self.properties)
        self.element_properties_group = QGroupBox(tr("ui.element_properties"))
        element_property_layout = QVBoxLayout(self.element_properties_group)
        self.element_properties = ElementProperties()
        element_property_layout.addWidget(self.element_properties)
        self.element_properties_group.hide()
        property_panel = QWidget()
        property_layout = QVBoxLayout(property_panel)
        property_layout.setContentsMargins(0, 0, 0, 0)
        property_layout.addWidget(self.multi_properties_group)
        property_layout.addWidget(self.component_properties_group)
        property_layout.addWidget(self.element_properties_group)
        property_layout.addStretch(1)
        self.properties_scroll = QScrollArea()
        self.properties_scroll.setObjectName("propertiesScroll")
        self.properties_scroll.setWidgetResizable(True)
        self.properties_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.properties_scroll.setWidget(property_panel)
        self.properties_scroll.setMinimumHeight(100)
        self.right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_splitter.setObjectName("elementsPropertiesSplitter")
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.addWidget(self.right_tabs)
        self.right_splitter.addWidget(self.properties_scroll)
        self.right_splitter.setStretchFactor(0, 3)
        self.right_splitter.setStretchFactor(1, 2)
        self.right_splitter.setSizes([480, 320])
        self.right_splitter.setMinimumWidth(180)
        return self.right_splitter

    def _update_project_visibility(self) -> None:
        has_project = self.session is not None
        self.edit_menu.menuAction().setVisible(has_project)
        self.build_menu.menuAction().setVisible(has_project)
        self.quick_toolbar.setVisible(has_project)
        self.icon_toolbar.setVisible(has_project)
        self.windows_menu.menuAction().setEnabled(has_project)
        for dock in self.docks:
            dock.toggleViewAction().setEnabled(has_project)
        if has_project != self._project_ui_visible:
            self._project_ui_visible = has_project
            for dock in self.docks:
                if has_project:
                    dock.setVisible(self._saved_dock_visibility.get(dock.objectName(), True))
                else:
                    dock.hide()
        self.import_menu.menuAction().setVisible(has_project)
        self.export_menu.menuAction().setVisible(has_project)

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

    def _show_about(self) -> None:
        from importlib import import_module
        from importlib.metadata import PackageNotFoundError, metadata, version
        from PySide6 import __version__ as qt_version
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QComboBox, QFileDialog, QFormLayout, QHBoxLayout
        import platform

        from componentpress.version import APPLICATION_VERSION
        from componentpress.domain.schema import SCHEMA_VERSION

        dialog = QDialog(self)
        dialog.setObjectName("aboutDialog")
        dialog.setWindowTitle(tr("about.title"))
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"ComponentPress {APPLICATION_VERSION}\n{tr('about.description')}"))
        details = QFormLayout()
        details.addRow(QLabel(tr("about.schema", version=SCHEMA_VERSION)))
        details.addRow(QLabel(tr("about.runtime", python=platform.python_version(), qt=qt_version)))
        dependency_rows = []
        packages = {
            "openpyxl": ("openpyxl", "MIT"),
            "ruamel.yaml": ("ruamel.yaml", "MIT"),
            "pydantic": ("pydantic", "MIT"),
            "Jinja2": ("jinja2", "BSD-3-Clause"),
            "PySide6": ("PySide6", "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"),
            "PyQtDarkTheme-fork": ("qdarktheme", "MIT"),
        }
        for package, (module_name, fallback_license) in packages.items():
            try:
                try:
                    package_version = version(package)
                    package_metadata = metadata(package)
                    license_name = (package_metadata.get("License-Expression") or package_metadata.get("License") or fallback_license).splitlines()[0][:100]
                except PackageNotFoundError:
                    package_module = import_module(module_name)
                    package_version = getattr(package_module, "__version__", getattr(package_module, "VERSION", "included"))
                    license_name = fallback_license
                dependency_rows.append(f"{package} {package_version} ({license_name})")
            except (PackageNotFoundError, ImportError):
                continue
        dependency_label = QLabel(tr("about.dependencies", dependencies="; ".join(dependency_rows)))
        dependency_label.setWordWrap(True)
        details.addRow(dependency_label)
        details.addRow(QLabel(tr("about.license")))
        details.addRow(QLabel(tr("about.theme_licenses")))
        layout.addLayout(details)

        language_row = QHBoxLayout()
        language_row.addWidget(QLabel(tr("about.language")))
        selector = QComboBox()
        selector.setObjectName("languageSelector")
        selector.addItem(tr("language.ru"), "ru")
        selector.addItem(tr("language.en"), "en")
        selector.setCurrentIndex(0 if language() == "ru" else 1)
        language_row.addWidget(selector)
        layout.addLayout(language_row)
        restart = QLabel(tr("about.restart"))
        layout.addWidget(restart)

        actions = QDialogButtonBox()
        open_logs = actions.addButton(tr("about.open_logs"), QDialogButtonBox.ButtonRole.ActionRole)
        report = actions.addButton(tr("about.create_report"), QDialogButtonBox.ButtonRole.ActionRole)
        close_button = actions.addButton(QDialogButtonBox.StandardButton.Close)
        close_button.setText(tr("ui.close"))
        layout.addWidget(actions)

        def save_language(index: int) -> None:
            selected = selector.itemData(index)
            if selected != language():
                settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ComponentPress", "ComponentPress")
                settings.setValue("language", selected)
                settings.sync()
                restart.setText(tr("about.restart"))

        def show_report() -> None:
            filename, _filter = QFileDialog.getSaveFileName(
                dialog, tr("about.report_title"), "componentpress-error.zip", tr("about.report_filter")
            )
            if not filename:
                return
            target = Path(filename)
            if target.exists() and QMessageBox.question(dialog, tr("about.report_title"), str(target)) != QMessageBox.StandardButton.Yes:
                return
            try:
                result = self._last_build_result
                diagnostic = next(iter(result.diagnostics), None) if result is not None else None
                technical_log.create_report(
                    target,
                    incident_id=getattr(diagnostic, "incident_id", None),
                    code=getattr(diagnostic, "code", None),
                    job_id=result.job_id if result is not None else None,
                    phase=result.status if result is not None else None,
                    status=result.status if result is not None else None,
                    mode=result.mode if result is not None else None,
                )
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                QMessageBox.warning(dialog, tr("about.report_title"), tr("about.report_failed", error=type(exc).__name__))
                return
            QMessageBox.information(dialog, tr("about.report_title"), tr("about.report_saved", path=target.name))

        def show_log_folder() -> None:
            folder = technical_log.log_folder()
            if folder is None or not folder.exists() or not PlatformServices.open_path(folder):
                QMessageBox.information(dialog, tr("about.title"), tr("about.open_logs_failed"))

        selector.currentIndexChanged.connect(save_language)
        open_logs.clicked.connect(show_log_folder)
        report.clicked.connect(show_report)
        actions.rejected.connect(dialog.reject)
        dialog.resize(720, 340)
        dialog.exec()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def _connect_signals(self) -> None:
        self.new_action.triggered.connect(self.new_project_dialog)
        self.open_action.triggered.connect(self.open_project_dialog)
        self.save_action.triggered.connect(self.save_active)
        self.save_all_action.triggered.connect(self.save_all)
        self.close_tab_action.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        self.exit_action.triggered.connect(self.close)
        self.import_project_archive_action.triggered.connect(self._import_project_archive)
        self.export_project_archive_action.triggered.connect(self._export_project_archive)
        self.export_png_zip_action.triggered.connect(self._export_png_archive)
        self.add_component_action.triggered.connect(self.add_component_dialog)
        self.project_settings_action.triggered.connect(self.project_settings_dialog)
        self.image_manager_action.triggered.connect(self.open_image_manager)
        self.about_action.triggered.connect(self._show_about)
        self.validate_active_action.triggered.connect(self._validate_active)
        self.validate_all_action.triggered.connect(lambda: self._validate_all(force_read=True))
        self.build_all_action.triggered.connect(lambda: self._start_build(False))
        self.build_active_action.triggered.connect(lambda: self._start_build(True))
        self.cancel_build_action.triggered.connect(self._cancel_build)
        self.open_build_action.triggered.connect(lambda: self._open_build_result(False))
        self.open_pdf_action.triggered.connect(lambda: self._open_build_result(True))
        self.undo_action.triggered.connect(self._undo)
        self.redo_action.triggered.connect(self._redo)
        self.delete_action.triggered.connect(self.delete_selected)
        self.delete_component_action.triggered.connect(lambda: self.active_tab and self.delete_component(self.active_tab.component_id))
        self.group_action.triggered.connect(lambda: self.group_selected())
        self.ungroup_action.triggered.connect(lambda: self.ungroup_selected())
        self.zoom_in_action.triggered.connect(lambda: self._zoom_active(1.2))
        self.zoom_out_action.triggered.connect(lambda: self._zoom_active(1 / 1.2))
        self.fit_action.triggered.connect(lambda: self.active_tab and self.active_tab.canvas.fit_page())
        self.actual_size_action.triggered.connect(lambda: self.active_tab and self.active_tab.canvas.actual_size())
        self.component_tree.doubleClicked.connect(self._open_component_index)
        self.component_tree.clicked.connect(self._open_component_index)
        self.tabs.currentChanged.connect(self._active_tab_changed)
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)
        self.select_tool.clicked.connect(self._select_tool)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.properties.nameChanged.connect(lambda value: self._edit_active(name=value))
        self.properties.sizeChanged.connect(self._edit_active_size)
        self.properties.backgroundChanged.connect(lambda value: self._edit_active(background=value))
        self.properties.backgroundColorRequested.connect(self._pick_background_color)
        self.image_tool.clicked.connect(lambda: self._begin_tool("image"))
        self.html_tool.clicked.connect(lambda: self._begin_tool("html"))
        self.conditional_group_tool.clicked.connect(lambda: self._begin_tool("conditional_group"))
        self.rectangle_tool.clicked.connect(lambda: self._begin_tool("rectangle"))
        self.ellipse_tool.clicked.connect(lambda: self._begin_tool("ellipse"))
        self.line_tool.clicked.connect(lambda: self._begin_tool("line"))
        self.layer_up.clicked.connect(lambda: self.reorder_selected(1))
        self.layer_down.clicked.connect(lambda: self.reorder_selected(-1))
        self.group_button.clicked.connect(lambda: self.group_selected())
        self.ungroup_button.clicked.connect(lambda: self.ungroup_selected())
        self.element_tree.layerDropRequested.connect(self._layer_drop)
        self.element_tree.lockToggleRequested.connect(self._toggle_element_lock)
        self.element_properties.nodeChanged.connect(self._edit_selected_node)
        self.element_properties.selectImageRequested.connect(self.select_element_image)
        self.element_properties.rotationRequested.connect(self._rotate_selected)
        self.element_properties.insertImageRequested.connect(self.insert_html_image)
        self.element_properties.colorRequested.connect(self._pick_node_color)
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
        self.event_search.textChanged.connect(self._filter_events)

    def load_session(self, session: ProjectSession, *, reservation: str | None = None) -> None:
        """Install an already opened session; useful to bootstrap and to test the UI."""
        if not self._operation_allowed("project_switch") and reservation != self._version_busy_id:
            return
        self._clear_tabs(discard=True, reservation=reservation)
        self._preview_session_identity = uuid4().hex
        self.controller.set_session(session)
        self._clear_component_history()
        self._project_history_focus = False
        self._refresh_component_tree()
        self.setWindowTitle(f"{session.snapshot.model.name} — ComponentPress")
        self.status_project.setText(str(session.snapshot.root))
        self._update_project_visibility()
        if self.controller.recovery_diagnostics:
            details = "; ".join(format_diagnostic(item) for item in self.controller.recovery_diagnostics)
            self.statusBar().showMessage(f"Восстановление операций: {details}", 15000)
        if self.controller.preview is not None:
            self.controller.preview.invalidate(session.snapshot.root)
            self._validate_all(force_read=True)
        self._update_actions()

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
        if not self._operation_allowed("project_switch"):
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("archive.import_title"), "", tr("archive.filter"))
        if not filename:
            return
        parent = QFileDialog.getExistingDirectory(self, tr("archive.restore_directory"))
        if not parent:
            return
        folder_name, accepted = QInputDialog.getText(self, tr("archive.restored_name"), tr("archive.restored_folder_name"))
        if not accepted or not folder_name.strip():
            return
        if not self._valid_archive_folder_name(folder_name):
            return
        destination = Path(parent) / folder_name.strip()
        if destination.exists():
            QMessageBox.warning(self, tr("archive.import_title"), tr("archive.destination_exists", path=destination))
            return
        if not self.save_all():
            return
        request = RestoreArchiveRequest(Path(filename), destination)
        self._start_version_task(
            request,
            lambda cancellation, progress: self.controller.service.versions.restore_path(
                request.archive_path, request.destination, cancellation=cancellation, on_progress=progress),
        )

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
            try:
                self.controller.update_project_settings(
                    name=values["name"], version=values["version"], prod=values["prod"], test=values["test"],
                    data_source_file=values["source_file"],
                    clear_data_source=not values["source"] and values["source_file"] is None,
                )
                self.setWindowTitle(f"{values['name']} — ComponentPress")
                self._invalidate_preview_cache()
                self._active_tab_changed(self.tabs.currentIndex())
                return
            except ProjectError as exc:
                self._show_error(exc)
                if exc.diagnostic.code not in {"PROJECT_VERSION_INVALID", "PROJECT_SETTINGS_INVALID"}:
                    return
            except (ValidationError, ValueError, OSError) as exc:
                self._show_error(exc)
                return

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
        document = self.session.documents[component_id]
        self._push_component_history(ComponentOperation.CREATE, component_id, document)
        self._project_history_focus = True
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
        self._push_component_history(ComponentOperation.CREATE, component_id, self.session.documents[component_id])
        self._project_history_focus = True
        self.statusBar().showMessage("Компонент добавлен", 3000)
        return True

    def edit_component_dialog(self, component_id: str) -> None:
        if self.session is None or component_id not in self.session.documents:
            return
        document = self.session.documents[component_id]
        if document.has_invalid_draft:
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
        if confirm and not self._confirm_component_delete(document.model.name):
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
        tab.canvas.moveRequested.connect(self._move_selected)
        tab.canvas.nudgeRequested.connect(self._move_selected)
        tab.canvas.resizeRequested.connect(self._resize_selected)
        tab.canvas.lineResizeRequested.connect(self._resize_line_selected)
        tab.canvas.placementRequested.connect(self._place_element)
        tab.canvas.drawRequested.connect(self._draw_element)
        tab.canvas.toolCancelled.connect(self._tool_cancelled)
        tab.canvas.deleteRequested.connect(self.delete_selected)

    def _install_component_tab(self, tab: DocumentTab, component_id: str) -> None:
        # Seed the editor before adding it to the tab widget: palette events can
        # rehighlight it synchronously, and must not turn an empty editor into a draft.
        tab.set_text(self.session.documents[component_id].current_text)
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
                candidate.canvas.release_preview()
        tab = self.active_tab
        self._sync_component_tree_selection()
        if tab is None or self.session is None:
            self.undo_group.setActiveStack(None)
            self.element_tree.setModel(None)
            self.properties.show_component(None)
            self.element_properties.show_node(None)
            self.component_properties_group.show()
            self.element_properties_group.hide()
            self.status_zoom.setText("")
        else:
            self.undo_group.setActiveStack(tab.undo_stack)
            document = self.session.documents[tab.component_id]
            valid_ids = locations(document.model)
            selected = tuple(node_id for node_id in tab.canvas.selected_ids if node_id in valid_ids)
            self._install_element_model(document.model, selected)
            self.properties.show_component(document.model)
            tab.set_text(document.current_text)
            self.status_zoom.setText(tr("ui.zoom_status", percent=tab.canvas.zoom_percent))
            self._populate_data_panel()
            self._project_history_focus = False
            self._refresh_preview(tab)
        self._update_actions()

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
        binding = document.model.data
        source = self.session.snapshot.model.data_source
        sources = [(tr("ui.data_no_xlsx"), None)] + ([(source.path, "main")] if source else [])
        self._fill_combo(self.data_source, sources, "main" if source else None)
        sheets: list[tuple[str, object]] = []
        if source is not None and self.controller.preview is not None:
            try:
                from componentpress.project_io.paths import resolve_project_path
                names = self.controller.preview.reader.sheet_names(resolve_project_path(self.session.snapshot.root, source.path))
                sheets = [(name, name) for name in names]
            except ProjectError as exc:
                message = format_diagnostic(exc.diagnostic)
                self.statusBar().showMessage(message, 7000)
        self._fill_combo(self.data_sheet, sheets, binding.sheet if binding else None)
        self.properties.set_sheets(tuple(str(value) for _, value in sheets if value))
        self.properties.show_component(document.model)
        data = self._refresh_data(force=False, render=False) if binding is not None else None
        headers = list(data.headers) if data is not None else []
        self._fill_combo(self.data_column, [(header, header) for header in headers], self.data_column.currentData())
        self._populate_rows(data)
        self._update_data_chain()
        enabled = binding is not None and data is not None
        self.data_row.setEnabled(enabled)
        self.data_column.setEnabled(enabled)
        self.data_insert.setEnabled(enabled)
        self.data_export.setEnabled(enabled)

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
            is_excel = self.session.documents[self.active_tab.component_id].model.data is not None
            if data is None and is_excel:
                self.active_tab.preview_row_number = None
                self.active_tab.canvas.clear_document()
                self.statusBar().showMessage(tr("ui.mode_requires_validation"), 5000)
            elif is_excel and data is not None and not any(copies_for_mode(row, self.data_mode.mode) > 0 for row in data.rows):
                self.active_tab.preview_row_number = None
                self.active_tab.canvas.clear_document()
                self.statusBar().showMessage(tr("ui.preview_no_instances"), 5000)
            else:
                self._refresh_preview(self.active_tab)
            self._update_preview_navigation()

    def _add_events(self, diagnostics) -> None:
        reveal = False
        for diagnostic in diagnostics:
            technical_log.log_event("project.diagnostic", code=getattr(diagnostic, "code", "UNKNOWN"), status=getattr(diagnostic, "severity", "error"))
            self.event_list.addItem(format_diagnostic(diagnostic))
            if getattr(diagnostic, "severity", "error") != "info":
                reveal = True
        self._filter_events(self.event_search.text())
        if reveal:
            self.events_dock.show()

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

    def _validate_active(self) -> None:
        if self.session is None or self.active_tab is None or self.controller.preview is None:
            return
        component_id = self.active_tab.component_id
        if not self._apply_draft(component_id):
            return
        component = self.session.documents[component_id].model
        try:
            snapshot = self._snapshot_for_current_documents()
            if snapshot is None:
                return
            diagnostics = self.controller.preview.validate_all(snapshot, manual=True, component_ids=(component_id,))
            if diagnostics:
                other_paths = {document.path for other_id, document in snapshot.documents.items() if other_id != component_id}
                current_failed = any(item.path == snapshot.documents[component_id].path for item in diagnostics)
                if current_failed or not all(item.path in other_paths for item in diagnostics):
                    self._add_events(diagnostics)
                    self.statusBar().showMessage(tr("ui.error_count", count=len(diagnostics)), 7000)
                    return
                self._add_events(diagnostics)
                self.statusBar().showMessage(tr("ui.validation_other_components"), 7000)
            data = self.controller.preview.cached_data(snapshot, component_id, component=component)
        except ValidationIssues as exc:
            self._add_events(exc.diagnostics)
            self.statusBar().showMessage(tr("ui.error_count", count=len(exc.diagnostics)), 7000)
            return
        except ProjectError as exc:
            self._add_events((exc.diagnostic,))
            self.statusBar().showMessage(tr("ui.validation_failed"), 7000)
            return
        if data is None:
            self._add_events((Diagnostic("VALIDATION_OK", "", snapshot.documents[component_id].path, source="main", severity="info", message_key="ui.validation_component_ok", params={"name": component.name}),))
        else:
            self._add_events((Diagnostic("VALIDATION_OK", "", data.path, source="main", sheet=data.sheet, severity="info", message_key="ui.validation_sheet_ok", params={"sheet": data.sheet, "area": data.area, "count": len(data.rows)}),))
        self._populate_data_panel()
        self._refresh_preview(self.active_tab)

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
        if self.session.documents[tab.component_id].model.data is None:
            return None
        try:
            component = self.session.documents[tab.component_id].model
            if force:
                diagnostics = self.controller.preview.validate_all(
                    self._snapshot_for_current_documents(), manual=True, component_ids=(tab.component_id,),
                )
                if diagnostics:
                    self._add_events(diagnostics)
                    self.statusBar().showMessage(format_diagnostic(diagnostics[0]), 10000)
                    return None
                data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)
            else:
                data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)
        except ProjectError as exc:
            self._add_events(exc.diagnostics if isinstance(exc, ValidationIssues) else (exc.diagnostic,))
            self.statusBar().showMessage(format_diagnostic(exc.diagnostic), 10000)
            return None
        if data is not None:
            self._populate_rows(data)
            headers = [(header, header) for header in data.headers]
            self._fill_combo(self.data_column, headers, self.data_column.currentData())
            if render:
                self._refresh_preview(tab)
            self.statusBar().showMessage(tr("ui.data_updated", sheet=data.sheet, version=data.version), 3500)
        return data

    def _refresh_preview(self, tab: DocumentTab, *, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        if self.session is None:
            return False
        document = self.session.documents[tab.component_id]
        if document.model.data is None or self.controller.preview is None:
            tab.resolved_component = None
            active_component = document.model
            bindings_resolved = False
            self._update_preview_navigation(tab)
        else:
            tab.resolved_component = None
            cached = self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=document.model)
            if cached is None:
                self._clear_preview(tab)
                return False
            eligible = [row for row in cached.rows if copies_for_mode(row, self.data_mode.mode) > 0]
            if not eligible:
                tab.preview_row_number = None
                self._clear_preview(tab)
                return False
            if tab.preview_row_number not in {row.row_number for row in eligible}:
                tab.preview_row_number = eligible[0].row_number
            try:
                resolved = self.controller.preview.select_row(
                    self.session.snapshot, tab.component_id, row_number=tab.preview_row_number,
                    mode=self.data_mode.mode,
                    component=document.model,
                    validate_resources=False,
                )
            except ProjectError as exc:
                tab.previewError.emit(format_diagnostic(exc.diagnostic))
                if self.active_tab is tab and len(tab.canvas.selected_ids) == 1:
                    self._show_selection(tab.canvas.selected_ids)
                return False
            tab.preview_row_number = resolved.row.row_number if resolved.row else None
            tab.resolved_component = resolved.component
            active_component = resolved.component
            bindings_resolved = True
        dpi = self._preview_dpi_for_visible_views(tab, active_component)
        tab.queue_preview(
            self.session, component=active_component, dpi=dpi, fit=fit,
            selected=selected, bindings_resolved=bindings_resolved,
        )
        self._update_preview_navigation(tab)
        return True

    def _clear_preview(self, tab: DocumentTab) -> None:
        tab.invalidate_preview()
        tab.canvas.clear_document()
        self._update_preview_navigation(tab)

    def _step_preview(self, direction: int, *, endpoint: str | None = None) -> None:
        tab = self.active_tab
        if self.session is None or tab is None or tab.resolved_component is None or self.controller.preview is None:
            return
        component = self.session.documents[tab.component_id].model
        if component.data is None:
            return
        data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id, component=component)
        if data is None:
            return
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
            position = tr("ui.instance_position", current=1, total=1) if document and document.model.data is None else tr("ui.instance_position_empty")
            tab.instance_position.setText(position)
            tab.instance_position.setAccessibleName(position)
            for button in (tab.preview_first, tab.preview_previous, tab.preview_next, tab.preview_last):
                button.setEnabled(False)
            return
        eligible = [row for row in data.rows if copies_for_mode(row, self.data_mode.mode) > 0]
        index = next((i for i, row in enumerate(eligible) if row.row_number == tab.preview_row_number), -1)
        position = tr("ui.instance_position", current=index + 1, total=len(eligible)) if index >= 0 else tr("ui.instance_position_empty")
        tab.instance_position.setText(position)
        tab.instance_position.setAccessibleName(position)
        tab.preview_previous.setEnabled(index > 0)
        tab.preview_next.setEnabled(0 <= index < len(eligible) - 1)
        tab.preview_first.setEnabled(index > 0)
        tab.preview_last.setEnabled(0 <= index < len(eligible) - 1)

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
            job.fonts.require(family, owner=job.request.snapshot.root / "project.yaml", field="font_family")

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
            self._show_build_options(self._build_pending_active_only)

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
        diagnostics = []
        for item in quality:
            warning = item.diagnostic(document.path, source=source, sheet=sheet)
            if warning is None:
                continue
            found = indexed.get(item.node_id)
            if row is not None and found is not None:
                node = found.node
                column = (
                    node.source_column if isinstance(node, ImageNode) and node.source_mode == "column"
                    else node.content_column if isinstance(node, HtmlNode) and node.content_mode == "column"
                    else None
                )
                cell = next((value.coordinate for name, value in row.values.items() if column and name.strip().casefold() == column.strip().casefold()), None)
                message = tr(warning.message_key, **(warning.params or {})) if warning.message_key else warning.message
                warning = Diagnostic(
                    warning.code, "", warning.path, warning.field, warning.line, warning.severity,
                    warning.source, warning.sheet, cell, warning.node_id,
                    message_key="ui.excel_row_prefix", params={"row": row.row_number, "message": message},
                )
            diagnostics.append(warning)
        return tuple(diagnostics)

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
                if document.model.data is not None:
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
        if self.session is None or self.controller.build is None or not self._operation_allowed("build"):
            return
        self._build_pending_active_only = active_only
        self._build_operation_id = uuid4().hex
        self._build_session = self.session
        self._build_tab = self.active_tab
        self._build_mode = self.data_mode.mode
        if not self._save_all_for_reservation(self._build_operation_id):
            self._cancel_pending_build()
            return
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_deferred = tab.preview_arguments is not None
        self._preview_coordinator.discard_pending()
        self._update_actions()
        if self._preview_coordinator.active is not None:
            self.statusBar().showMessage("Предпросмотр завершает работу; сборка начнётся сразу после неё")
            return
        self._show_build_options(active_only)

    def _show_build_options(self, active_only: bool) -> None:
        if self._build_pending_active_only is None or self.session is None:
            return
        if self._build_session is not self.session or active_only and self._build_tab is not self.active_tab:
            self._cancel_pending_build()
            return
        dialog = BuildOptionsDialog(self.session.snapshot.model.print, self, mode=self._build_mode)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self._cancel_pending_build()
            return
        if (self._build_pending_active_only is None or self.session is None
                or self._build_session is not self.session or active_only and self._build_tab is not self.active_tab):
            self._cancel_pending_build()
            return
        self._launch_build(dialog, active_only, self._build_mode)

    def _launch_build(self, dialog, active_only: bool, mode: str) -> None:
        component_ids = None
        if active_only:
            if self._build_tab is None:
                self._cancel_pending_build()
                return
            component_ids = (self._build_tab.component_id,)
        request = BuildRequest(
            component_ids=component_ids,
            mode=mode,
            print_settings=dialog.print_settings(),
            max_render_workers=dialog.workers.value(),
        )
        self._start_build_request(request)

    def _start_build_request(self, request: BuildRequest) -> None:
        try:
            job_id = self._start_build_worker(request)
        except Exception as exc:
            self._reject_build_start(exc)
            return
        self._active_build_job = job_id
        self._release_build_reservation()
        self.statusBar().showMessage(f"Сборка запущена: {job_id}")
        self._update_actions()

    def _start_build_worker(self, request: BuildRequest) -> str:
        self._build_progress = self._build_progress_dialog()
        self._last_build_progress = None
        return self.controller.start_build(request, on_progress=self._on_build_progress,
                                           on_finished=self._queue_build_finished)

    def _reject_build_start(self, error: Exception) -> None:
        if self._build_progress is not None:
            self._build_progress.close()
        self._build_progress = None
        self._cancel_pending_build()
        self._show_error(error)

    def _release_build_reservation(self) -> None:
        self._build_pending_active_only = self._build_operation_id = None
        self._build_session = self._build_tab = self._build_mode = None

    def _build_progress_dialog(self) -> QProgressDialog:
        progress = QProgressDialog(tr("ui.build_prepare"), tr("action.cancel_build"), 0, 0, self)
        progress.setWindowTitle("Сборка ComponentPress")
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel_build)
        progress.show()
        return progress

    def _cancel_pending_build(self, *, resume_preview: bool = True) -> None:
        if self._build_pending_active_only is None:
            return
        self._build_pending_active_only = None
        self._build_operation_id = None
        self._build_session = None
        self._build_tab = None
        self._build_mode = None
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
        if progress.job_id != self._active_build_job or self._build_progress is None:
            return
        if not self._accept_build_progress(progress):
            return
        self._last_build_progress = progress
        self._build_progress.setRange(0, max(progress.total, 1))
        self._build_progress.setValue(progress.completed)
        self._build_progress.setLabelText(progress.message or progress.phase)
        if progress.phase == "preparing" and progress.total and progress.completed >= progress.total:
            self._update_actions()

    def _accept_build_progress(self, progress: BuildProgress) -> bool:
        previous = self._last_build_progress
        order = {"preparing": 0, "rendering": 1, "assembling_pdf": 2}
        return previous is None or (
            order.get(progress.phase, 0) >= order.get(previous.phase, 0)
            and (progress.phase != previous.phase or progress.completed >= previous.completed)
        )

    def _on_build_finished(self, result: BuildResult) -> None:
        if self._build_progress is not None:
            self._build_progress.close()
            self._build_progress.deleteLater()
            self._build_progress = None
        self._active_build_job = None
        self._last_build_result = result if result.status == "succeeded" else self._last_build_result
        self._update_actions()
        if result.status == "succeeded":
            suffix = " (очистка будет повторена)" if result.cleanup_state == "cleanup_pending" else ""
            warning = f"; предупреждения: {'; '.join(format_diagnostic(item) for item in result.diagnostics)}" if result.diagnostics else ""
            self.statusBar().showMessage(f"Сборка готова: {result.output_directory}{suffix}{warning}", 12000)
        else:
            message = "\n".join(format_diagnostic(item) for item in result.diagnostics) or f"Сборка: {result.status}"
            QMessageBox.warning(self, "Сборка не завершена", format_error(ValueError(message)))
        if self.active_tab is not None and self.active_tab.canvas._pixmap_item is None:
            self._refresh_preview(self.active_tab)
        if self.active_tab is not None and self.active_tab.preview_deferred:
            self.active_tab.preview_deferred = False
            self._refresh_preview(self.active_tab)
        if self._close_after_build:
            self._close_after_build = False
            self.close()

    def _queue_build_finished(self, result: BuildResult) -> None:
        QTimer.singleShot(0, lambda: self._deliver_build_finished(result))

    def _deliver_build_finished(self, result: BuildResult) -> None:
        if result.job_id == self._active_build_job:
            self._on_build_finished(result)

    def _cancel_build(self) -> None:
        if self._active_build_job and self.controller.cancel_build(self._active_build_job):
            self.statusBar().showMessage("Запрошена отмена; выполняющиеся операции освобождают ресурсы…")

    def _open_build_result(self, pdf: bool) -> None:
        result = self._last_build_result
        if result is None:
            return
        target = result.pdf_path if pdf else result.output_directory
        if target is not None and not PlatformServices.open_path(target):
            self.statusBar().showMessage(f"Не удалось открыть: {target}", 7000)

    def _request_mode(self, tab: DocumentTab, mode: str) -> None:
        if self.session is None or not self._user_editing_allowed():
            return
        if mode == "text":
            tab.canvas.cancel_tool()
            tab.set_text(self.session.documents[tab.component_id].current_text)
            tab.set_mode("text")
            tab.text_editor.setFocus()
        elif self._apply_draft(tab.component_id):
            tab.set_mode("layout")
            self._refresh_preview(tab)
        else:
            tab.set_mode("text")
        self._active_tab_changed(self.tabs.currentIndex())

    def _draft_changed(self, tab: DocumentTab, text: str) -> None:
        if self.session is None or not self._user_editing_allowed():
            return
        document = self.session.documents[tab.component_id]
        try:
            self.controller.prepare_text(tab.component_id, text)
        except ProjectError as exc:
            document.set_draft(text, (exc.diagnostic,))
            tab.show_diagnostic(format_diagnostic(exc.diagnostic))
        else:
            document.set_draft(text)
            tab.show_diagnostic()
        self._update_tab_title(tab.component_id)
        self._update_actions()

    def _apply_draft(self, component_id: str, *, reservation: str | None = None) -> bool:
        if self.session is None or not self._user_editing_allowed(reservation):
            return False
        document = self.session.documents[component_id]
        if document.draft_text is None:
            return True
        tab = self._tabs.get(component_id)
        try:
            after = self.controller.prepare_text(component_id, document.draft_text)
        except ProjectError as exc:
            document.set_draft(document.draft_text, (exc.diagnostic,))
            if tab is not None:
                tab.show_diagnostic(format_diagnostic(exc.diagnostic))
            self.statusBar().showMessage(format_diagnostic(exc.diagnostic), 7000)
            return False
        if after.text == document.committed.text:
            document.clear_draft()
            return True
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
            tab.show_diagnostic()
        else:
            document.replace_snapshot(after)
            self._document_changed(component_id)
        return True

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
        del source  # Kept in the API to make signal origins explicit at call sites.
        tab = self.active_tab
        if tab is None or self.session is None or self._selection_sync:
            return
        component = self.session.documents[tab.component_id].model
        index = locations(component)
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
        selected = tuple(normalized)
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

    def _show_selection(self, ids: tuple[str, ...]) -> None:
        tab = self.active_tab
        if tab is None or self.session is None:
            return
        if len(ids) == 1:
            found = locations(self.session.documents[tab.component_id].model).get(ids[0])
            if found is None:
                self._show_selection(())
                return
            node = found.node
            columns = ()
            if self.controller.preview is not None:
                try:
                    data = self.controller.preview.cached_data(self.session.snapshot, tab.component_id)
                    columns = data.headers if data is not None else ()
                except ProjectError:
                    pass
            component = self.session.documents[tab.component_id].model
            preview_node = locations(tab.resolved_component).get(node.id) if isinstance(node, ConditionalGroupNode) and tab.resolved_component is not None else None
            matches = preview_node.node.condition_matches if preview_node is not None and isinstance(preview_node.node, ConditionalGroupNode) else None
            font_families = ()
            font_error = None
            if isinstance(node, HtmlNode) and self._operation_allowed("resource_write"):
                try:
                    font_families = available_font_families(self.session.snapshot.root)
                except ProjectError as exc:
                    font_error = exc
            self.element_properties.show_node(node, columns, component, matches, font_families)
            self.element_properties.show_image_quality(
                item for item in tab.image_quality if item.node_id == node.id
            )
            self.component_properties_group.hide()
            self.multi_properties_group.hide()
            self.element_properties_group.show()
            self.element_properties.set_edit_locked(ids[0] in effectively_locked_ids(component))
            message = format_diagnostic(font_error.diagnostic) if font_error is not None else f"Выбран элемент: {node.id}"
            self.statusBar().showMessage(message, 7000 if font_error is not None else 2500)
        else:
            self.element_properties.show_node(None)
            self.element_properties_group.hide()
            if len(ids) > 1:
                self.component_properties_group.hide()
                self.multi_properties_group.show()
            else:
                self.multi_properties_group.hide()
                self.component_properties_group.show()
                self.properties.show_component(self.session.documents[tab.component_id].model)
        self._update_actions()

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
            values = self.session.documents[tab.component_id].model.model_dump(mode="python")
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
        if self.controller.preview is not None:
            snapshot = self._snapshot_for_current_documents()
            if snapshot is not None:
                diagnostics = self.controller.preview.validate_all(
                    snapshot, manual=False, component_ids=(component_id,),
                )
                if diagnostics:
                    self._add_events(diagnostics)
        tab = self._tabs.get(component_id)
        if tab is not None:
            current_ids = locations(self.session.documents[component_id].model)
            if selected is None:
                selected = tuple(node_id for node_id in tab.canvas.selected_ids if node_id in current_ids)
            else:
                selected = tuple(node_id for node_id in selected if node_id in current_ids)
            self._refresh_preview(tab, selected=selected)
            tab.set_text(self.session.documents[component_id].current_text)
        if tab is self.active_tab:
            document = self.session.documents[component_id]
            self._install_element_model(document.model, selected or ())
            self.properties.show_component(document.model)
            self._populate_data_panel()
        self._update_tab_title(component_id)
        self._refresh_component_tree()
        self._update_actions()

    def _after_undo_redo(self) -> None:
        self._update_actions()

    def _selected_ids(self) -> tuple[str, ...]:
        if self.active_tab is None:
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
        selected = self._selected_ids()
        if not selected:
            return False
        used_ids = set(locations(component))
        used_names = {item.node.name for item in locations(component).values()}
        created_ids: dict[str, str] = {}

        def new_id(node: Node, name: str) -> str:
            candidate = self.controller.node_id(component_id, name, node.type)
            while candidate in used_ids:
                candidate = self.controller.node_id(component_id, name, node.type)
            used_ids.add(candidate)
            created_ids[node.id] = candidate
            return candidate

        def new_name(node: Node) -> str | None:
            return self._next_default_name(node.type, used_names) if self._is_default_name(node) else None

        return self._apply_tree(
            tr("ui.duplicate"), duplicate_nodes, selected, new_id, new_name,
            selected=lambda: tuple(created_ids[node_id] for node_id in selected),
        )

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

    def _resize_selected(self, node_id: str, width_mm: float, height_mm: float, anchor_x: float = 0.0, anchor_y: float = 0.0) -> None:
        self._apply_tree("Изменить размер элемента", resize_node, node_id, width_mm, height_mm, (anchor_x, anchor_y), selected=(node_id,))

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
        parent_id = None
        sibling_id = None
        before = False
        if target_id is not None:
            index = locations(self.session.documents[tab.component_id].model).get(target_id)
            if index is None:
                return
            if position == LayerTreeView.DropIndicatorPosition.OnItem and isinstance(index.node, GroupNode):
                parent_id = target_id
            else:
                parent_id = index.parent_id
                sibling_id = target_id
                before = position == LayerTreeView.DropIndicatorPosition.BelowItem
        self._apply_tree(
            "Перетащить слой",
            drop_nodes,
            selected,
            parent_id,
            sibling_id,
            before=before,
            selected=selected,
        )

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
        root = self.session.snapshot.root
        accepted = True
        if tool == "conditional_group":
            columns: tuple[str, ...] = ()
            if self.controller.preview is not None and self.active_tab is not None:
                try:
                    data = self.controller.preview.cached_data(self.session.snapshot, self.active_tab.component_id)
                    columns = data.headers if data is not None else ()
                except ProjectError:
                    pass
            if not columns:
                self._show_error(ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "сначала задайте лист Excel и проверьте его заголовки")))
                accepted = False
            else:
                name, accepted = QInputDialog.getText(self, tr("ui.conditional_group"), tr("ui.name"), text=self._next_default_name("conditional_group"))
                if accepted and name.strip():
                    component_id = self.active_tab.component_id
                    node_id = self.controller.node_id(component_id, name, "condition")
                    node = ConditionalGroupNode(id=node_id, type="conditional_group", name=name,
                        x_mm=x_mm, y_mm=y_mm, children=(), condition_column=columns[0], condition_value="")
                    self._apply_tree(tr("ui.add_conditional_group_action"), add_node, node, selected=(node_id,))
        elif tool == "image":
            columns = self._cached_columns()
            dialog = ImageAddDialog(root, self._import_image, self, columns=columns, images=self.session.snapshot.model.images)
            dialog.name.setText(self._next_default_name("image"))
            accepted = dialog.exec() == dialog.DialogCode.Accepted
            if accepted:
                width_mm = height_mm = None
                if dialog.content_mode.currentData() == "column":
                    width_mm, height_mm = self._initial_excel_image_size(dialog.content_column.currentText())
                    if width_mm is None:
                        accepted = False
                else:
                    image = QImage(str(root / dialog.source.text().strip()))
                    if image.isNull():
                        self._show_error(ProjectError(Diagnostic("IMAGE_SOURCE_INVALID", "", message_key="error.image_source_invalid")))
                        accepted = False
                    else:
                        width_mm, height_mm = image.width() * 25.4 / 96, image.height() * 25.4 / 96
                if accepted:
                    self.add_image(
                        dialog.source.text().strip(), x_mm, y_mm,
                        name=dialog.name.text().strip(), fit=dialog.fit.currentText(),
                        source_mode=dialog.content_mode.currentData(),
                        source_column=dialog.content_column.currentText() or None,
                        width_mm=width_mm, height_mm=height_mm,
                    )
        elif tool == "html":
            try:
                dialog = HtmlAddDialog(root, self._import_image, self, columns=self._cached_columns(), images=self.session.snapshot.model.images)
                dialog.name.setText(self._next_default_name("html"))
            except ProjectError as exc:
                self._show_error(exc)
                accepted = False
            else:
                accepted = dialog.exec() == dialog.DialogCode.Accepted
            if accepted:
                self.add_html(
                    dialog.html.toPlainText(), x_mm, y_mm,
                    name=dialog.name.text().strip(),
                    font_family=dialog.font.currentText(), font_size_pt=dialog.font_size.value(), color=dialog.color.text(),
                    content_mode=dialog.content_mode.currentData(),
                    content_column=dialog.content_column.currentText() or None,
                )
        if self.active_tab is not None:
            self.active_tab.canvas.cancel_tool()
        if not accepted:
            self.statusBar().showMessage("Добавление элемента отменено", 2000)

    def _tool_write_allowed(self, tool: str) -> bool:
        return tool != "html" or self._operation_allowed("resource_write")

    def _draw_element(self, tool: str, x1: float, y1: float, x2: float, y2: float) -> None:
        if self.session is None or self.active_tab is None:
            return
        component_id = self.active_tab.component_id
        if tool == "line":
            dx, dy = x2 - x1, y2 - y1
            if abs(dx) < 0.01 and abs(dy) < 0.01:
                return
            name = self._next_default_name("line")
            node_id = self.controller.node_id(component_id, name, "line")
            node = LineNode(id=node_id, type="line", name=name, x_mm=x1, y_mm=y1, dx_mm=dx, dy_mm=dy)
        elif tool in ("rectangle", "ellipse"):
            x, y = min(x1, x2), min(y1, y2)
            width, height = abs(x2 - x1), abs(y2 - y1)
            if min(width, height) < 0.01:
                return
            name = self._next_default_name(tool)
            node_id = self.controller.node_id(component_id, name, tool)
            node = ShapeNode(id=node_id, type=tool, name=name, x_mm=x, y_mm=y, width_mm=width, height_mm=height)
        else:
            return
        self._apply_tree(tr("ui.draw_element"), add_node, node, selected=(node_id,))

    def add_image(
        self,
        source: str,
        x_mm: float,
        y_mm: float,
        *,
        name: str | None = None,
        fit: str = "contain",
        source_mode: str = "manual",
        source_column: str | None = None,
        width_mm: float | None = None,
        height_mm: float | None = None,
        node_id: str | None = None,
    ) -> bool:
        if self.session is None or self.active_tab is None or not self._visual_editing_allowed():
            return False
        component = self.session.documents[self.active_tab.component_id].model
        name = name or self._next_default_name("image")
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "image")
        if width_mm is None or height_mm is None:
            width_mm = max(0.001, min(30.0, component.size_mm.width - x_mm))
            height_mm = max(0.001, min(30.0, component.size_mm.height - y_mm))
        try:
            node = ImageNode(id=node_id, name=name, type="image", x_mm=x_mm, y_mm=y_mm, width_mm=width_mm, height_mm=height_mm, source=source, source_mode=source_mode, source_column=source_column, fit=fit)
        except ValidationError as exc:
            self._show_error(exc)
            return False
        return self._apply_tree(tr("ui.image_add_title"), add_node, node, selected=(node_id,))

    def add_html(
        self,
        html: str,
        x_mm: float,
        y_mm: float,
        *,
        name: str | None = None,
        font_family: str = "Arial",
        font_size_pt: float = 10.0,
        color: str = "#111111",
        content_mode: str = "manual",
        content_column: str | None = None,
        node_id: str | None = None,
    ) -> bool:
        if self.session is None or self.active_tab is None or not self._visual_editing_allowed():
            return False
        component = self.session.documents[self.active_tab.component_id].model
        name = name or self._next_default_name("html")
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "html")
        width = max(0.001, min(40.0, component.size_mm.width - x_mm))
        height = max(0.001, min(15.0, component.size_mm.height - y_mm))
        try:
            node = HtmlNode(
                id=node_id, name=name, type="html", x_mm=x_mm, y_mm=y_mm,
                width_mm=width, height_mm=height, font_family=font_family,
                font_size_pt=font_size_pt, color=color, html=html,
                content_mode=content_mode, content_column=content_column,
            )
        except ValidationError as exc:
            self._show_error(exc)
            return False
        return self._apply_tree(tr("ui.html_text"), add_node, node, selected=(node_id,))

    def insert_html_image(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None or not self._user_editing_allowed():
            return
        found = locations(self.session.documents[self.active_tab.component_id].model)[ids[0]].node
        if not isinstance(found, HtmlNode):
            return
        picker = ResourcePickerDialog(self.session.snapshot.root, self._import_image, self, images=self.session.snapshot.model.images)
        if picker.exec() != picker.DialogCode.Accepted:
            return
        width, accepted = QInputDialog.getInt(self, tr("ui.image_size"), tr("ui.width_px"), 16, 1, 4096)
        if not accepted:
            return
        height, accepted = QInputDialog.getInt(self, tr("ui.image_size"), tr("ui.height_px"), 16, 1, 4096)
        if not accepted:
            return
        align, accepted = QInputDialog.getItem(self, tr("ui.align"), tr("ui.position"), ["", "middle", "top", "bottom"], 0, False)
        if not accepted:
            return
        self.element_properties.insert_html_image(picker.selected_path, width, height, align)
        self._edit_selected_node({"html": self.element_properties.html_edit.toPlainText()})

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
        self.statusBar().showMessage("Документ сохранён", 3000)
        self._update_actions()
        return True

    def save_all(self, *, _reservation: str | None = None) -> bool:
        reservation = _reservation or self._save_reservation_id
        if not self._user_editing_allowed(reservation):
            return False
        if self.session is None:
            return True
        return self._save_all_documents(reservation)

    def _save_all_for_reservation(self, reservation: str | None) -> bool:
        self._save_reservation_id = reservation
        try:
            return self.save_all()
        finally:
            self._save_reservation_id = None

    def _save_all_documents(self, reservation: str | None = None) -> bool:
        for component_id in tuple(self.session.documents):
            if not self._apply_draft(component_id, reservation=reservation):
                tab = self._tabs.get(component_id)
                if tab is not None:
                    self.tabs.setCurrentWidget(tab)
                    tab.set_mode("text")
                return False
        try:
            self.controller.save_all()
        except ProjectError as exc:
            self._show_error(exc)
            return False
        for component_id in self._tabs:
            self._update_tab_title(component_id)
        self._refresh_component_tree()
        self.statusBar().showMessage("Все документы сохранены", 3000)
        self._update_actions()
        return True

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
        name = self.session.documents[component_id].model.name
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
        box = QMessageBox(self)
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
        title = document.model.name + (" *" if document.dirty else "")
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
        return editing_actions(self._editing_context()).layout

    def _update_actions(self) -> None:
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
            tab.text_editor.setReadOnly(not actions.text if tab is self.active_tab else not self._user_editing_allowed())
            tab.layout_mode.setEnabled(self._user_editing_allowed())
            tab.text_mode.setEnabled(self._user_editing_allowed())
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
        self.new_action.setEnabled(idle)
        self.open_action.setEnabled(idle)

    def _show_error(self, error: Exception) -> None:
        if isinstance(error, ProjectError):
            message = format_error(error.diagnostic)
        else:
            incident_id = technical_log.log_exception("gui.operation", error, operation="dialog", status="failed")
            message = tr("error.unexpected", incident_id=incident_id)
        QMessageBox.critical(self, tr("error.title"), message)
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
            event.ignore()
            return False
        job = self._preview_coordinator.active
        if job is not None:
            assert self._preview_coordinator.phase is not None
            self._preview_coordinator.finish(job.request.identity.request, self._preview_coordinator.phase)
            self._release_preview_fonts(job)
        return True
