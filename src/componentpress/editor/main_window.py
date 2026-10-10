"""Stage-five main window with synchronized visual and YAML editing."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QThreadPool, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QCloseEvent, QKeySequence, QUndoGroup
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
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from componentpress.application.sessions import ProjectSession
from componentpress.application.contracts import BuildRequest, BuildResult, BuildProgress
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.component import DataBinding
from componentpress.application.preview_service import ValidationIssues
from componentpress.execution.cancellation import CancellationToken
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.paths import validate_relative_path
from componentpress.domain.copy_mode import copies_for_mode
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, ShapeNode
from componentpress.rendering.fonts import ProjectFonts
from componentpress.platforms.services import PlatformServices
from componentpress.domain.tree import (
    TreeOperationError,
    align_nodes,
    effectively_locked_ids,
    add_node,
    group_nodes,
    locations,
    move_nodes,
    place_nodes,
    remove_nodes,
    reorder_nodes,
    reparent_nodes,
    resize_node,
    resize_line_endpoint,
    ungroup_node,
    update_node,
)

from .controllers import ProjectController
from .commands import ReplaceComponentCommand
from .dialogs import HtmlAddDialog, ImageAddDialog, ResourcePickerDialog
from .build_dialog import BuildOptionsDialog
from .document_tab import DocumentTab
from .properties import ComponentProperties, ElementProperties
from .preview_worker import PreviewFontReadTask, PreviewRequest, PreviewSignals, PreviewTask
from .version_worker import VersionTask
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
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(1)
        self._preview_signals = PreviewSignals(self)
        self._preview_signals.finished.connect(self._preview_task_finished, Qt.ConnectionType.QueuedConnection)
        self._preview_active: tuple[DocumentTab, PreviewRequest, ProjectFonts | None] | None = None
        self._preview_pending: DocumentTab | None = None
        self._build_retry_scheduled = False
        self._version_pool = QThreadPool(self)
        self._version_pool.setMaxThreadCount(1)
        self._version_task = None
        self._version_task_kind: str | None = None
        self._version_task_payload = None
        self._version_cancellation: CancellationToken | None = None
        self._version_progress: QProgressDialog | None = None
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
        self.import_project_archive_action = self._action("Импорт архива проекта…")
        self.export_project_archive_action = self._action("Архив версии проекта…")
        self.export_png_zip_action = self._action("Экспорт PNG ZIP…")
        self.add_component_action = self._action("Добавить компонент…", "Ctrl+Shift+N")
        self.project_settings_action = self._action("Настройки проекта…")
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
        file_menu.addActions([self.new_action, self.open_action, self.import_project_archive_action])
        file_menu.addSeparator()
        file_menu.addActions([self.save_action, self.save_all_action, self.close_tab_action])
        file_menu.addSeparator()
        file_menu.addActions([self.export_png_zip_action, self.export_project_archive_action])
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)
        edit_menu = self.menuBar().addMenu("Правка")
        edit_menu.addActions([self.undo_action, self.redo_action])
        edit_menu.addSeparator()
        edit_menu.addActions([self.delete_action, self.group_action, self.ungroup_action])
        project_menu = self.menuBar().addMenu("Проект")
        project_menu.addAction(self.add_component_action)
        project_menu.addAction(self.project_settings_action)
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
        self.data_mode = QComboBox()
        self.data_mode.setObjectName("dataMode")
        self.data_mode.addItem("Prod", "prod")
        self.data_mode.addItem("Test", "test")
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
        central = QWidget()
        central_layout = QVBoxLayout(central)
        quick = QHBoxLayout()
        quick.addWidget(QLabel("Режим тиража"))
        quick.addWidget(self.data_mode)
        quick.addStretch()
        central_layout.addLayout(quick)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
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
        self.data_row = QComboBox()
        self.data_row.setObjectName("dataRow")
        self.data_column = QComboBox()
        self.data_column.setObjectName("dataColumn")
        self.data_chain = QLabel("Нет привязки")
        self.data_chain.setObjectName("dataChain")
        self.data_chain.setWordWrap(True)
        for label, widget in (
            ("Источник проекта", self.data_source), ("Лист компонента", self.data_sheet),
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
        self.data_export_zip = QPushButton("PNG ZIP…")
        self.data_validate = QPushButton("Валидировать")
        self.data_validate_all = QPushButton("Валидировать всё")
        self.data_clear = QPushButton("Удалить XLSX")
        for button in (self.data_import, self.data_refresh, self.data_insert, self.data_export, self.data_export_zip):
            data_buttons.addWidget(button)
        data_layout.addLayout(data_buttons)
        validation_buttons = QHBoxLayout()
        validation_buttons.addWidget(self.data_validate)
        validation_buttons.addWidget(self.data_validate_all)
        validation_buttons.addWidget(self.data_clear)
        data_layout.addLayout(validation_buttons)
        layout.addWidget(data)
        tools = QGroupBox("Инструменты")
        tools_layout = QVBoxLayout(tools)
        self.image_tool = QPushButton("Изображение")
        self.image_tool.setObjectName("imageTool")
        self.html_tool = QPushButton("HTML-текст")
        self.html_tool.setObjectName("htmlTool")
        self.conditional_group_tool = QPushButton("Условная группа")
        self.conditional_group_tool.setObjectName("conditionalGroupTool")
        self.rectangle_tool = QPushButton("Прямоугольник")
        self.rectangle_tool.setObjectName("rectangleTool")
        self.ellipse_tool = QPushButton("Эллипс")
        self.ellipse_tool.setObjectName("ellipseTool")
        self.line_tool = QPushButton("Линия")
        self.line_tool.setObjectName("lineTool")
        self.drawing_tools = {
            "image": self.image_tool, "html": self.html_tool,
            "conditional_group": self.conditional_group_tool,
            "rectangle": self.rectangle_tool, "ellipse": self.ellipse_tool, "line": self.line_tool,
        }
        for button in self.drawing_tools.values():
            button.setCheckable(True)
            button.setEnabled(False)
            tools_layout.addWidget(button)
        layout.addWidget(tools)
        events = QGroupBox("События")
        events_layout = QVBoxLayout(events)
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText("Поиск событий")
        self.event_list = QListWidget()
        clear_events = QPushButton("Очистить")
        clear_events.clicked.connect(self.event_list.clear)
        events_layout.addWidget(self.event_search)
        events_layout.addWidget(self.event_list, 1)
        events_layout.addWidget(clear_events)
        layout.addWidget(events, 1)
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
        self.multi_properties_group = QGroupBox("Выравнивание выделения")
        multi_layout = QHBoxLayout(self.multi_properties_group)
        self.align_buttons: dict[str, QPushButton] = {}
        for edge, label in (("left", "По левому"), ("right", "По правому"), ("top", "По верхнему"), ("bottom", "По нижнему")):
            button = QPushButton(label)
            button.setObjectName(f"align{edge.title()}")
            button.clicked.connect(lambda _checked=False, edge=edge: self._align_selected(edge))
            self.align_buttons[edge] = button
            multi_layout.addWidget(button)
        self.multi_properties_group.hide()
        layout.addWidget(self.multi_properties_group)
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
        self.import_project_archive_action.triggered.connect(self._import_project_archive)
        self.export_project_archive_action.triggered.connect(self._export_project_archive)
        self.export_png_zip_action.triggered.connect(self._export_png_archive)
        self.add_component_action.triggered.connect(self.add_component_dialog)
        self.project_settings_action.triggered.connect(self.project_settings_dialog)
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
        self.reparent_button.clicked.connect(self.reparent_selected_dialog)
        self.element_tree.layerDropRequested.connect(self._layer_drop)
        self.element_properties.nodeChanged.connect(self._edit_selected_node)
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
        self.data_mode.currentIndexChanged.connect(self._data_mode_changed)
        self.event_search.textChanged.connect(self._filter_events)

    def load_session(self, session: ProjectSession) -> None:
        """Install an already opened session; useful to bootstrap and to test the UI."""
        self._clear_tabs(discard=True)
        self.controller.set_session(session)
        self._refresh_component_tree()
        self.setWindowTitle(f"{session.snapshot.model.name} — ComponentPress")
        self.status_project.setText(str(session.snapshot.root))
        if self.controller.recovery_diagnostics:
            details = "; ".join(item.message for item in self.controller.recovery_diagnostics)
            self.statusBar().showMessage(f"Восстановление операций: {details}", 15000)
        self._update_actions()

    def open_path(self, root: Path) -> bool:
        if not self._confirm_all_dirty():
            return False
        try:
            session = self.controller.open(root)
        except ProjectError as exc:
            self._show_error(exc)
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

    def _import_project_archive(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт архива проекта", "", "Архив проекта (*.zip)")
        if not filename:
            return
        parent = QFileDialog.getExistingDirectory(self, "Папка для восстановления архива")
        if not parent:
            return
        folder_name, accepted = QInputDialog.getText(self, "Имя восстановленного проекта", "Новая папка проекта")
        if not accepted or not folder_name.strip():
            return
        try:
            validate_relative_path(folder_name)
            if "/" in folder_name:
                raise ValueError("нужно указать имя одной папки")
        except (ProjectError, ValueError) as exc:
            self._show_error(exc)
            return
        destination = Path(parent) / folder_name.strip()
        if destination.exists():
            QMessageBox.warning(self, "Импорт архива", f"Папка уже существует:\n{destination}")
            return
        if not self.save_all():
            return
        self._start_version_task(
            "import",
            (Path(filename), destination),
            lambda cancellation, progress: self.controller.service.versions.restore_path(
                Path(filename), destination, cancellation=cancellation, on_progress=progress
            ),
        )

    def _export_project_archive(self) -> None:
        if self.session is None or self._active_build_job is not None or self._version_task is not None:
            return
        if not self.save_all():
            return
        version = self.session.snapshot.model.version
        default_name = f"{self.session.snapshot.model.id}-{version}.zip"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Архив версии проекта", str(self.session.snapshot.root / "archive" / default_name), "ZIP (*.zip)"
        )
        if not filename:
            return
        target = Path(filename)
        snapshot = self.session.snapshot
        self._start_version_task(
            "export",
            (snapshot, target, False),
            lambda cancellation, progress: self.controller.service.versions.export(
                snapshot, target, cancellation=cancellation, on_progress=progress
            ),
        )

    def _start_version_task(self, kind: str, payload, operation) -> None:
        if self._version_task is not None:
            return
        cancellation = CancellationToken()
        task = VersionTask(operation, cancellation)
        task.signals.progress.connect(self._version_task_progress, Qt.ConnectionType.QueuedConnection)
        task.signals.finished.connect(self._version_task_finished, Qt.ConnectionType.QueuedConnection)
        self._version_task = task
        self._version_task_kind = kind
        self._version_task_payload = payload
        self._version_cancellation = cancellation
        self._version_progress = QProgressDialog("Подготовка архива…", "Отменить", 0, 0, self)
        self._version_progress.setWindowTitle("Архив проекта")
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
        self._version_progress.setLabelText(phase)
        self._version_progress.setRange(0, max(1, total))
        self._version_progress.setValue(min(completed, max(1, total)))

    def _version_task_finished(self, result, error) -> None:
        kind, payload = self._version_task_kind, self._version_task_payload
        if self._version_progress is not None:
            self._version_progress.close()
        self._version_progress = None
        self._version_task = None
        self._version_task_kind = None
        self._version_task_payload = None
        self._version_cancellation = None
        self._update_actions()
        if isinstance(error, ProjectError):
            if kind == "export" and error.diagnostic.code in {"ARCHIVE_EXISTS", "ARCHIVE_VERSION_EXISTS"}:
                _snapshot, target, _replace = payload
                replace_target = error.diagnostic.path or target
                answer = QMessageBox.question(
                    self,
                    "Архив уже существует",
                    f"Архив проекта уже существует:\n{replace_target}\n\nЗаменить его после проверки нового архива?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer == QMessageBox.StandardButton.Yes and self.session is not None:
                    snapshot = self.session.snapshot
                    self._start_version_task(
                        "export",
                        (snapshot, replace_target, True),
                        lambda cancellation, progress: self.controller.service.versions.export(
                            snapshot, replace_target, replace=True, cancellation=cancellation, on_progress=progress
                        ),
                    )
                return
            if error.diagnostic.code == "BUILD_CANCELLED":
                self.statusBar().showMessage("Операция с архивом отменена", 5000)
            else:
                self._show_error(error)
            return
        if error is not None:
            self._show_error(error)
            return
        if kind == "import":
            try:
                session = self.controller.open(result)
            except ProjectError as exc:
                self._show_error(exc)
                self.statusBar().showMessage(f"Архив восстановлен в {result}; предыдущая сессия сохранена", 12000)
                return
            self.load_session(session)
            return
        if kind == "export":
            archive_path, next_version, patched, patch_error = result
            try:
                snapshot = self.controller.service.projects.open(payload[0].root)
                if self.session is not None and self.session.snapshot.root == snapshot.root:
                    self.session.replace_snapshot(snapshot, saved=set(snapshot.documents))
                self._refresh_component_tree()
                self._active_tab_changed(self.tabs.currentIndex())
            except ProjectError as exc:
                self._show_error(exc)
            if not patched:
                current_version = self.session.snapshot.model.version if self.session is not None else "неизвестна"
                QMessageBox.warning(
                    self,
                    "Архив создан",
                    f"Архив сохранён: {archive_path}\nТекущая версия проекта: {current_version}. Журнал операции будет сверён при следующем открытии.\n{patch_error or ''}",
                )
                return
            assert self.session is not None
            self.setWindowTitle(f"{self.session.snapshot.model.name} — ComponentPress")
            self.statusBar().showMessage(f"Архив {archive_path} сохранён; версия проекта повышена до {next_version}", 10000)

    def project_settings_dialog(self) -> None:
        if self.session is None:
            return
        model = self.session.snapshot.model
        name, accepted = QInputDialog.getText(self, "Настройки проекта", "Название", text=model.name)
        if not accepted:
            return
        version, accepted = QInputDialog.getText(self, "Настройки проекта", "Версия игры", text=model.version)
        if not accepted:
            return
        try:
            old_parts = tuple(int(part) for part in model.version.split("."))
            new_parts = tuple(int(part) for part in version.split("."))
            if len(new_parts) == 3 and new_parts < old_parts:
                answer = QMessageBox.warning(
                    self,
                    "Понижение версии",
                    "Понижение версии может привести к конфликту имени архива. Продолжить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        except ValueError:
            pass
        prod, accepted = QInputDialog.getText(self, "Столбцы тиража", "Prod", text=model.copies_columns.prod)
        if not accepted:
            return
        test, accepted = QInputDialog.getText(self, "Столбцы тиража", "Test", text=model.copies_columns.test)
        if not accepted:
            return
        try:
            self.controller.service.set_project_details(self.session, name=name, version=version)
            self.controller.service.set_copies_columns(self.session, prod=prod, test=test)
            self.setWindowTitle(f"{name} — ComponentPress")
        except (ProjectError, ValidationError) as exc:
            self._show_error(exc if isinstance(exc, ProjectError) else ProjectError(Diagnostic("PROJECT_SETTINGS", str(exc))))

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
        tab.previewRequested.connect(lambda item=tab: self._preview_requested(item))
        tab.previewError.connect(lambda message: (self.statusBar().showMessage(message, 7000), self._add_events((Diagnostic("PREVIEW_ERROR", message),))))
        tab.modeRequested.connect(lambda mode, item=tab: self._request_mode(item, mode))
        tab.draftChanged.connect(lambda text, item=tab: self._draft_changed(item, text))
        tab.canvas.selectionChanged.connect(self._canvas_selection_changed)
        tab.canvas.moveRequested.connect(self._move_selected)
        tab.canvas.resizeRequested.connect(self._resize_selected)
        tab.canvas.lineResizeRequested.connect(self._resize_line_selected)
        tab.canvas.placementRequested.connect(self._place_element)
        tab.canvas.drawRequested.connect(self._draw_element)
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
                candidate.canvas.release_preview()
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
            if tab.canvas._pixmap_item is None:
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
        sources = [("Без XLSX", None)] + ([(source.path, "main")] if source else [])
        self._fill_combo(self.data_source, sources, "main" if source else None)
        sheets: list[tuple[str, object]] = []
        if source is not None and self.controller.preview is not None:
            try:
                from componentpress.project_io.paths import resolve_project_path
                names = self.controller.preview.reader.sheet_names(resolve_project_path(self.session.snapshot.root, source.path))
                sheets = [(name, name) for name in names]
            except ProjectError as exc:
                message = str(exc.diagnostic)
                self.statusBar().showMessage(message, 7000)
        self._fill_combo(self.data_sheet, sheets, binding.sheet if binding else None)
        data = self._refresh_data(force=False, render=False) if binding is not None else None
        headers = list(data.headers) if data is not None else []
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
        mode = self.data_mode.currentData() or "prod"
        values = [] if data is None else [
            (f"{row.instance_id} — строка {row.row_number} — тираж {copies_for_mode(row, mode)}", row.row_number)
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
                self.statusBar().showMessage("Сначала обновите данные или провалидируйте их, затем выберите экземпляр в текущем режиме", 5000)
            elif is_excel and data is not None and not any(copies_for_mode(row, str(self.data_mode.currentData())) > 0 for row in data.rows):
                self.active_tab.preview_row_number = None
                self.active_tab.canvas.clear_document()
                self.statusBar().showMessage("В выбранном режиме нет экземпляров", 5000)
            else:
                self._refresh_preview(self.active_tab)

    def _add_events(self, diagnostics) -> None:
        for diagnostic in diagnostics:
            self.event_list.addItem(str(diagnostic))
        self._filter_events(self.event_search.text())

    def _filter_events(self, query: str) -> None:
        needle = query.casefold().strip()
        for index in range(self.event_list.count()):
            item = self.event_list.item(index)
            item.setHidden(needle not in item.text().casefold())

    def _validate_active(self) -> None:
        if self.session is None or self.active_tab is None or self.controller.preview is None:
            return
        component_id = self.active_tab.component_id
        if self.session.documents[component_id].model.data is None:
            self.statusBar().showMessage("У компонента не выбран лист Excel", 5000)
            return
        try:
            data = self.controller.preview.refresh(self.session.snapshot, component_id)
        except ValidationIssues as exc:
            self._add_events(exc.diagnostics)
            self.statusBar().showMessage(f"Ошибок: {len(exc.diagnostics)}; подробности в панели событий", 7000)
            return
        except ProjectError as exc:
            self._add_events((exc.diagnostic,))
            self.statusBar().showMessage("Проверка не пройдена; подробности в панели событий", 7000)
            return
        self._add_events((Diagnostic("VALIDATION_OK", f"Лист {data.sheet} проверен; строк: {len(data.rows)}", data.path, source="main", sheet=data.sheet, severity="info"),))
        self._populate_data_panel()

    def _validate_all(self) -> None:
        if self.session is None or self.controller.preview is None:
            return
        diagnostics = self.controller.preview.validate_all(self.session.snapshot)
        self._add_events(diagnostics or (Diagnostic("VALIDATION_OK", "Все листы проекта проверены", self.session.snapshot.root, severity="info"),))
        if diagnostics:
            self.statusBar().showMessage(f"Ошибок: {len(diagnostics)}; подробности в панели событий", 7000)
        else:
            self.statusBar().showMessage("Все листы проекта проверены", 5000)

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
            self._add_events(exc.diagnostics if isinstance(exc, ValidationIssues) else (exc.diagnostic,))
            self.statusBar().showMessage(str(exc.diagnostic), 10000)
            return None
        if data is not None:
            self._populate_rows(data)
            headers = [(header, header) for header in data.headers]
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
            active_component = document.model
            bindings_resolved = False
        else:
            tab.resolved_component = None
            try:
                resolved = self.controller.preview.select_row(
                    self.session.snapshot, tab.component_id, row_number=tab.preview_row_number,
                    mode=str(self.data_mode.currentData() or "prod"),
                    component=document.model,
                    validate_resources=False,
                )
            except ProjectError as exc:
                tab.previewError.emit(str(exc.diagnostic))
                if self.active_tab is tab and len(tab.canvas.selected_ids) == 1:
                    self._show_selection(tab.canvas.selected_ids)
                return False
            tab.preview_row_number = resolved.row.row_number if resolved.row else None
            tab.resolved_component = resolved.component
            active_component = resolved.component
            bindings_resolved = True
        dpi = tab._preview_dpi(
            active_component, tab.canvas.zoom_percent / 100,
            tab.canvas.viewport().devicePixelRatioF(),
        )
        tab.queue_preview(
            self.session, component=active_component, dpi=dpi, fit=fit,
            selected=selected, bindings_resolved=bindings_resolved,
        )
        return True

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
        if self.active_tab is not tab or self._active_build_job is not None:
            tab.preview_deferred = True
            return
        if self._preview_active is not None:
            self._preview_pending = tab
            return
        payload = tab.preview_request_data()
        if payload is None:
            return
        snapshot, component, dpi, bindings_resolved, generation = payload
        request = PreviewRequest(tab.component_id, generation, snapshot, component, dpi, bindings_resolved, tab.preview_row_number)
        fonts = ProjectFonts(snapshot.root, self._preview_font_families(component))
        self._preview_active = (tab, request, fonts)
        self._preview_pool.start(PreviewFontReadTask(request, self._preview_signals))

    def _preview_task_finished(self, payload) -> None:
        phase, request, result, error = payload
        active = self._preview_active
        if active is None or active[1] != request:
            return
        tab, _request, fonts = active
        if phase == "fonts":
            try:
                if error is not None:
                    raise error
                assert fonts is not None
                fonts.register_captured(result)
                for family in self._preview_font_families(request.component):
                    fonts.require(family, owner=request.snapshot.root / "project.yaml", field="font_family")
            except Exception as exc:
                if fonts is not None:
                    fonts.close()
                self._preview_active = None
                if self._tabs.get(tab.component_id) is tab:
                    message = str(exc.diagnostic) if isinstance(exc, ProjectError) else str(exc)
                    tab.fail_preview(message, generation=request.generation)
                self._start_pending_preview()
                return
            self._preview_pool.start(PreviewTask(request, self._preview_signals))
            return
        if fonts is not None:
            fonts.close()
        self._preview_active = None
        if self._tabs.get(tab.component_id) is tab and self.session is not None and self.session.snapshot.root == request.snapshot.root:
            if error is None and self.active_tab is tab:
                image, quality = result
                if tab.apply_preview(image, quality, generation=request.generation):
                    self._update_selected_image_quality(tab)
                    diagnostics = self._preview_quality_diagnostics(request, quality)
                    fingerprint = tuple((item.node_id, item.source, item.message, item.cell) for item in diagnostics)
                    if fingerprint != tab.quality_warning_fingerprint:
                        tab.quality_warning_fingerprint = fingerprint
                        if diagnostics:
                            self._add_events(diagnostics)
                            self.statusBar().showMessage(
                                f"Изображений ниже 300 dpi: {len(diagnostics)}; сборка доступна",
                                6000,
                            )
            elif error is not None and self.active_tab is tab and tab.fail_preview(str(error.diagnostic) if isinstance(error, ProjectError) else str(error), generation=request.generation):
                self.event_list.setCurrentRow(self.event_list.count() - 1)
                self.event_list.setFocus()
            elif self.active_tab is not tab:
                tab.preview_deferred = True
        self._start_pending_preview()

    def _preview_quality_diagnostics(self, request: PreviewRequest, quality) -> tuple[Diagnostic, ...]:
        document = request.snapshot.documents[request.component_id]
        binding = document.model.data
        data = None
        row = None
        if binding is not None and self.controller.preview is not None:
            data = self.controller.preview.cached_data(request.snapshot, request.component_id, component=document.model)
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
                warning = Diagnostic(
                    warning.code, f"строка Excel {row.row_number}: {warning.message}",
                    warning.path, warning.field, warning.line, warning.severity,
                    warning.source, warning.sheet, cell, warning.node_id,
                )
            diagnostics.append(warning)
        return tuple(diagnostics)

    def _start_pending_preview(self) -> None:
        pending, self._preview_pending = self._preview_pending, None
        if pending is not None and self._tabs.get(pending.component_id) is pending:
            if self.active_tab is pending and self._active_build_job is None:
                self._preview_requested(pending)
            else:
                pending.preview_deferred = True

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
        self._edit_selected_node({"content_mode": "column", "content_column": str(column)})

    def _import_xlsx(self) -> None:
        if self.session is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт XLSX", "", "Excel (*.xlsx)")
        if not filename:
            return
        try:
            self.controller.import_data(Path(filename))
        except ProjectError as exc:
            self._show_error(exc)
            return
        self._populate_data_panel()
        self._refresh_component_tree()

    def _clear_xlsx(self) -> None:
        if self.session is None:
            return
        choice = QMessageBox.question(self, "Удалить XLSX", "Отвязать все листы компонентов и убрать источник Excel из проекта?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
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
                mode=str(self.data_mode.currentData() or "prod"),
                component=self.session.documents[self.active_tab.component_id].model,
            )
        except ProjectError as exc:
            self._show_error(exc)
            return
        self.statusBar().showMessage(f"Экспортировано: {result.png}", 5000)

    def _export_png_archive(self) -> None:
        if self.session is None or not self.save_all():
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Экспорт PNG ZIP")
        choice.setText("Какие компоненты включить в архив?")
        active_button = choice.addButton("Активный компонент", QMessageBox.ButtonRole.AcceptRole) if self.active_tab is not None else None
        project_button = choice.addButton("Весь проект", QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked not in (active_button, project_button):
            return
        component_ids = (self.active_tab.component_id,) if self.active_tab is not None and clicked is active_button else None
        target_name = f"{self.active_tab.component_id}-{self.data_mode.currentData()}.zip" if component_ids else f"{self.session.snapshot.model.id}-{self.data_mode.currentData()}.zip"
        filename, _ = QFileDialog.getSaveFileName(self, "Сохранить PNG ZIP", target_name, "ZIP (*.zip)")
        if not filename:
            return
        try:
            from componentpress.application.png_archive import export_png_archive
            from componentpress.execution.cancellation import CancellationToken

            cancellation = CancellationToken()
            progress = QProgressDialog("Подготовка PNG ZIP…", "Отменить", 0, 0, self)
            progress.setWindowTitle("Экспорт PNG ZIP")
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.canceled.connect(cancellation.cancel)
            progress.show()

            def update_progress(done: int, total: int, phase: str) -> None:
                progress.setRange(0, max(total, 1))
                progress.setValue(done)
                progress.setLabelText(phase)
                QApplication.processEvents()

            result = export_png_archive(
                self.session.snapshot,
                Path(filename),
                mode=str(self.data_mode.currentData() or "prod"),
                component_ids=component_ids,
                cancellation=cancellation,
                on_progress=update_progress,
            )
        except ProjectError as exc:
            if "progress" in locals():
                progress.close()
            if exc.diagnostic.code == "BUILD_CANCELLED":
                self.statusBar().showMessage("Экспорт PNG ZIP отменён", 5000)
                return
            self._show_error(exc)
            return
        progress.close()
        self.statusBar().showMessage(
            f"PNG ZIP создан: {result.path} ({result.png_count} PNG, {result.copies} копий, {result.mode})", 7000
        )

    def _start_build(self, active_only: bool) -> None:
        if self.session is None or self.controller.build is None or self._active_build_job is not None:
            return
        if self._preview_active is not None or self._preview_pool.activeThreadCount():
            if not self._build_retry_scheduled:
                self._build_retry_scheduled = True
                self.statusBar().showMessage("Предпросмотр завершает подготовку; сборка начнётся сразу после неё")
                QTimer.singleShot(50, lambda: (setattr(self, "_build_retry_scheduled", False), self._start_build(active_only)))
            return
        if not self.save_all():
            return
        mode = str(self.data_mode.currentData() or "prod")
        dialog = BuildOptionsDialog(self.session.snapshot.model.print, self, mode=mode)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        component_ids = None
        if active_only:
            if self.active_tab is None:
                return
            component_ids = (self.active_tab.component_id,)
        request = BuildRequest(
            component_ids=component_ids,
            mode=mode,
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
        if self.active_tab is not None and self.active_tab.canvas._pixmap_item is None:
            self._refresh_preview(self.active_tab)
        if self.active_tab is not None and self.active_tab.preview_deferred:
            self.active_tab.preview_deferred = False
            self._refresh_preview(self.active_tab)
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
            columns = ()
            if self.controller.preview is not None:
                try:
                    data = self.controller.preview.data(self.session.snapshot, tab.component_id)
                    columns = data.headers if data is not None else ()
                except ProjectError:
                    pass
            component = self.session.documents[tab.component_id].model
            preview_node = locations(tab.resolved_component).get(node.id) if isinstance(node, ConditionalGroupNode) and tab.resolved_component is not None else None
            matches = preview_node.node.condition_matches if preview_node is not None and isinstance(preview_node.node, ConditionalGroupNode) else None
            self.element_properties.show_node(node, columns, component, matches)
            self.element_properties.show_image_quality(
                item for item in tab.image_quality if item.node_id == node.id
            )
            self.component_properties_group.hide()
            self.multi_properties_group.hide()
            self.element_properties_group.show()
            self.element_properties.set_edit_locked(ids[0] in effectively_locked_ids(component))
            self.statusBar().showMessage(f"Выбран элемент: {node.id}", 2500)
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
        current = self.session.documents[self.active_tab.component_id].model.background
        color = QColorDialog.getColor(QColor(current), self, "Цвет фона компонента")
        if color.isValid():
            self._edit_active(background=color.name(QColor.NameFormat.HexRgb).upper())

    def _pick_node_color(self, current: str) -> None:
        initial = QColor(current)
        if not initial.isValid():
            initial = QColor("#111111")
        color = QColorDialog.getColor(initial, self, "Цвет текста")
        if color.isValid():
            self._edit_selected_node({"color": color.name(QColor.NameFormat.HexRgb).upper()})

    def _align_selected(self, edge: str) -> None:
        selected = self._selected_ids()
        self._apply_tree("Выровнять элементы", align_nodes, selected, edge, selected=selected)

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

    def _resize_line_selected(self, node_id: str, dx_mm: float, dy_mm: float) -> None:
        self._apply_tree("Изменить линию", resize_line_endpoint, node_id, dx_mm, dy_mm, selected=(node_id,))

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
            prompt = "Протяните по листу для рисования; Esc отменяет" if tool in ("rectangle", "ellipse", "line") else "Щёлкните внутри листа; Esc отменяет"
            self.statusBar().showMessage(prompt)

    def _tool_cancelled(self) -> None:
        self._sync_tool_buttons()
        self.statusBar().showMessage("Размещение отменено", 2000)

    def _sync_tool_buttons(self) -> None:
        active = self.active_tab.canvas.active_tool if self.active_tab is not None else None
        for name, button in self.drawing_tools.items():
            button.setChecked(active == name)

    def _import_image(self, source: Path, folder: str) -> str:
        if self.session is None:
            raise RuntimeError("проект не открыт")
        return self.controller.service.import_image(self.session.snapshot.root, source, folder)

    def _place_element(self, tool: str, x_mm: float, y_mm: float) -> None:
        if self.session is None:
            return
        root = self.session.snapshot.root
        accepted = True
        if tool == "conditional_group":
            columns: tuple[str, ...] = ()
            if self.controller.preview is not None and self.active_tab is not None:
                try:
                    data = self.controller.preview.data(self.session.snapshot, self.active_tab.component_id)
                    columns = data.headers if data is not None else ()
                except ProjectError:
                    pass
            if not columns:
                self._show_error(ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "сначала задайте лист Excel и проверьте его заголовки")))
                accepted = False
            else:
                name, accepted = QInputDialog.getText(self, "Условная группа", "Название", text="Условная группа")
                if accepted and name.strip():
                    component_id = self.active_tab.component_id
                    node_id = self.controller.node_id(component_id, name, "condition")
                    node = ConditionalGroupNode(id=node_id, type="conditional_group", name=name,
                        x_mm=x_mm, y_mm=y_mm, children=(), condition_column=columns[0], condition_value="")
                    self._apply_tree("Добавить условную группу", add_node, node, selected=(node_id,))
        elif tool == "image":
            dialog = ImageAddDialog(root, self._import_image, self)
            accepted = dialog.exec() == dialog.DialogCode.Accepted
            if accepted:
                self.add_image(
                    dialog.source.text().strip(), x_mm, y_mm,
                    name=dialog.name.text().strip(), fit=dialog.fit.currentText(),
                )
        elif tool == "html":
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

    def _draw_element(self, tool: str, x1: float, y1: float, x2: float, y2: float) -> None:
        if self.session is None or self.active_tab is None:
            return
        component_id = self.active_tab.component_id
        if tool == "line":
            dx, dy = x2 - x1, y2 - y1
            if abs(dx) < 0.01 and abs(dy) < 0.01:
                return
            name = "Линия"
            node_id = self.controller.node_id(component_id, name, "line")
            node = LineNode(id=node_id, type="line", name=name, x_mm=x1, y_mm=y1, dx_mm=dx, dy_mm=dy)
        elif tool in ("rectangle", "ellipse"):
            x, y = min(x1, x2), min(y1, y2)
            width, height = abs(x2 - x1), abs(y2 - y1)
            if min(width, height) < 0.01:
                return
            name = "Прямоугольник" if tool == "rectangle" else "Эллипс"
            node_id = self.controller.node_id(component_id, name, tool)
            node = ShapeNode(id=node_id, type=tool, name=name, x_mm=x, y_mm=y, width_mm=width, height_mm=height)
        else:
            return
        self._apply_tree("Нарисовать элемент", add_node, node, selected=(node_id,))

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
        tab.preview_timer.stop()
        tab.preview_generation += 1
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
            tab.preview_timer.stop()
            tab.preview_generation += 1
            self.undo_group.removeStack(tab.undo_stack)
        self.tabs.clear()
        self._tabs.clear()
        self._preview_pending = None

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
        idle = self._active_build_job is None and self._version_task is None
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
        for button in self.drawing_tools.values():
            button.setEnabled(visual)
        self.export_png_zip_action.setEnabled(has_project and idle)
        self.export_project_archive_action.setEnabled(has_project and idle)
        self.import_project_archive_action.setEnabled(idle)
        selected = self._selected_ids()
        has_selection = bool(selected)
        locked_selection = False
        if has_tab and self.session is not None and selected:
            component = self.session.documents[self.active_tab.component_id].model
            locked_selection = any(node_id in effectively_locked_ids(component) for node_id in selected)
        # Reordering another layer across a locked sibling is explicitly allowed;
        # operations that target the selected locked node are disabled.
        for widget in (self.layer_up, self.layer_down):
            widget.setEnabled(visual and has_selection)
        self.reparent_button.setEnabled(visual and has_selection and not locked_selection)
        for button in self.align_buttons.values():
            button.setEnabled(visual and len(selected) >= 2 and not locked_selection)
        self.element_tree.setEnabled(bool(visual))
        self.properties.setEnabled(bool(visual))
        self.element_properties.setEnabled(bool(visual and has_selection))
        self.delete_action.setEnabled(visual and has_selection and not locked_selection)
        self.group_action.setEnabled(visual and len(selected) >= 2 and not locked_selection)
        self.group_button.setEnabled(visual and len(selected) >= 2 and not locked_selection)
        can_ungroup = False
        if has_tab and len(selected) == 1 and self.session is not None:
            node = locations(self.session.documents[self.active_tab.component_id].model).get(selected[0])
            can_ungroup = bool(node and isinstance(node.node, GroupNode))
        can_ungroup = can_ungroup and not locked_selection
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
        if self._version_task is not None:
            QMessageBox.information(self, "Архив проекта", "Дождитесь завершения операции или отмените её.")
            event.ignore()
            return
        for tab in self._tabs.values():
            tab.preview_timer.stop()
            tab.preview_generation += 1
        self._preview_pool.waitForDone(10000)
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
