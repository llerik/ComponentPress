"""Main window widgets, menus, and panel construction."""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QTabWidget, QToolBar, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from componentpress.diagnostics import logging as technical_log
from componentpress.i18n import tr
from componentpress.user_preferences import save_preferences
from componentpress.app_assets import set_themed_icon
from .copy_mode_switch import CopyModeSwitch
from .program_settings_dialog import ProgramSettingsDialog
from .properties import ComponentProperties, ElementProperties
from .qt_models import LayerTreeView
from .theme import apply_theme


class MainWindowShell:
    """Own construction of the main window shell and its menus."""


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
        self._create_file_actions()
        self._create_edit_actions()
        self._create_project_actions()
        self._create_view_actions()
        self._create_build_actions()
        self._create_help_actions()
        self._build_menus()

    def _create_file_actions(self) -> None:
        self.new_action = self._action(tr("action.new"), QKeySequence.StandardKey.New)
        self.open_action = self._action(tr("action.open"), QKeySequence.StandardKey.Open)
        self.save_action = self._action(tr("action.save"), QKeySequence.StandardKey.Save)
        self.save_all_action = self._action(tr("action.save_all"), "Ctrl+Shift+S")
        self.close_tab_action = self._action(tr("action.close_tab"), QKeySequence.StandardKey.Close)
        self.exit_action = self._action(tr("action.exit"), QKeySequence.StandardKey.Quit)
        self.recent_projects_menu = QMenu(tr("menu.recent_projects"), self)
        self.import_project_archive_action = self._action(tr("action.import_archive"))
        self.export_project_archive_action = self._action(tr("action.export_archive"))

    def _create_edit_actions(self) -> None:
        self.undo_action = self._action(tr("action.undo"), QKeySequence.StandardKey.Undo)
        self.redo_action = self._action(tr("action.redo"), QKeySequence.StandardKey.Redo)
        self.delete_action = self._action(tr("action.delete"), QKeySequence.StandardKey.Delete)
        self.group_action = self._action(tr("action.group"), "Ctrl+G")
        self.ungroup_action = self._action(tr("action.ungroup"), "Ctrl+Shift+G")

    def _create_project_actions(self) -> None:
        self.add_component_action = self._action(tr("action.add_component"), "Ctrl+Shift+N")
        self.delete_component_action = self._action(tr("action.delete_component"))
        self.project_settings_action = self._action(tr("action.project_settings"))
        self.image_manager_action = self._action(tr("ui.image_manager"))
        self.export_png_zip_action = self._action(tr("action.export_png"))

    def _create_view_actions(self) -> None:
        self.zoom_in_action = self._action(tr("action.zoom_in"), QKeySequence.StandardKey.ZoomIn)
        self.zoom_out_action = self._action(tr("action.zoom_out"), QKeySequence.StandardKey.ZoomOut)
        self.fit_action = self._action(tr("action.fit"), "Ctrl+0")
        self.actual_size_action = self._action(tr("ui.zoom_actual"), "Ctrl+1")
        self.addAction(self.actual_size_action)

    def _create_build_actions(self) -> None:
        self.build_all_action = self._action(tr("action.build_all"), "Ctrl+B")
        self.build_active_action = self._action(tr("action.build_active"), "Ctrl+Shift+B")
        self.cancel_build_action = self._action(tr("action.cancel_build"))
        self.open_build_action = self._action(tr("action.open_result"))
        self.open_pdf_action = self._action(tr("action.open_pdf"))
        self.validate_active_action = self._action(tr("action.validate_active"))
        self.validate_all_action = self._action(tr("action.validate_all"))

    def _create_help_actions(self) -> None:
        self.program_settings_action = self._action(tr("action.program_settings"))
        self.about_action = self._action(tr("menu.about"))
        self.help_action = self._action(tr("help.open"), QKeySequence("F1"))
        self.hotkeys_action = self._action(tr("help.hotkeys.open"))
        self.error_report_action = self._action(tr("about.create_report"))

    def _build_menus(self) -> None:
        self._build_file_menu()
        self._build_edit_menu()
        self._build_project_menu()
        self._build_view_menu()
        self._build_build_menu()
        self._build_help_menus()

    def _build_file_menu(self) -> None:
        self.file_menu = self.menuBar().addMenu(tr("menu.file"))
        self.file_menu.addActions([self.new_action, self.open_action])
        self.file_menu.addMenu(self.recent_projects_menu)
        self.file_project_separator = self.file_menu.addSeparator()
        self.file_menu.addActions([self.save_action, self.save_all_action, self.close_tab_action])
        self.file_exit_separator = self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)

    def _build_edit_menu(self) -> None:
        self.edit_menu = self.menuBar().addMenu(tr("menu.edit"))
        self.edit_menu.addActions([self.undo_action, self.redo_action])
        self.edit_menu.addSeparator()
        self.edit_menu.addActions([self.delete_action, self.group_action, self.ungroup_action])

    def _build_project_menu(self) -> None:
        self.project_menu = self.menuBar().addMenu(tr("menu.project"))
        self.project_menu.addActions([self.add_component_action, self.delete_component_action, self.image_manager_action])
        self.project_archive_separator = self.project_menu.addSeparator()
        self.project_menu.addActions([self.export_project_archive_action, self.import_project_archive_action])

    def _build_view_menu(self) -> None:
        self.view_menu = self.menuBar().addMenu(tr("menu.view"))
        self.view_menu.addActions([self.zoom_in_action, self.zoom_out_action, self.fit_action, self.actual_size_action])
        self.view_menu.addSeparator()
        self.windows_menu = self.view_menu.addMenu(tr("menu.windows"))

    def _build_build_menu(self) -> None:
        self.build_menu = self.menuBar().addMenu(tr("menu.build"))
        self.build_menu.addActions([self.validate_all_action, self.validate_active_action, self.export_png_zip_action])
        self.build_menu.addSeparator()
        self.build_menu.addActions([self.build_all_action, self.build_active_action, self.cancel_build_action,
                                    self.open_build_action, self.open_pdf_action])

    def _build_help_menus(self) -> None:
        self.settings_menu = self.menuBar().addMenu(tr("menu.settings"))
        self.settings_menu.addActions([self.project_settings_action, self.program_settings_action])
        self.help_menu = self.menuBar().addMenu(tr("menu.help"))
        self.help_menu.addActions([self.help_action, self.hotkeys_action, self.error_report_action])
        self.help_menu.addSeparator()
        self.help_menu.addAction(self.about_action)


    def _show_program_settings(self, parent=None) -> bool:
        dialog = ProgramSettingsDialog(self._program_preferences, lambda: self._register_file_type(parent), parent or self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        try:
            preferences = dialog.preferences().model_copy(
                update={"recent_projects": self._program_preferences.recent_projects}
            )
        except (ValueError, TypeError):
            return self._reject_program_preferences(parent)
        return self._save_program_preferences(preferences, parent)

    def _reject_program_preferences(self, parent) -> bool:
        QMessageBox.warning(parent or self, tr("ui.program_settings"), tr("error.program_settings_save"))
        technical_log.log_event("application.settings_save", operation="program_settings",
                                phase="preparation", status="failed", code="PROGRAM_SETTINGS_INVALID")
        return False

    def _save_program_preferences(self, preferences, parent) -> bool:
        operation_id = uuid4().hex
        technical_log.log_event("application.settings_save", operation="program_settings",
                                operation_id=operation_id, phase="publishing", status="started")
        if not save_preferences(preferences):
            QMessageBox.warning(parent or self, tr("ui.program_settings"), tr("error.program_settings_save"))
            technical_log.log_event("application.settings_save", operation="program_settings",
                                    operation_id=operation_id, phase="finished", status="failed",
                                    code="PROGRAM_SETTINGS_WRITE")
            return False
        self._program_preferences = preferences
        self._theme = preferences.theme
        self._refresh_recent_projects_menu()
        apply_theme(preferences.theme)
        technical_log.log_event("application.settings_save", operation="program_settings",
                                operation_id=operation_id, phase="finished", status="succeeded")
        return True


    def _build_ui(self) -> None:
        self._build_document_tabs()
        self._build_docks()
        self._build_quick_toolbar()
        self._build_status_bar()
        self._install_panel_event_filters()

    def _build_document_tabs(self) -> None:
        self.data_mode = CopyModeSwitch()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("documentTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(0)
        self.setCentralWidget(self.tabs)

    def _build_docks(self) -> None:
        self.right_panel = self._right_panel()
        self.right_dock = self._create_dock("rightDock", "panel.workspace", Qt.DockWidgetArea.RightDockWidgetArea, self.right_panel)
        self.left_panel = self._left_panel()
        self.events_dock = self._create_dock("eventsDock", "panel.events", Qt.DockWidgetArea.BottomDockWidgetArea, self._events_panel())
        self.docks = (self.right_dock, self.events_dock)
        self.resizeDocks([self.right_dock], [300], Qt.Orientation.Horizontal)
        self._saved_dock_visibility = {dock.objectName(): True for dock in self.docks}
        self._project_ui_visible = False
        for dock, area in zip(self.docks, (Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea)):
            self._add_dock_menu_action(dock, area)

    def _create_dock(self, name: str, title: str, area, panel: QWidget) -> QDockWidget:
        dock = QDockWidget(tr(title), self)
        dock.setObjectName(name)
        dock.setWidget(panel)
        self.addDockWidget(area, dock)
        return dock

    def _add_dock_menu_action(self, dock: QDockWidget, area) -> None:
        action = dock.toggleViewAction()
        self.windows_menu.addAction(action)
        action.triggered.connect(lambda visible: self._dock_visibility_requested(dock, area, visible))

    def _build_quick_toolbar(self) -> None:
        self.quick_toolbar = QToolBar(tr("ui.quick_actions"), self)
        self.quick_toolbar.setObjectName("quickActions")
        self.quick_toolbar.setMovable(False)
        self.quick_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.quick_toolbar)
        self._add_mode_controls()
        self._add_quick_actions()
        self._add_build_controls()
        self._style_quick_actions()
        self._update_build_toolbar()
        self._build_icon_toolbar()

    def _add_mode_controls(self) -> None:
        self.quick_toolbar.addWidget(self.data_mode)
        self.mode_info_button = QToolButton(self.quick_toolbar)
        set_themed_icon(self.mode_info_button, "info.png")
        self.mode_info_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.mode_info_button.setToolTip("")
        self.mode_info_button.setAccessibleName(tr("ui.copy_mode_info_tip"))
        self.mode_info_button.clicked.connect(self._show_copy_mode_tooltip)
        self.mode_info_button.installEventFilter(self)
        self.quick_toolbar.addWidget(self.mode_info_button)
        self.quick_toolbar.addSeparator()

    def _add_quick_actions(self) -> None:
        actions = (self.add_component_action, self.project_settings_action, self.save_action, self.undo_action, self.redo_action)
        self.quick_toolbar.addActions(list(actions))
        self.quick_toolbar.addSeparator()
        self.validate_all_button = self._toolbar_button(self.validate_all_action, "validate.png")
        self.quick_toolbar.addWidget(self.validate_all_button)

    def _add_build_controls(self) -> None:
        self.build_active_button = self._toolbar_button(self.build_active_action, "build.png")
        self.build_all_button = self._toolbar_button(self.build_all_action, "build_all.png")
        self.cancel_build_button = self._toolbar_button(self.cancel_build_action, "stop.png")
        self.build_active_widget_action = self.quick_toolbar.addWidget(self.build_active_button)
        self.build_all_widget_action = self.quick_toolbar.addWidget(self.build_all_button)
        self.cancel_build_widget_action = self.quick_toolbar.addWidget(self.cancel_build_button)
        self.cancel_build_widget_action.setVisible(False)

    def _style_quick_actions(self) -> None:
        icons = {self.add_component_action: "add.png", self.project_settings_action: "settings.png",
                 self.save_action: "save.png", self.undo_action: "undo.png", self.redo_action: "redo.png"}
        for action, filename in icons.items():
            set_themed_icon(action, filename)
            self._set_action_text(action, action.text())
        self._set_action_text(self.validate_all_action, tr("action.validate_all"))
        self.validate_all_button.setToolTip(self.validate_all_action.text())
        self.validate_all_button.setAccessibleName(self.validate_all_action.text())

    def _build_status_bar(self) -> None:
        self.status_project = QLabel(tr("ui.project_closed"))
        self.status_event = QLabel("")
        self.status_event.setObjectName("lastEventStatus")
        self.status_event.setMaximumWidth(420)
        self.status_zoom = QLabel("")
        self.statusBar().addWidget(self.status_project, 1)
        self.statusBar().addPermanentWidget(self.status_event)
        self.statusBar().addPermanentWidget(self.status_zoom)
        self._update_project_visibility()
        self._refresh_recent_projects_menu()

    def _install_panel_event_filters(self) -> None:
        self.right_panel.installEventFilter(self)
        for child in self.right_panel.findChildren(QWidget):
            child.installEventFilter(self)


    def _left_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.hide()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(3, 5, 3, 5)
        data = QGroupBox(tr("ui.excel_data"))
        self._build_data_controls(QVBoxLayout(data))
        self.compat_data_panel = data
        data.hide()
        self._create_drawing_tools()
        self._style_drawing_tools(layout)
        return panel

    def _build_data_controls(self, layout: QVBoxLayout) -> None:
        self._create_data_selectors(layout)
        self._create_data_actions(layout)

    def _create_data_selectors(self, layout: QVBoxLayout) -> None:
        self.data_source = self._data_combo("dataSource")
        self.data_sheet = self._data_combo("dataSheet")
        self.data_row = self._data_combo("dataRow")
        self.data_column = self._data_combo("dataColumn")
        self.data_chain = QLabel(tr("ui.no_binding"))
        self.data_chain.setObjectName("dataChain")
        self.data_chain.setWordWrap(True)
        labels = (tr("ui.project_source"), tr("ui.component_sheet"), tr("ui.row"), tr("ui.column"))
        for label, widget in zip(labels, (self.data_source, self.data_sheet, self.data_row, self.data_column)):
            layout.addWidget(QLabel(label))
            layout.addWidget(widget)
        layout.addWidget(self.data_chain)

    @staticmethod
    def _data_combo(name: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(name)
        return combo

    def _create_data_actions(self, layout: QVBoxLayout) -> None:
        buttons = ("data_import", "data_refresh", "data_insert", "data_export", "data_export_zip")
        labels = ("ui.import_xlsx", "ui.refresh", "ui.insert_column", "ui.export_png", "ui.png_zip")
        for name, label in zip(buttons, labels):
            setattr(self, name, QPushButton(tr(label, column=tr("ui.column")) if name == "data_insert" else tr(label)))
        self.data_validate = QPushButton(tr("ui.validate"))
        self.data_validate_all = QPushButton(tr("ui.validate_all"))
        self.data_clear = QPushButton(tr("ui.remove_xlsx"))
        row = QHBoxLayout()
        for button in (self.data_import, self.data_refresh, self.data_insert, self.data_export, self.data_export_zip):
            row.addWidget(button)
        layout.addLayout(row)
        self._add_validation_actions(layout)

    def _add_validation_actions(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        for button in (self.data_validate, self.data_validate_all, self.data_clear):
            row.addWidget(button)
        layout.addLayout(row)

    def _create_drawing_tools(self) -> None:
        keys = ("select", "image", "html", "conditional_group", "rectangle", "ellipse", "line")
        labels = ("ui.select", "ui.image", "ui.html_text", "ui.conditional_group", "ui.rectangle", "ui.ellipse", "ui.line")
        icons = ("cursor.png", "image.png", "text.png", "condition.png", "rectangle.png", "ellipse.png", "line.png")
        self.drawing_tools = {key: QPushButton(tr(label)) for key, label in zip(keys, labels)}
        for key, button in self.drawing_tools.items():
            button.setObjectName({"conditional_group": "conditionalGroupTool"}.get(key, f"{key}Tool"))
        self.select_tool, self.image_tool, self.html_tool = (self.drawing_tools[key] for key in keys[:3])
        self.conditional_group_tool, self.rectangle_tool = (self.drawing_tools[key] for key in keys[3:5])
        self.ellipse_tool, self.line_tool = (self.drawing_tools[key] for key in keys[5:])
        self._drawing_tool_icons = dict(zip(self.drawing_tools.values(), icons))

    def _style_drawing_tools(self, layout: QVBoxLayout) -> None:
        for button, icon in self._drawing_tool_icons.items():
            button.setCheckable(True)
            button.setEnabled(False)
            self._style_icon_button(button, icon)
        self._style_layer_tools()
        layout.addStretch(1)

    @staticmethod
    def _style_icon_button(button: QPushButton, icon: str) -> None:
        button.setToolTip(button.text())
        button.setAccessibleName(button.text())
        set_themed_icon(button, icon)
        button.setText("")
        button.setFixedSize(34, 34)

    def _style_layer_tools(self) -> None:
        for button, icon in ((self.layer_up, "up.png"), (self.layer_down, "down.png"),
                             (self.group_button, "group.png"), (self.ungroup_button, "ungroup.png")):
            self._style_icon_button(button, icon)
            button.setIconSize(QSize(22, 22))


    def _events_panel(self) -> QWidget:
        panel = QWidget()
        events_layout = QVBoxLayout(panel)
        self._create_event_controls()
        self._connect_event_controls()
        events_layout.addWidget(self.event_search)
        events_layout.addWidget(self.event_list, 1)
        events_layout.addLayout(self._event_options())
        return panel

    def _create_event_controls(self) -> None:
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText(tr("event.search"))
        self.event_list = QListWidget()
        self.hide_event_project_path = QCheckBox(tr("event.hide_project_path"))
        self.hide_event_project_path.setObjectName("hideEventProjectPath")
        self.hide_event_project_path.setChecked(True)
        self.copy_event = QToolButton()
        self.copy_event.setObjectName("copyEvent")
        self.copy_event.setToolTip(tr("event.copy"))
        self.copy_event.setAccessibleName(tr("event.copy"))
        set_themed_icon(self.copy_event, "copy.png")
        self.copy_event.setEnabled(False)
        self.clear_events = QPushButton(tr("event.clear"))
        set_themed_icon(self.clear_events, "delete.png")

    def _connect_event_controls(self) -> None:
        self.clear_events.clicked.connect(self._clear_events)
        self.copy_event.clicked.connect(self._copy_selected_event)
        self.hide_event_project_path.toggled.connect(self._format_event_items)
        self.event_list.currentItemChanged.connect(lambda *_: self._update_copy_event_enabled())

    def _event_options(self) -> QHBoxLayout:
        options = QHBoxLayout()
        options.addWidget(self.hide_event_project_path)
        options.addWidget(self.copy_event)
        options.addStretch(1)
        options.addWidget(self.clear_events)
        return options


    def _right_panel(self) -> QWidget:
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("rightPanelTabs")
        self.right_tabs.addTab(self._component_tab(), tr("panel.components"))
        self.right_tabs.addTab(self._element_tab(), tr("panel.elements"))
        properties = self._property_panel()
        return self._property_splitter(properties)

    def _component_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self._add_component_header(layout)
        self.component_tree = QTreeView()
        self.component_tree.setObjectName("componentTree")
        self.component_tree.setHeaderHidden(True)
        self.component_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.component_tree.customContextMenuRequested.connect(self._component_context_menu)
        layout.addWidget(self.component_tree)
        return panel

    def _add_component_header(self, layout: QVBoxLayout) -> None:
        header = QHBoxLayout()
        header.addWidget(QLabel(tr("panel.components")), 1)
        self.add_component_button = QToolButton()
        self.add_component_button.setToolTip(tr("ui.add_component"))
        self.add_component_button.setAccessibleName(tr("ui.add_component"))
        set_themed_icon(self.add_component_button, "add.png")
        self.add_component_button.clicked.connect(self.add_component_dialog)
        header.addWidget(self.add_component_button)
        layout.addLayout(header)

    def _element_tab(self) -> QWidget:
        panel = QGroupBox(tr("panel.element_order"))
        layout = QVBoxLayout(panel)
        self.element_tree = LayerTreeView()
        self.element_tree.setObjectName("elementTree")
        self.element_tree.nodeContextMenuRequested.connect(self._element_tree_context_menu)
        layout.addWidget(self.element_tree)
        self._create_layer_buttons()
        return panel

    def _create_layer_buttons(self) -> None:
        self.layer_up = QPushButton(tr("ui.layer_up"))
        self.layer_up.setObjectName("layerUp")
        self.layer_down = QPushButton(tr("ui.layer_down"))
        self.layer_down.setObjectName("layerDown")
        self.group_button = QPushButton(tr("ui.group_create"))
        self.ungroup_button = QPushButton(tr("ui.ungroup"))

    def _property_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self._build_multi_properties()
        self._build_component_properties()
        self._build_element_properties()
        for group in (self.multi_properties_group, self.component_properties_group, self.element_properties_group):
            layout.addWidget(group)
        layout.addStretch(1)
        return panel

    def _build_multi_properties(self) -> None:
        self.multi_properties_group = QGroupBox(tr("ui.align_group"))
        layout = QHBoxLayout(self.multi_properties_group)
        self.align_buttons = {}
        icons = {
            "left": "align_horizontal_left.png", "right": "align_horizontal_right.png",
            "top": "align_vertical_top.png", "bottom": "align_vertical_bottom.png",
        }
        for edge, icon in icons.items():
            layout.addWidget(self._alignment_button(edge, icon))
        self.multi_properties_group.hide()

    def _alignment_button(self, edge, icon):
        label = tr(f"ui.align_{edge}")
        button = QPushButton()
        button.setObjectName(f"align{edge.title()}")
        button.setFixedSize(34, 34)
        button.setIconSize(QSize(22, 22))
        button.setToolTip(label)
        button.setAccessibleName(label)
        set_themed_icon(button, icon)
        button.clicked.connect(lambda _checked=False: self._align_selected(edge))
        self.align_buttons[edge] = button
        return button

    def _build_component_properties(self) -> None:
        self.component_properties_group = QGroupBox(tr("ui.component_properties"))
        layout = QVBoxLayout(self.component_properties_group)
        self.properties = ComponentProperties()
        self.properties.sheetChanged.connect(self._component_property_sheet_changed)
        layout.addWidget(self.properties)

    def _build_element_properties(self) -> None:
        self.element_properties_group = QGroupBox(tr("ui.element_properties"))
        layout = QVBoxLayout(self.element_properties_group)
        self.element_properties = ElementProperties()
        layout.addWidget(self.element_properties)
        self.element_properties_group.hide()

    def _property_splitter(self, panel: QWidget) -> QSplitter:
        self.properties_scroll = QScrollArea()
        self.properties_scroll.setObjectName("propertiesScroll")
        self.properties_scroll.setWidgetResizable(True)
        self.properties_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.properties_scroll.setWidget(panel)
        self.properties_scroll.setMinimumHeight(100)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("elementsPropertiesSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.right_tabs)
        splitter.addWidget(self.properties_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([480, 320])
        splitter.setMinimumWidth(180)
        self.right_splitter = splitter
        return splitter


    def _update_project_visibility(self) -> None:
        has_project = self.session is not None
        self._set_project_menu_visibility(has_project)
        self._set_project_dock_visibility(has_project)
        self._set_project_action_visibility(has_project)
        self.recent_projects_menu.menuAction().setVisible(bool(self._program_preferences.recent_projects))

    def _set_project_menu_visibility(self, visible: bool) -> None:
        for menu in (self.edit_menu, self.view_menu, self.build_menu):
            menu.menuAction().setVisible(visible)
        self.quick_toolbar.setVisible(visible)
        self.icon_toolbar.setVisible(visible)
        self.windows_menu.menuAction().setEnabled(visible)
        for dock in self.docks:
            dock.toggleViewAction().setEnabled(visible)
        for separator in (self.file_project_separator, self.file_exit_separator, self.project_archive_separator):
            separator.setVisible(visible)

    def _set_project_dock_visibility(self, visible: bool) -> None:
        if visible == self._project_ui_visible:
            return
        self._project_ui_visible = visible
        for dock in self.docks:
            if visible:
                dock.setVisible(self._saved_dock_visibility.get(dock.objectName(), True))
            else:
                dock.hide()

    def _set_project_action_visibility(self, visible: bool) -> None:
        actions = (self.save_action, self.save_all_action, self.close_tab_action, self.add_component_action,
                   self.delete_component_action, self.image_manager_action, self.export_project_archive_action,
                   self.export_png_zip_action, self.project_settings_action)
        for action in actions:
            action.setVisible(visible)
