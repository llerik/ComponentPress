"""Stage-five main window with synchronized visual and YAML editing."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, Qt
from PySide6.QtGui import QAction, QColor, QCloseEvent, QKeySequence, QUndoGroup
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QSplitter,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from componentpress.application.sessions import ProjectSession
from componentpress.application.contracts import BuildRequest, BuildResult, BuildProgress
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.component import DataBinding
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode
from componentpress.platforms.services import PlatformServices
from componentpress.domain.tree import (
    TreeOperationError,
    add_node,
    group_nodes,
    locations,
    move_nodes,
    place_nodes,
    remove_nodes,
    reorder_nodes,
    reparent_nodes,
    resize_node,
    ungroup_node,
    update_node,
)

from .controllers import ProjectController
from .commands import ReplaceComponentCommand
from .dialogs import HtmlAddDialog, ImageAddDialog, ResourcePickerDialog
from .build_dialog import BuildOptionsDialog
from .document_tab import DocumentTab
from .properties import ComponentProperties, ElementProperties
from .qt_models import ID_ROLE, LayerTreeView, component_model, element_model


class MainWindow(QMainWindow):
    def __init__(self, controller: ProjectController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._tabs: dict[str, DocumentTab] = {}
        self._selection_sync = False
        self._active_build_job: str | None = None
        self._last_build_result: BuildResult | None = None
        self._build_progress: QProgressDialog | None = None
        self._close_after_build = False
        self.undo_group = QUndoGroup(self)
        self.setObjectName("mainWindow")
        self.setWindowTitle("ComponentPress")
        self.resize(1280, 800)
        self._build_actions()
        self._build_ui()
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

    def _build_actions(self) -> None:
        self.new_action = self._action("Новый проект…", QKeySequence.StandardKey.New)
        self.open_action = self._action("Открыть проект…", QKeySequence.StandardKey.Open)
        self.save_action = self._action("Сохранить документ", QKeySequence.StandardKey.Save)
        self.save_all_action = self._action("Сохранить всё", "Ctrl+Shift+S")
        self.close_tab_action = self._action("Закрыть вкладку", QKeySequence.StandardKey.Close)
        self.exit_action = self._action("Выход", QKeySequence.StandardKey.Quit)
        self.add_component_action = self._action("Добавить компонент…", "Ctrl+Shift+N")
        self.undo_action = self.undo_group.createUndoAction(self, "Отменить")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.redo_action = self.undo_group.createRedoAction(self, "Повторить")
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.delete_action = self._action("Удалить элементы", QKeySequence.StandardKey.Delete)
        self.group_action = self._action("Сгруппировать", "Ctrl+G")
        self.ungroup_action = self._action("Разгруппировать", "Ctrl+Shift+G")
        self.zoom_in_action = self._action("Увеличить", QKeySequence.StandardKey.ZoomIn)
        self.zoom_out_action = self._action("Уменьшить", QKeySequence.StandardKey.ZoomOut)
        self.fit_action = self._action("По размеру окна", "Ctrl+0")
        self.build_all_action = self._action("Собрать всё…", "Ctrl+B")
        self.build_active_action = self._action("Собрать активный компонент…", "Ctrl+Shift+B")
        self.cancel_build_action = self._action("Отменить сборку")
        self.open_build_action = self._action("Открыть папку результата")
        self.open_pdf_action = self._action("Открыть печатный PDF")
        file_menu = self.menuBar().addMenu("Файл")
        file_menu.addActions([self.new_action, self.open_action])
        file_menu.addSeparator()
        file_menu.addActions([self.save_action, self.save_all_action, self.close_tab_action])
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)
        edit_menu = self.menuBar().addMenu("Правка")
        edit_menu.addActions([self.undo_action, self.redo_action])
        edit_menu.addSeparator()
        edit_menu.addActions([self.delete_action, self.group_action, self.ungroup_action])
        project_menu = self.menuBar().addMenu("Проект")
        project_menu.addAction(self.add_component_action)
        build_menu = self.menuBar().addMenu("Сборка")
        build_menu.addActions([
            self.build_all_action,
            self.build_active_action,
            self.cancel_build_action,
            self.open_build_action,
            self.open_pdf_action,
        ])
        view_menu = self.menuBar().addMenu("Вид")
        view_menu.addActions([self.zoom_in_action, self.zoom_out_action, self.fit_action])

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        self.left_panel = self._left_panel()
        splitter.addWidget(self.left_panel)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("documentTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(500)
        splitter.addWidget(self.tabs)
        self.right_panel = self._right_panel()
        splitter.addWidget(self.right_panel)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 800, 280])
        self.setCentralWidget(splitter)
        self.status_project = QLabel("Проект не открыт")
        self.status_zoom = QLabel("")
        self.statusBar().addWidget(self.status_project, 1)
        self.statusBar().addPermanentWidget(self.status_zoom)
        for panel in (self.left_panel, self.right_panel):
            panel.installEventFilter(self)
            for child in panel.findChildren(QWidget):
                child.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            tab = getattr(self, "active_tab", None)
            if tab is not None and tab.canvas.active_tool is not None:
                tab.canvas.cancel_tool()
                return True
        return super().eventFilter(watched, event)

    def _left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        components = QGroupBox("Компоненты")
        component_layout = QVBoxLayout(components)
        self.component_tree = QTreeView()
        self.component_tree.setObjectName("componentTree")
        self.component_tree.setHeaderHidden(False)
        component_layout.addWidget(self.component_tree)
        layout.addWidget(components, 1)
        data = QGroupBox("Данные Excel")
        data_layout = QVBoxLayout(data)
        self.data_source = QComboBox()
        self.data_source.setObjectName("dataSource")
        self.data_sheet = QComboBox()
        self.data_sheet.setObjectName("dataSheet")
        self.data_id_column = QComboBox()
        self.data_id_column.setObjectName("dataIdColumn")
        self.data_copies_column = QComboBox()
        self.data_copies_column.setObjectName("dataCopiesColumn")
        self.data_row = QComboBox()
        self.data_row.setObjectName("dataRow")
        self.data_column = QComboBox()
        self.data_column.setObjectName("dataColumn")
        self.data_chain = QLabel("Нет привязки")
        self.data_chain.setObjectName("dataChain")
        self.data_chain.setWordWrap(True)
        for label, widget in (
            ("Источник", self.data_source), ("Лист", self.data_sheet),
            ("ID", self.data_id_column), ("Тираж", self.data_copies_column),
            ("Строка", self.data_row), ("Столбец", self.data_column),
        ):
            data_layout.addWidget(QLabel(label))
            data_layout.addWidget(widget)
        data_layout.addWidget(self.data_chain)
        data_buttons = QHBoxLayout()
        self.data_import = QPushButton("Импорт XLSX…")
        self.data_refresh = QPushButton("Обновить")
        self.data_insert = QPushButton("Вставить {Столбец}")
        self.data_export = QPushButton("Экспорт PNG…")
        for button in (self.data_import, self.data_refresh, self.data_insert, self.data_export):
            data_buttons.addWidget(button)
        data_layout.addLayout(data_buttons)
        layout.addWidget(data)
        tools = QGroupBox("Инструменты")
        tools_layout = QVBoxLayout(tools)
        self.image_tool = QPushButton("Изображение")
        self.image_tool.setObjectName("imageTool")
        self.html_tool = QPushButton("HTML-текст")
        self.html_tool.setObjectName("htmlTool")
        for button in (self.image_tool, self.html_tool):
            button.setCheckable(True)
            button.setEnabled(False)
            tools_layout.addWidget(button)
        layout.addWidget(tools)
        return panel

    def _right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        elements = QGroupBox("Элементы (передний план сверху)")
        element_layout = QVBoxLayout(elements)
        self.element_tree = LayerTreeView()
        self.element_tree.setObjectName("elementTree")
        element_layout.addWidget(self.element_tree)
        order_buttons = QHBoxLayout()
        self.layer_up = QPushButton("Вверх")
        self.layer_up.setObjectName("layerUp")
        self.layer_down = QPushButton("Вниз")
        self.layer_down.setObjectName("layerDown")
        self.group_button = QPushButton("Создать группу")
        self.ungroup_button = QPushButton("Разгруппировать")
        self.reparent_button = QPushButton("Переместить…")
        for button in (self.layer_up, self.layer_down, self.group_button, self.ungroup_button, self.reparent_button):
            order_buttons.addWidget(button)
        element_layout.addLayout(order_buttons)
        layout.addWidget(elements, 2)
        self.component_properties_group = QGroupBox("Свойства компонента")
        property_layout = QVBoxLayout(self.component_properties_group)
        self.properties = ComponentProperties()
        property_layout.addWidget(self.properties)
        layout.addWidget(self.component_properties_group, 1)
        self.element_properties_group = QGroupBox("Свойства элемента")
        element_property_layout = QVBoxLayout(self.element_properties_group)
        self.element_properties = ElementProperties()
        element_property_layout.addWidget(self.element_properties)
        self.element_properties_group.hide()
        layout.addWidget(self.element_properties_group, 2)
        return panel

    def _connect_signals(self) -> None:
        self.new_action.triggered.connect(self.new_project_dialog)
        self.open_action.triggered.connect(self.open_project_dialog)
        self.save_action.triggered.connect(self.save_active)
        self.save_all_action.triggered.connect(self.save_all)
        self.close_tab_action.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        self.exit_action.triggered.connect(self.close)
        self.add_component_action.triggered.connect(self.add_component_dialog)
        self.build_all_action.triggered.connect(lambda: self._start_build(False))
        self.build_active_action.triggered.connect(lambda: self._start_build(True))
        self.cancel_build_action.triggered.connect(self._cancel_build)
        self.open_build_action.triggered.connect(lambda: self._open_build_result(False))
        self.open_pdf_action.triggered.connect(lambda: self._open_build_result(True))
        self.undo_action.triggered.connect(self._after_undo_redo)
        self.redo_action.triggered.connect(self._after_undo_redo)
        self.delete_action.triggered.connect(self.delete_selected)
        self.group_action.triggered.connect(lambda: self.group_selected())
        self.ungroup_action.triggered.connect(lambda: self.ungroup_selected())
        self.zoom_in_action.triggered.connect(lambda: self._zoom_active(1.2))
        self.zoom_out_action.triggered.connect(lambda: self._zoom_active(1 / 1.2))
        self.fit_action.triggered.connect(lambda: self.active_tab and self.active_tab.canvas.fit_page())
        self.component_tree.doubleClicked.connect(self._open_component_index)
        self.tabs.currentChanged.connect(self._active_tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.properties.nameChanged.connect(lambda value: self._edit_active(name=value))
        self.properties.sizeChanged.connect(self._edit_active_size)
        self.properties.backgroundChanged.connect(lambda value: self._edit_active(background=value))
        self.image_tool.clicked.connect(lambda: self._begin_tool("image"))
        self.html_tool.clicked.connect(lambda: self._begin_tool("html"))
        self.layer_up.clicked.connect(lambda: self.reorder_selected(1))
        self.layer_down.clicked.connect(lambda: self.reorder_selected(-1))
        self.group_button.clicked.connect(lambda: self.group_selected())
        self.ungroup_button.clicked.connect(lambda: self.ungroup_selected())
        self.reparent_button.clicked.connect(self.reparent_selected_dialog)
        self.element_tree.layerDropRequested.connect(self._layer_drop)
        self.element_properties.nodeChanged.connect(self._edit_selected_node)
        self.element_properties.insertImageRequested.connect(self.insert_html_image)
        self.data_import.clicked.connect(self._import_xlsx)
        self.data_refresh.clicked.connect(lambda: self._refresh_data(force=True))
        self.data_export.clicked.connect(self._export_instance)
        self.data_insert.clicked.connect(self._insert_column_binding)
        self.data_row.currentIndexChanged.connect(self._row_changed)
        self.data_source.currentIndexChanged.connect(self._source_changed)
        self.data_sheet.currentIndexChanged.connect(self._data_binding_changed)
        self.data_id_column.currentIndexChanged.connect(self._data_binding_changed)
        self.data_copies_column.currentIndexChanged.connect(self._data_binding_changed)
        self.data_column.currentIndexChanged.connect(self._update_data_chain)

    def load_session(self, session: ProjectSession) -> None:
        """Install an already opened session; useful to bootstrap and to test the UI."""
        self._clear_tabs(discard=True)
        self.controller.set_session(session)
        self._refresh_component_tree()
        self.setWindowTitle(f"{session.snapshot.model.name} — ComponentPress")
        self.status_project.setText(str(session.snapshot.root))
        if self.controller.recovery_diagnostics:
            details = "; ".join(item.message for item in self.controller.recovery_diagnostics)
            self.statusBar().showMessage(f"Восстановление сборок: {details}", 15000)
        self._update_actions()

    def open_path(self, root: Path) -> bool:
        if not self._confirm_all_dirty():
            return False
        try:
            session = self.controller.open(root)
        except ProjectError as exc:
            if exc.diagnostic.code != "MIGRATION_REQUIRED":
                self._show_error(exc)
                return False
            choice = QMessageBox.question(
                self,
                "Обновление формата проекта",
                "Проект использует формат 1. Создать восстанавливаемую копию и перейти на формат 2?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return False
            try:
                session = self.controller.migrate(root)
            except ProjectError as migration_error:
                self._show_error(migration_error)
                return False
        self.load_session(session)
        return True

    def create_path(self, root: Path, name: str) -> bool:
        if not self._confirm_all_dirty():
            return False
        try:
            session = self.controller.create(root, name)
        except ProjectError as exc:
            self._show_error(exc)
            return False
        self.load_session(session)
        return True

    def new_project_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Выберите пустую папку проекта")
        if not folder:
            return
        name, accepted = QInputDialog.getText(self, "Новый проект", "Название проекта")
        if accepted:
            self.create_path(Path(folder), name)

    def open_project_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Открыть проект")
        if folder:
            self.open_path(Path(folder))

    def add_component_dialog(self) -> None:
        if self.session is None:
            return
        name, accepted = QInputDialog.getText(self, "Новый компонент", "Название")
        if accepted:
            self.add_component(name)

    def add_component(self, component_id: str | None = None, name: str | None = None) -> bool:
        # The two-argument form remains available to automated legacy scenarios;
        # the UI always supplies only a display name and receives an automatic ID.
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
        self.open_component(component_id)
        self.statusBar().showMessage("Компонент добавлен", 3000)
        return True

    def _open_component_index(self, index: QModelIndex) -> None:
        component_id = index.data(ID_ROLE)
        if component_id:
            self.open_component(component_id)

    def open_component(self, component_id: str) -> DocumentTab | None:
        if self.session is None or component_id not in self.session.documents:
            return None
        existing = self._tabs.get(component_id)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
            return existing
        tab = DocumentTab(component_id)
        tab.zoomChanged.connect(self._zoom_changed)
        tab.previewError.connect(lambda message: self.statusBar().showMessage(message, 7000))
        tab.modeRequested.connect(lambda mode, item=tab: self._request_mode(item, mode))
        tab.draftChanged.connect(lambda text, item=tab: self._draft_changed(item, text))
        tab.canvas.selectionChanged.connect(self._canvas_selection_changed)
        tab.canvas.moveRequested.connect(self._move_selected)
        tab.canvas.resizeRequested.connect(self._resize_selected)
        tab.canvas.placementRequested.connect(self._place_element)
        tab.canvas.toolCancelled.connect(self._tool_cancelled)
        tab.canvas.deleteRequested.connect(self.delete_selected)
        self.undo_group.addStack(tab.undo_stack)
        self._tabs[component_id] = tab
        index = self.tabs.addTab(tab, "")
        self._update_tab_title(component_id)
        self.tabs.setCurrentIndex(index)
        tab.set_text(self.session.documents[component_id].current_text)
        self._refresh_preview(tab, fit=True)
        return tab

    def _active_tab_changed(self, _index: int) -> None:
        for candidate in self._tabs.values():
            if candidate is not self.active_tab:
                candidate.canvas.cancel_tool()
        tab = self.active_tab
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
            self.status_zoom.setText(f"Масштаб: {tab.canvas.zoom_percent}%")
            self._populate_data_panel()
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
        sources = [("Без данных", None)] + [(name, name) for name in self.session.snapshot.model.data_sources]
        self._fill_combo(self.data_source, sources, binding.source if binding else None)
        sheets: list[tuple[str, object]] = []
        if binding is not None and self.controller.preview is not None:
            try:
                source = self.session.snapshot.model.data_sources[binding.source]
                from componentpress.project_io.paths import resolve_project_path
                names = self.controller.preview.reader.sheet_names(resolve_project_path(self.session.snapshot.root, source.path))
                sheets = [(name, name) for name in names]
            except (ProjectError, KeyError) as exc:
                message = str(exc.diagnostic) if isinstance(exc, ProjectError) else str(exc)
                self.statusBar().showMessage(message, 7000)
        self._fill_combo(self.data_sheet, sheets, binding.sheet if binding else None)
        data = self._refresh_data(force=False, render=False) if binding is not None else None
        headers = list(data.headers) if data is not None else []
        optional = [("—", None)] + [(header, header) for header in headers]
        self._fill_combo(self.data_id_column, optional, binding.id_column if binding else None)
        self._fill_combo(self.data_copies_column, optional, binding.copies_column if binding else None)
        self._fill_combo(self.data_column, [(header, header) for header in headers])
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
        values = [] if data is None else [
            (f"{row.instance_id} — строка {row.row_number} — тираж {row.copies}", row.row_number)
            for row in data.rows
        ]
        self._fill_combo(self.data_row, values, current)
        if tab is not None and values:
            tab.preview_row_number = int(self.data_row.currentData())

    def _refresh_data(self, *, force: bool, render: bool = True):
        tab = self.active_tab
        if self.session is None or tab is None or self.controller.preview is None:
            return None
        if self.session.documents[tab.component_id].model.data is None:
            return None
        try:
            component = self.session.documents[tab.component_id].model
            data = self.controller.preview.refresh(self.session.snapshot, tab.component_id, component=component) if force else self.controller.preview.data(self.session.snapshot, tab.component_id, component=component)
        except ProjectError as exc:
            self.statusBar().showMessage(str(exc.diagnostic), 10000)
            return None
        if data is not None:
            self._populate_rows(data)
            headers = [(header, header) for header in data.headers]
            binding = self.session.documents[tab.component_id].model.data
            self._fill_combo(self.data_id_column, [("—", None), *headers], binding.id_column if binding else None)
            self._fill_combo(self.data_copies_column, [("—", None), *headers], binding.copies_column if binding else None)
            self._fill_combo(self.data_column, headers, self.data_column.currentData())
            if render:
                self._refresh_preview(tab)
            self.statusBar().showMessage(f"Данные обновлены: {data.sheet}, версия {data.version}", 3500)
        return data

    def _refresh_preview(self, tab: DocumentTab, *, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        if self.session is None:
            return False
        document = self.session.documents[tab.component_id]
        if document.model.data is None or self.controller.preview is None:
            tab.resolved_component = None
            return tab.refresh(self.session, fit=fit, selected=selected)
        try:
            resolved = self.controller.preview.select_row(
                self.session.snapshot, tab.component_id, row_number=tab.preview_row_number,
                component=document.model,
            )
        except ProjectError as exc:
            tab.previewError.emit(str(exc.diagnostic))
            return False
        tab.preview_row_number = resolved.row.row_number if resolved.row else None
        tab.resolved_component = resolved.component
        return tab.refresh(self.session, component=resolved.component, fit=fit, selected=selected)

    def _row_changed(self, _index: int) -> None:
        tab = self.active_tab
        if tab is None:
            return
        value = self.data_row.currentData()
        tab.preview_row_number = int(value) if value is not None else None
        self._refresh_preview(tab)
        self._update_data_chain()

    def _data_binding_changed(self, _index: int) -> None:
        if self.session is None or self.active_tab is None:
            return
        source = self.data_source.currentData()
        if source is None:
            binding = None
        else:
            sheet = self.data_sheet.currentData()
            if not sheet:
                return
            binding = DataBinding(
                source=str(source), sheet=str(sheet),
                id_column=self.data_id_column.currentData(),
                copies_column=self.data_copies_column.currentData(),
            )
        try:
            self.controller.update_component(self.active_tab.component_id, data=binding)
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.active_tab.preview_row_number = None
        self._populate_data_panel()
        self._refresh_data(force=True)

    def _source_changed(self, _index: int) -> None:
        if self.session is None:
            return
        source_id = self.data_source.currentData()
        if source_id is None:
            self._fill_combo(self.data_sheet, [])
            self._data_binding_changed(-1)
            return
        try:
            source = self.session.snapshot.model.data_sources[str(source_id)]
            from componentpress.project_io.paths import resolve_project_path
            names = self.controller.preview.reader.sheet_names(resolve_project_path(self.session.snapshot.root, source.path)) if self.controller.preview else ()
        except (ProjectError, KeyError) as exc:
            message = str(exc.diagnostic) if isinstance(exc, ProjectError) else str(exc)
            self.statusBar().showMessage(message, 7000)
            return
        self._fill_combo(self.data_sheet, [(name, name) for name in names])
        self._data_binding_changed(-1)

    def _update_data_chain(self, _index: int = -1) -> None:
        source = self.data_source.currentData()
        sheet = self.data_sheet.currentData()
        column = self.data_column.currentData()
        self.data_chain.setText(" → ".join(str(item) for item in (source, sheet, column) if item) or "Нет привязки")

    def _insert_column_binding(self) -> None:
        if self.session is None or self.active_tab is None:
            return
        column = self.data_column.currentData()
        selected = self._selected_ids()
        if not column or len(selected) != 1:
            self.statusBar().showMessage("Выберите один HTML-элемент и столбец", 5000)
            return
        item = locations(self.session.documents[self.active_tab.component_id].model).get(selected[0])
        if item is None or not isinstance(item.node, HtmlNode):
            self.statusBar().showMessage("Привязка столбца вставляется в HTML-элемент", 5000)
            return
        self._edit_selected_node({"html": item.node.html + "{" + str(column) + "}"})

    def _import_xlsx(self) -> None:
        if self.session is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт XLSX", "", "Excel (*.xlsx)")
        if not filename:
            return
        try:
            source_id = self.controller.import_data(Path(filename))
        except ProjectError as exc:
            self._show_error(exc)
            return
        self._populate_data_panel()
        index = self.data_source.findData(source_id)
        if index >= 0:
            self.data_source.setCurrentIndex(index)

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
                component=self.session.documents[self.active_tab.component_id].model,
            )
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.statusBar().showMessage(f"Экспортировано: {result.png}", 5000)

    def _start_build(self, active_only: bool) -> None:
        if self.session is None or self.controller.build is None or self._active_build_job is not None:
            return
        if not self.save_all():
            return
        dialog = BuildOptionsDialog(self.session.snapshot.model.print, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        component_ids = None
        if active_only:
            if self.active_tab is None:
                return
            component_ids = (self.active_tab.component_id,)
        request = BuildRequest(
            component_ids=component_ids,
            print_settings=dialog.print_settings(),
            max_render_workers=dialog.workers.value(),
        )
        progress = QProgressDialog("Подготовка снимка…", "Отменить", 0, 0, self)
        progress.setWindowTitle("Сборка ComponentPress")
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel_build)
        progress.show()
        self._build_progress = progress
        try:
            self._active_build_job = self.controller.start_build(
                request,
                on_progress=self._on_build_progress,
                on_finished=self._on_build_finished,
            )
        except Exception as exc:
            progress.close()
            self._build_progress = None
            self._show_error(exc)
            return
        self.statusBar().showMessage(f"Сборка запущена: {self._active_build_job}")
        self._update_actions()

    def _on_build_progress(self, progress: BuildProgress) -> None:
        if progress.job_id != self._active_build_job or self._build_progress is None:
            return
        self._build_progress.setRange(0, max(progress.total, 1))
        self._build_progress.setValue(progress.completed)
        self._build_progress.setLabelText(progress.message or progress.phase)

    def _on_build_finished(self, result: BuildResult) -> None:
        if self._build_progress is not None:
            self._build_progress.close()
            self._build_progress.deleteLater()
            self._build_progress = None
        self._active_build_job = None
        self._last_build_result = result if result.status == "succeeded" else self._last_build_result
        if result.status == "succeeded":
            suffix = " (очистка будет повторена)" if result.cleanup_state == "cleanup_pending" else ""
            warning = f"; предупреждения: {'; '.join(str(item) for item in result.diagnostics)}" if result.diagnostics else ""
            self.statusBar().showMessage(f"Сборка готова: {result.output_directory}{suffix}{warning}", 12000)
        else:
            message = "\n".join(str(item) for item in result.diagnostics) or f"Сборка: {result.status}"
            QMessageBox.warning(self, "Сборка не завершена", message)
        self._update_actions()
        if self._close_after_build:
            self._close_after_build = False
            self.close()

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
        if self.session is None:
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
        if self.session is None:
            return
        document = self.session.documents[tab.component_id]
        try:
            self.controller.prepare_text(tab.component_id, text)
        except ProjectError as exc:
            document.set_draft(text, (exc.diagnostic,))
            tab.show_diagnostic(str(exc.diagnostic))
        else:
            document.set_draft(text)
            tab.show_diagnostic()
        self._update_tab_title(tab.component_id)
        self._update_actions()

    def _apply_draft(self, component_id: str) -> bool:
        if self.session is None:
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
                tab.show_diagnostic(str(exc.diagnostic))
            self.statusBar().showMessage(str(exc.diagnostic), 7000)
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
            self.element_properties.show_node(node)
            self.component_properties_group.hide()
            self.element_properties_group.show()
            self.statusBar().showMessage(f"Выбран элемент: {node.id}", 2500)
        else:
            self.element_properties.show_node(None)
            self.element_properties_group.hide()
            self.component_properties_group.show()
            self.properties.show_component(self.session.documents[tab.component_id].model)
        self._update_actions()

    def _edit_active_size(self, width: float, height: float) -> None:
        try:
            size = SizeMM.model_validate({"width": width, "height": height})
        except ValidationError as exc:
            self._show_error(exc)
            return
        self._edit_active(size_mm=size)

    def _edit_active(self, **changes: object) -> None:
        tab = self.active_tab
        if tab is None or self.session is None:
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
        if tab is None or self.session is None:
            return False
        document = self.session.documents[tab.component_id]
        if updated == document.model:
            return False
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
        self._update_tab_title(component_id)
        self._refresh_component_tree()
        self._update_actions()

    def _after_undo_redo(self) -> None:
        self._update_actions()

    def _selected_ids(self) -> tuple[str, ...]:
        return self.active_tab.canvas.selected_ids if self.active_tab is not None else ()

    def _apply_tree(self, label: str, operation, *args, selected: tuple[str, ...] | None = None, **kwargs) -> bool:
        tab = self.active_tab
        if tab is None or self.session is None:
            return False
        try:
            updated = operation(self.session.documents[tab.component_id].model, *args, **kwargs)
        except (TreeOperationError, ValidationError, ValueError) as exc:
            QMessageBox.warning(self, "Операция недоступна", str(exc))
            return False
        return self._commit_component(label, updated, selected=selected)

    def _move_selected(self, ids: object, dx_mm: float, dy_mm: float) -> None:
        selected = tuple(ids)
        self._apply_tree("Переместить элементы", move_nodes, selected, dx_mm, dy_mm, selected=selected)

    def _resize_selected(self, node_id: str, width_mm: float, height_mm: float) -> None:
        self._apply_tree("Изменить размер элемента", resize_node, node_id, width_mm, height_mm, selected=(node_id,))

    def _edit_selected_node(self, changes: dict) -> None:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None:
            return
        if "color" in changes and not QColor(str(changes["color"])).isValid():
            QMessageBox.warning(self, "Некорректное значение", "Цвет должен иметь вид #RRGGBB")
            self._show_selection(ids)
            return
        self._apply_tree("Изменить элемент", update_node, ids[0], selected=ids, **changes)

    def delete_selected(self) -> bool:
        ids = self._selected_ids()
        if not ids:
            return False
        return self._apply_tree("Удалить элементы", remove_nodes, ids, selected=())

    def reorder_selected(self, direction: int) -> bool:
        ids = self._selected_ids()
        if not ids:
            return False
        return self._apply_tree("Изменить порядок слоёв", reorder_nodes, ids, direction, selected=ids)

    def _layer_drop(self, ids: object, target_id: str) -> None:
        selected = tuple(ids)
        self._apply_tree("Перетащить слой", place_nodes, selected, target_id, selected=selected)

    def group_selected(self, group_id: str | None = None, name: str | None = None) -> bool:
        ids = self._selected_ids()
        if len(ids) < 2 or self.session is None or self.active_tab is None:
            return False
        component = self.session.documents[self.active_tab.component_id].model
        if name is None:
            name, accepted = QInputDialog.getText(self, "Новая группа", "Название", text="Группа")
            if not accepted:
                return False
        if not name.strip():
            return False
        group_id = group_id or self.controller.node_id(self.active_tab.component_id, name, "group")
        return self._apply_tree("Сгруппировать элементы", group_nodes, ids, group_id, name, selected=(group_id,))

    def ungroup_selected(self) -> bool:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None:
            return False
        found = locations(self.session.documents[self.active_tab.component_id].model).get(ids[0])
        if found is None or not isinstance(found.node, GroupNode):
            return False
        children = tuple(child.id for child in found.node.children)
        return self._apply_tree("Разгруппировать", ungroup_node, ids[0], selected=children)

    def reparent_selected_dialog(self) -> None:
        ids = self._selected_ids()
        if not ids or self.session is None or self.active_tab is None:
            return
        component = self.session.documents[self.active_tab.component_id].model
        create_label = "Создать новую группу…"
        root_label = "Корень (без группы)"
        choices = [create_label, root_label] + [
            item.node.id for item in locations(component).values()
            if isinstance(item.node, GroupNode) and item.node.id not in ids
        ]
        choice, accepted = QInputDialog.getItem(self, "Перемещение элементов", "Новый родитель", choices, 0, False)
        if accepted:
            if choice == create_label:
                self.group_selected()
            else:
                self.reparent_selected(None if choice == root_label else choice)

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
            self.statusBar().showMessage("Щёлкните внутри листа; Esc или щелчок снаружи отменяет размещение")

    def _tool_cancelled(self) -> None:
        self._sync_tool_buttons()
        self.statusBar().showMessage("Размещение отменено", 2000)

    def _sync_tool_buttons(self) -> None:
        active = self.active_tab.canvas.active_tool if self.active_tab is not None else None
        self.image_tool.setChecked(active == "image")
        self.html_tool.setChecked(active == "html")

    def _import_image(self, source: Path, folder: str) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.controller.service.import_image(self.session.snapshot.root, source, folder)

    def _place_element(self, tool: str, x_mm: float, y_mm: float) -> None:
        if self.session is None:
            return
        root = self.session.snapshot.root
        if tool == "image":
            dialog = ImageAddDialog(root, self._import_image, self)
            accepted = dialog.exec() == dialog.DialogCode.Accepted
            if accepted:
                self.add_image(
                    dialog.source.text().strip(), x_mm, y_mm,
                    name=dialog.name.text().strip(), fit=dialog.fit.currentText(),
                )
        else:
            dialog = HtmlAddDialog(root, self._import_image, self)
            accepted = dialog.exec() == dialog.DialogCode.Accepted
            if accepted:
                self.add_html(
                    dialog.html.toPlainText(), x_mm, y_mm,
                    name=dialog.name.text().strip(),
                    font_family=dialog.font.text(), font_size_pt=dialog.font_size.value(), color=dialog.color.text(),
                )
        if self.active_tab is not None:
            self.active_tab.canvas.cancel_tool()
        if not accepted:
            self.statusBar().showMessage("Добавление элемента отменено", 2000)

    def add_image(
        self,
        source: str,
        x_mm: float,
        y_mm: float,
        *,
        name: str = "Изображение",
        fit: str = "contain",
        node_id: str | None = None,
    ) -> bool:
        if self.session is None or self.active_tab is None:
            return False
        component = self.session.documents[self.active_tab.component_id].model
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "image")
        width = max(0.001, min(30.0, component.size_mm.width - x_mm))
        height = max(0.001, min(30.0, component.size_mm.height - y_mm))
        try:
            node = ImageNode(id=node_id, name=name, type="image", x_mm=x_mm, y_mm=y_mm, width_mm=width, height_mm=height, source=source, fit=fit)
        except ValidationError as exc:
            self._show_error(exc)
            return False
        return self._apply_tree("Добавить изображение", add_node, node, selected=(node_id,))

    def add_html(
        self,
        html: str,
        x_mm: float,
        y_mm: float,
        *,
        name: str = "HTML-текст",
        font_family: str = "Arial",
        font_size_pt: float = 10.0,
        color: str = "#111111",
        node_id: str | None = None,
    ) -> bool:
        if self.session is None or self.active_tab is None:
            return False
        component = self.session.documents[self.active_tab.component_id].model
        node_id = node_id or self.controller.node_id(self.active_tab.component_id, name, "html")
        width = max(0.001, min(40.0, component.size_mm.width - x_mm))
        height = max(0.001, min(15.0, component.size_mm.height - y_mm))
        try:
            node = HtmlNode(
                id=node_id, name=name, type="html", x_mm=x_mm, y_mm=y_mm,
                width_mm=width, height_mm=height, font_family=font_family,
                font_size_pt=font_size_pt, color=color, html=html,
            )
        except ValidationError as exc:
            self._show_error(exc)
            return False
        return self._apply_tree("Добавить HTML-текст", add_node, node, selected=(node_id,))

    def insert_html_image(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1 or self.session is None or self.active_tab is None:
            return
        found = locations(self.session.documents[self.active_tab.component_id].model)[ids[0]].node
        if not isinstance(found, HtmlNode):
            return
        picker = ResourcePickerDialog(self.session.snapshot.root, self._import_image, self)
        if picker.exec() != picker.DialogCode.Accepted:
            return
        width, accepted = QInputDialog.getInt(self, "Размер изображения", "Ширина, px", 16, 1, 4096)
        if not accepted:
            return
        height, accepted = QInputDialog.getInt(self, "Размер изображения", "Высота, px", 16, 1, 4096)
        if not accepted:
            return
        align, accepted = QInputDialog.getItem(self, "Выравнивание", "Положение", ["", "middle", "top", "bottom"], 0, False)
        if not accepted:
            return
        self.element_properties.insert_html_image(picker.selected_path, width, height, align)
        self._edit_selected_node({"html": self.element_properties.html_edit.toPlainText()})

    def save_active(self) -> bool:
        tab = self.active_tab
        if tab is None:
            return True
        return self._save_component(tab.component_id)

    def _save_component(self, component_id: str) -> bool:
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

    def save_all(self) -> bool:
        if self.session is None:
            return True
        for component_id in tuple(self.session.documents):
            if not self._apply_draft(component_id):
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
        if index < 0:
            return True
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab):
            return True
        if not self._confirm_document(tab.component_id):
            return False
        self._tabs.pop(tab.component_id, None)
        self.undo_group.removeStack(tab.undo_stack)
        self.tabs.removeTab(index)
        tab.deleteLater()
        return True

    def _confirm_document(self, component_id: str) -> bool:
        if self.session is None or not self.session.documents[component_id].dirty:
            return True
        name = self.session.documents[component_id].model.name
        choice = QMessageBox.question(
            self,
            "Несохранённые изменения",
            f"Сохранить изменения компонента «{name}»?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self._save_component(component_id)
        self.controller.revert_document(component_id)
        return True

    def _confirm_all_dirty(self) -> bool:
        if self.session is None or not self.session.dirty:
            return True
        choice = QMessageBox.question(
            self,
            "Несохранённые изменения",
            "Сохранить изменения открытого проекта?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self.save_all()
        for component_id in tuple(self.session.documents):
            self.controller.revert_document(component_id)
        return True

    def _clear_tabs(self, *, discard: bool = False) -> None:
        if discard and self.session is not None:
            for component_id in tuple(self.session.documents):
                self.controller.revert_document(component_id)
        for tab in self._tabs.values():
            self.undo_group.removeStack(tab.undo_stack)
        self.tabs.clear()
        self._tabs.clear()

    def _refresh_component_tree(self) -> None:
        if self.session is None:
            self.component_tree.setModel(None)
            return
        self.component_tree.setModel(component_model(self.session))
        self.component_tree.resizeColumnToContents(0)

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
            self.status_zoom.setText(f"Масштаб: {percent}%")

    def _update_actions(self) -> None:
        has_project = self.session is not None
        has_tab = self.active_tab is not None
        visual = has_tab and self.active_tab.mode == "layout"
        active_stack = self.active_tab.undo_stack if self.active_tab is not None else None
        # In text mode Ctrl+Z/Ctrl+Y belong to QPlainTextEdit's local history.
        self.undo_action.setEnabled(bool(visual and active_stack and active_stack.canUndo()))
        self.redo_action.setEnabled(bool(visual and active_stack and active_stack.canRedo()))
        self.add_component_action.setEnabled(has_project)
        idle = self._active_build_job is None
        self.build_all_action.setEnabled(has_project and idle)
        self.build_active_action.setEnabled(has_tab and idle)
        self.cancel_build_action.setEnabled(not idle)
        ready = self._last_build_result is not None
        self.open_build_action.setEnabled(ready)
        self.open_pdf_action.setEnabled(ready)
        self.save_all_action.setEnabled(has_project and bool(self.session and self.session.dirty))
        self.save_action.setEnabled(
            has_tab
            and bool(self.session)
            and bool(self.active_tab)
            and self.session.documents[self.active_tab.component_id].dirty
        )
        self.close_tab_action.setEnabled(has_tab)
        for action in (self.zoom_in_action, self.zoom_out_action, self.fit_action):
            action.setEnabled(visual)
        self.image_tool.setEnabled(visual)
        self.html_tool.setEnabled(visual)
        selected = self._selected_ids()
        has_selection = bool(selected)
        for widget in (self.layer_up, self.layer_down, self.reparent_button):
            widget.setEnabled(visual and has_selection)
        self.element_tree.setEnabled(bool(visual))
        self.properties.setEnabled(bool(visual))
        self.element_properties.setEnabled(bool(visual and has_selection))
        self.delete_action.setEnabled(visual and has_selection)
        self.group_action.setEnabled(visual and len(selected) >= 2)
        self.group_button.setEnabled(visual and len(selected) >= 2)
        can_ungroup = False
        if has_tab and len(selected) == 1 and self.session is not None:
            node = locations(self.session.documents[self.active_tab.component_id].model).get(selected[0])
            can_ungroup = bool(node and isinstance(node.node, GroupNode))
        self.ungroup_action.setEnabled(bool(visual and can_ungroup))
        self.ungroup_button.setEnabled(bool(visual and can_ungroup))
        self._sync_tool_buttons()

    def _show_error(self, error: Exception) -> None:
        if isinstance(error, ProjectError):
            message = str(error.diagnostic)
        else:
            message = str(error)
        QMessageBox.critical(self, "Ошибка", message)
        self.statusBar().showMessage(message, 7000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._active_build_job is not None:
            choice = QMessageBox.question(
                self,
                "Выполняется сборка",
                "Отменить сборку и закрыть окно после освобождения ресурсов?\n"
                "Выберите «Нет», чтобы дождаться текущей сборки, или «Отмена», чтобы вернуться в редактор.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            self._close_after_build = True
            if choice == QMessageBox.StandardButton.Yes:
                self._cancel_build()
            event.ignore()
            return
        if self._confirm_all_dirty():
            event.accept()
        else:
            event.ignore()
