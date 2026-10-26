"""Element editors and the nested project resource picker."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from componentpress.i18n import tr
from componentpress.i18n.diagnostics import format_diagnostic, format_project_error
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.resources.repository import ResourceRepository
from componentpress.rendering.fonts import available_font_families
from componentpress.editor.numeric import NumericSpinBox


def format_error(error: Exception) -> str:
    if isinstance(error, Diagnostic):
        return format_diagnostic(error)
    if isinstance(error, ProjectError):
        return format_project_error(error)
    message = str(error).strip()
    return message or tr("ui.operation_failed", error=type(error).__name__)


class ResourcePickerDialog(QDialog):
    def __init__(self, root: Path, importer: Callable, parent=None, *, images: tuple[str, ...] | None = None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.selected_path = ""
        self.setWindowTitle(tr("ui.resource_picker"))
        self.resize(700, 480)
        layout = QVBoxLayout(self)
        image_root = root / "assets" / "images"
        image_root.mkdir(parents=True, exist_ok=True)
        available = ResourceRepository.list_images(root) if images is None else tuple(image_root / Path(item).name for item in images)
        self.images = QListWidget()
        self.images.setObjectName("resourceTiles")
        self.images.setViewMode(QListWidget.ViewMode.IconMode)
        self.images.setIconSize(QSize(72, 72))
        for path in available:
            pixmap = QPixmap(str(path))
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, f"assets/images/{path.name}")
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            self.images.addItem(item)
        layout.addWidget(self.images)
        import_button = QPushButton(tr("ui.import_png_jpeg"))
        layout.addWidget(import_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        import_button.clicked.connect(self._import)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        self.images.itemDoubleClicked.connect(lambda _item: self._accept_selection())

    def _accept_selection(self) -> None:
        item = self.images.currentItem()
        if item is None:
            return
        self.selected_path = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _import(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, tr("ui.image_import_one"), "", tr("ui.image_filter"))
        if not source:
            return
        try:
            self.selected_path = self.importer(Path(source), "")
        except Exception as exc:
            QMessageBox.critical(self, tr("ui.image_import_error"), format_error(exc))
            return
        self.accept()


class ImageKeyDialog(QDialog):
    def __init__(self, name: str, parent=None, *, editing=False, existing_names=(), original_name=""):
        super().__init__(parent)
        self.editing = editing
        self.existing_names = {value.casefold() for value in existing_names if value.casefold() != original_name.casefold()}
        self.setWindowTitle(tr("ui.image_key_edit_title" if editing else "ui.image_key_add_title"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(name)
        self.name.setObjectName("imageKeyName")
        form.addRow(tr("ui.image_key_name"), self.name)
        layout.addLayout(form)
        if editing:
            notice = QLabel(tr("ui.image_key_rename_warning"))
            notice.setWordWrap(True)
            layout.addWidget(notice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

    def _accept(self) -> None:
        value = self.name.text()
        if not value or not all(character.isascii() and (character.isalnum() or character in "_-") for character in value):
            QMessageBox.information(
                self, tr("ui.image_key_edit_title" if self.editing else "ui.image_key_add_title"),
                tr("ui.image_key_name_invalid"),
            )
            return
        value = value.lower()
        if value.casefold() in self.existing_names:
            QMessageBox.information(self, tr("ui.image_key_edit_title" if self.editing else "ui.image_key_add_title"),
                                    tr("error.image_key_exists", name=value))
            return
        self.name.setText(value)
        self.accept()


class ImageManagerDialog(QDialog):
    folderImportRequested = Signal(object)

    def __init__(self, root: Path, project, importer: Callable, unregisterer: Callable,
                 key_creator: Callable, key_editor: Callable, key_remover: Callable, parent=None):
        super().__init__(parent)
        self.root = root
        self.project = project
        self.importer = importer
        self.unregisterer = unregisterer
        self.key_creator, self.key_editor, self.key_remover = key_creator, key_editor, key_remover
        self.setWindowTitle(tr("ui.image_manager_title"))
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.browser = self._build_image_list()
        self.details = self._build_details()
        self.splitter.addWidget(self.browser)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 1)
        layout.addWidget(self.splitter, 1)
        self._build_actions(layout)
        self._connect_manager_signals()
        self.refresh()

    def _build_image_list(self):
        self._build_image_search()
        self._build_image_filters()
        self._build_image_grid()
        browser = QWidget()
        layout = QVBoxLayout(browser)
        layout.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        filters.addWidget(self.filter)
        filters.addWidget(self.image_count, 1)
        layout.addWidget(self.search)
        layout.addLayout(filters)
        layout.addWidget(self.images, 1)
        layout.addWidget(self.empty_state, 1)
        self.empty_state.hide()
        return browser

    def _build_image_search(self):
        self.search = QLineEdit()
        self.search.setObjectName("imageSearch")
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(tr("ui.image_search_placeholder"))

    def _build_image_filters(self):
        self.filter = QComboBox()
        self.filter.setObjectName("imageKeyFilter")
        for key in ("all", "keyed", "unkeyed"):
            self.filter.addItem(tr(f"ui.image_filter.{key}"), key)
        self.image_count = QLabel()
        self.image_count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _build_image_grid(self):
        self.images = QListWidget()
        self.images.setObjectName("projectImages")
        self.images.setViewMode(QListWidget.ViewMode.IconMode)
        self.images.setMovement(QListWidget.Movement.Static)
        self.images.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.images.setWrapping(True)
        self.images.setIconSize(QSize(96, 64))
        self.images.setGridSize(QSize(154, 126))
        self.images.setWordWrap(True)
        self.empty_state = QLabel()
        self.empty_state.setObjectName("imageSearchEmpty")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def _build_details(self):
        self.details = QWidget()
        self.details.setObjectName("imageDetails")
        self.details.setMinimumWidth(0)
        self.details.setMaximumWidth(0)
        details_layout = QVBoxLayout(self.details)
        self.properties = QLabel()
        self.properties.setObjectName("imageProperties")
        self._build_key_controls(details_layout)
        return self.details

    def _build_key_controls(self, layout):
        self._build_primary_key_controls(layout)
        self._build_alias_key_controls(layout)

    def _build_primary_key_controls(self, layout):
        self.copy_name_button = QPushButton(tr("ui.image_copy_name"))
        self.key_name = QLineEdit()
        self.key_name.setObjectName("imageKeyName")
        self.key_name.setReadOnly(True)
        layout.addWidget(self.properties)
        layout.addWidget(self.copy_name_button)
        layout.addWidget(QLabel(tr("ui.image_primary_key")))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_name, 1)
        self.copy_key_button = QPushButton(tr("ui.image_key_copy"))
        self.copy_key_button.setToolTip(tr("ui.image_key_copy"))
        key_row.addWidget(self.copy_key_button)
        layout.addLayout(key_row)

    def _build_alias_key_controls(self, layout):
        self.keys = QListWidget()
        self.keys.setObjectName("imageKeys")
        self.keys.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.aliases_label = QLabel(tr("ui.image_key_aliases"))
        self.add_key_button = QPushButton(tr("ui.image_key_add"))
        self.rename_key_button = QPushButton(tr("ui.image_key_rename"))
        layout.addWidget(self.aliases_label)
        layout.addWidget(self.keys, 1)
        layout.addWidget(self.add_key_button)
        layout.addWidget(self.rename_key_button)

    def _build_actions(self, layout):
        self.import_progress = QLabel()
        self.import_progress.setObjectName("imageImportProgress")
        layout.addWidget(self.import_progress)
        actions = QHBoxLayout()
        self.add_button = QPushButton(tr("ui.image_manager_add"))
        self.folder_button = QPushButton(tr("ui.image_manager_folder"))
        self.remove_button = QPushButton(tr("ui.image_manager_remove"))
        for button in (self.add_button, self.folder_button, self.remove_button):
            actions.addWidget(button)
        layout.addLayout(actions)

    def _connect_manager_signals(self):
        self._animation = QPropertyAnimation(self.details, b"maximumWidth", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._details_animation_finished)
        self.images.itemSelectionChanged.connect(self._selection_changed)
        self.search.textChanged.connect(lambda _text: self.refresh())
        self.filter.currentIndexChanged.connect(lambda _index: self.refresh())
        self.images.itemDoubleClicked.connect(lambda _item: self._select_image(self.images.currentItem()))
        self.add_button.clicked.connect(self._add_image)
        self.folder_button.clicked.connect(self._import_folder)
        self.remove_button.clicked.connect(self._remove_image)
        self.add_key_button.clicked.connect(self._save_key)
        self.keys.itemDoubleClicked.connect(self._edit_key)
        self.keys.customContextMenuRequested.connect(self._key_context_menu)
        self.copy_name_button.clicked.connect(self._copy_name)
        self.copy_key_button.clicked.connect(self._copy_key)
        self.rename_key_button.clicked.connect(self._rename_primary_key)
        self.refresh()

    def refresh(self, project=None) -> None:
        selected = next((image.data(Qt.ItemDataRole.UserRole) for image in self.images.selectedItems()), None)
        scroll_position = self.images.verticalScrollBar().value()
        if project is not None:
            self.project = project
        self.remove_button.setEnabled(False)
        self.images.clear()
        for relative in self.project.images:
            self._add_image_item(relative, selected)
        self._update_image_browser_state()
        self.images.verticalScrollBar().setValue(scroll_position)
        self._selection_changed()

    def _add_image_item(self, relative: str, selected: str | None) -> None:
        path = self.root / relative
        pixmap = QPixmap(str(path))
        names = sorted(name for name, definition in self.project.icons.items() if definition.path == relative)
        if not self._matches_filter(relative, names):
            return
        key_label = "{" + names[0] + "}" if names else tr("ui.image_unkeyed")
        item = QListWidgetItem(f"{path.name}\n{key_label}")
        item.setData(Qt.ItemDataRole.UserRole, relative)
        if not pixmap.isNull():
            item.setIcon(QIcon(self._preview_pixmap(pixmap)))
            item.setToolTip(tr("ui.image_dimensions", width=pixmap.width(), height=pixmap.height()))
        else:
            item.setToolTip(tr("ui.image_manager_read_error"))
        self.images.addItem(item)
        if relative == selected:
            item.setSelected(True)
            self.images.setCurrentItem(item)

    def _update_image_browser_state(self) -> None:
        self.image_count.setText(tr("ui.image_count", count=self.images.count()))
        has_items = self.images.count() > 0
        self.images.setVisible(has_items)
        self.empty_state.setText(tr("ui.image_search_empty" if self.project.images else "ui.image_manager_empty"))
        self.empty_state.setVisible(not has_items)

    def _matches_filter(self, relative: str, names: list[str]) -> bool:
        filter_mode = self.filter.currentData()
        if filter_mode == "keyed" and not names or filter_mode == "unkeyed" and names:
            return False
        query = self.search.text().casefold().strip().strip("{} ")
        return not query or query in Path(relative).name.casefold() or any(query in name.casefold() for name in names)

    @staticmethod
    def _preview_pixmap(source: QPixmap) -> QPixmap:
        preview = QPixmap(112, 76)
        painter = QPainter(preview)
        tile = 10
        for y in range(0, preview.height(), tile):
            for x in range(0, preview.width(), tile):
                color = QColor("#DDE2E8" if (x // tile + y // tile) % 2 else "#F0F2F5")
                painter.fillRect(x, y, tile, tile, color)
        scaled = source.scaled(
            preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap((preview.width() - scaled.width()) // 2, (preview.height() - scaled.height()) // 2, scaled)
        painter.end()
        return preview

    def _selection_changed(self) -> None:
        selected = self.images.selectedItems()
        self._select_image(selected[0] if selected else None)

    def _select_image(self, item) -> None:
        if item is None:
            self._clear_image_selection()
            return
        self._show_image_details(item.data(Qt.ItemDataRole.UserRole))

    def _clear_image_selection(self):
        self.keys.clear()
        self.key_name.clear()
        self.aliases_label.hide()
        self.copy_key_button.setEnabled(False)
        self.add_key_button.setEnabled(False)
        self.rename_key_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self._slide_details(False)

    def _show_image_details(self, relative):
        path = self.root / relative
        pixmap = QPixmap(str(path))
        self.properties.setText(
            tr("ui.image_properties", filename=path.name,
               dimensions=tr("ui.image_dimensions", width=pixmap.width(), height=pixmap.height())
               if not pixmap.isNull() else tr("ui.image_manager_read_error"))
        )
        self.keys.clear()
        names = sorted(name for name, definition in self.project.icons.items() if definition.path == relative)
        self._set_primary_key(names)
        self.aliases_label.setVisible(bool(names[1:]))
        self.keys.setVisible(bool(names[1:]))
        self.copy_key_button.setEnabled(bool(names))
        self.add_key_button.setVisible(not names)
        self.rename_key_button.setVisible(bool(names))
        self.rename_key_button.setEnabled(bool(names))
        self.add_key_button.setEnabled(not pixmap.isNull())
        self.remove_button.setEnabled(True)
        self._slide_details(True)

    def _set_primary_key(self, names):
        self.key_name.setText(names[0] if names else "")
        for name in names[1:]:
            key_item = QListWidgetItem(name)
            key_item.setData(Qt.ItemDataRole.UserRole, name)
            self.keys.addItem(key_item)

    def _slide_details(self, visible: bool) -> None:
        self._animation.stop()
        start = self.details.maximumWidth()
        end = min(420, max(285, self.width() // 3)) if visible else 0
        if visible:
            self.details.show()
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.start()

    def _details_animation_finished(self) -> None:
        if self.details.maximumWidth() == 0:
            self.details.hide()

    def _copy_name(self) -> None:
        selected = self.images.selectedItems()
        item = selected[0] if selected else None
        if item is not None:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(Path(item.data(Qt.ItemDataRole.UserRole)).name)

    def _copy_key(self) -> None:
        from PySide6.QtGui import QGuiApplication
        if self.key_name.text():
            QGuiApplication.clipboard().setText("{" + self.key_name.text() + "}")

    def _save_key(self) -> None:
        selected = self.images.selectedItems()
        item = selected[0] if selected else None
        if item is None:
            return
        relative = item.data(Qt.ItemDataRole.UserRole)
        self._edit_primary_key(relative, editing=False)

    def _rename_primary_key(self):
        selected = self.images.selectedItems()
        if selected:
            self._edit_primary_key(selected[0].data(Qt.ItemDataRole.UserRole), editing=True)

    def _edit_primary_key(self, relative, *, editing):
        pixmap = QPixmap(str(self.root / relative))
        if pixmap.isNull():
            return
        names = sorted(name for name, definition in self.project.icons.items() if definition.path == relative)
        original = self.key_name.text() if editing else ""
        dialog = ImageKeyDialog(
            Path(relative).stem if not names else original, self, editing=editing,
            existing_names=tuple(self.project.icons), original_name=original,
        )
        while dialog.exec() == QDialog.DialogCode.Accepted:
            if self._save_primary_key(relative, dialog, original, editing and bool(names)):
                return

    def _save_primary_key(self, relative, dialog, original, editing) -> bool:
        try:
            if editing:
                self.key_editor(original, dialog.name.text())
            else:
                self.key_creator(dialog.name.text(), relative)
            self.refresh()
            return True
        except Exception as exc:
            title = "ui.image_key_edit_title" if editing else "ui.image_key_add_title"
            QMessageBox.warning(self, tr(title), format_error(exc))
            return False

    def _edit_key(self, item) -> None:
        original_name = item.data(Qt.ItemDataRole.UserRole)
        definition = self.project.icons.get(original_name)
        if definition is None:
            return
        dialog = ImageKeyDialog(
            original_name, self, editing=True, existing_names=tuple(self.project.icons),
            original_name=original_name,
        )
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.key_editor(original_name, dialog.name.text())
                self.refresh()
                return
            except Exception as exc:
                QMessageBox.warning(self, tr("ui.image_key_edit_title"), format_error(exc))

    def _key_context_menu(self, position) -> None:
        item = self.keys.itemAt(position)
        if item is None:
            return
        self.keys.setCurrentItem(item)
        menu = QMenu(self.keys)
        copy = menu.addAction(tr("ui.image_key_copy_alias"))
        edit = menu.addAction(tr("ui.image_key_rename"))
        remove = menu.addAction(tr("ui.image_key_remove"))
        selected = menu.exec(self.keys.mapToGlobal(position))
        if selected == copy:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText("{" + item.data(Qt.ItemDataRole.UserRole) + "}")
        elif selected == edit:
            self._edit_key(item)
        elif selected == remove:
            self._remove_key(item)

    def _remove_key(self, item) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        answer = QMessageBox.question(
            self, tr("ui.image_key_remove_title"), tr("ui.image_key_remove_confirm", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.key_remover(name)
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_key_remove_title"), format_error(exc))

    def _remove_image(self) -> None:
        selected = self.images.selectedItems()
        item = selected[0] if selected else None
        if item is None:
            return
        relative = item.data(Qt.ItemDataRole.UserRole)
        box = QMessageBox(QMessageBox.Icon.Warning, tr("ui.image_manager_remove_title"),
                          tr("ui.image_manager_remove_confirm", name=Path(relative).name),
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setCheckBox(QCheckBox(tr("ui.image_manager_delete_file"), box))
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self.unregisterer(relative, box.checkBox().isChecked())
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_manager_remove_title"), format_error(exc))

    def _add_image(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, tr("ui.image_manager_add_title"), "", tr("ui.image_filter"))
        if not source:
            return
        try:
            self.importer(Path(source), "")
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_manager_add_title"), format_error(exc))

    def _import_folder(self) -> None:
        source = QFileDialog.getExistingDirectory(self, tr("ui.image_manager_choose_folder"))
        if source:
            self.folder_button.setEnabled(False)
            self.folderImportRequested.emit(Path(source))

    def set_import_progress(self, completed: int, total: int, message: str = "") -> None:
        self.import_progress.setText(message or tr("ui.import_progress", completed=completed, total=total))

    def finish_import(
        self, paths: tuple[str, ...] | None, error: Exception | None,
        error_message: str | None = None,
    ) -> None:
        self.folder_button.setEnabled(True)
        if error is not None:
            self.set_import_progress(0, 0, "")
            QMessageBox.warning(self, tr("ui.image_import_title"), error_message or format_error(error))
            return
        self.set_import_progress(len(paths or ()), len(paths or ()), tr("ui.image_import_complete", count=len(paths or ())))

class ImageAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None, *, columns: tuple[str, ...] = (), images: tuple[str, ...] | None = None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.images = images
        self.setWindowTitle(tr("ui.image_new"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.form = form
        self.name = QLineEdit(tr("ui.image"))
        self.name.setObjectName("newImageName")
        self.content_mode = QComboBox()
        self.content_mode.addItem(tr("ui.image_project_file"), "manual")
        self.content_mode.addItem(tr("ui.excel_column"), "column")
        self.content_mode.setEnabled(bool(columns))
        self.content_column = QComboBox()
        self.content_column.addItems(list(columns))
        self.source_row, choose = self._build_source_row()
        self.fit = QComboBox()
        self.fit.addItems(["contain", "cover", "stretch"])
        form.addRow(tr("ui.name"), self.name)
        form.addRow(tr("ui.source"), self.content_mode)
        form.addRow(tr("ui.image_resource"), self.source_row)
        form.addRow(tr("ui.column"), self.content_column)
        form.addRow(tr("ui.mode"), self.fit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        choose.clicked.connect(self._choose)
        self.content_mode.currentIndexChanged.connect(self._sync_content_mode)
        self._sync_content_mode()
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

    def _build_source_row(self):
        source_row = QWidget()
        row = QHBoxLayout(source_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.source = QLineEdit()
        self.source.setObjectName("newImageSource")
        self.source.setReadOnly(True)
        choose = QPushButton(tr("ui.choose"))
        row.addWidget(self.source, 1)
        row.addWidget(choose)
        return source_row, choose

    def _choose(self) -> None:
        if self.content_mode.currentData() != "manual":
            return
        picker = ResourcePickerDialog(self.root, self.importer, self, images=self.images)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.source.setText(picker.selected_path)

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.information(self, tr("ui.image_new"), tr("ui.image_name_required"))
            return
        if self.content_mode.currentData() == "manual" and not self.source.text().strip():
            QMessageBox.information(self, tr("ui.image_new"), tr("ui.image_required"))
            return
        if self.content_mode.currentData() == "column" and not self.content_column.currentText():
            QMessageBox.information(self, tr("ui.image_new"), tr("ui.check_excel_column"))
            return
        self.accept()

    def _sync_content_mode(self, _index: int = 0) -> None:
        manual = self.content_mode.currentData() == "manual"
        self.source.setEnabled(manual)
        self.content_column.setEnabled(not manual)
        self.form.setRowVisible(self.source_row, manual)
        self.form.setRowVisible(self.content_column, not manual)


class HtmlAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None, *, columns: tuple[str, ...] = (), images: tuple[str, ...] | None = None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.images = images
        self.setWindowTitle(tr("ui.html_new"))
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(tr("ui.html_text"))
        self.name.setObjectName("newHtmlName")
        self.content_mode = QComboBox()
        self.content_mode.addItem(tr("ui.html_manual"), "manual")
        self.content_mode.addItem(tr("ui.excel_column"), "column")
        self.content_mode.setEnabled(bool(columns))
        self.content_column = QComboBox()
        self.content_column.addItems(list(columns))
        self.font, self.font_size = _html_font_controls(root)
        self.color = QLineEdit("#111111")
        self.color.setReadOnly(True)
        self.color.setObjectName("newHtmlColor")
        self.color_button = QPushButton(tr("ui.select_color"))
        self.color_button.setObjectName("newHtmlColorPicker")
        color_row = QHBoxLayout()
        color_row.addWidget(self.color, 1)
        color_row.addWidget(self.color_button)
        form.addRow(tr("ui.name"), self.name)
        form.addRow(tr("ui.source"), self.content_mode)
        form.addRow(tr("ui.column"), self.content_column)
        form.addRow(tr("ui.font"), self.font)
        form.addRow(tr("ui.size_pt"), self.font_size)
        form.addRow(tr("ui.color"), color_row)
        layout.addLayout(form)
        self.html = QPlainTextEdit(f"<p>{tr('ui.default_html')}</p>")
        self.html.setObjectName("newHtmlSource")
        layout.addWidget(self.html, 1)
        insert = QPushButton(tr("ui.insert_image"))
        layout.addWidget(insert)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        insert.clicked.connect(self._insert_image)
        self.color_button.clicked.connect(self._pick_color)
        self.content_mode.currentIndexChanged.connect(self._sync_content_mode)
        self._sync_content_mode()
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

    def _insert_image(self) -> None:
        if self.content_mode.currentData() != "manual":
            return
        picker = ResourcePickerDialog(self.root, self.importer, self, images=self.images)
        if picker.exec() != QDialog.DialogCode.Accepted:
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
        suffix = f' align="{align}"' if align else ""
        self.html.textCursor().insertText(
            f'<img src="{picker.selected_path}" width="{width}" height="{height}"{suffix} />'
        )

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.information(self, tr("ui.html_new"), tr("ui.image_name_required"))
            return
        if self.content_mode.currentData() == "column" and not self.content_column.currentText():
            QMessageBox.information(self, tr("ui.html_new"), tr("ui.check_excel_column"))
            return
        self.accept()

    def _pick_color(self) -> None:
        selected = QColorDialog.getColor(QColor(self.color.text()), self, tr("ui.select_text_color"))
        if selected.isValid():
            self.color.setText(selected.name(QColor.NameFormat.HexRgb).upper())

    def _sync_content_mode(self, _index: int = 0) -> None:
        manual = self.content_mode.currentData() == "manual"
        self.content_column.setEnabled(not manual)
        self.html.setEnabled(manual)


def _html_font_controls(root):
    font = QComboBox()
    font.setObjectName("newHtmlFont")
    for family in available_font_families(root):
        font.addItem(family, family)
        font.setItemData(font.count() - 1, QFont(family), Qt.ItemDataRole.FontRole)
    if font.findText("Arial") >= 0:
        font.setCurrentText("Arial")
    size = NumericSpinBox()
    size.setRange(0.1, 1000)
    size.setDecimals(1)
    size.setSingleStep(1.0)
    size.setValue(10)
    size.setObjectName("newHtmlFontSize")
    return font, size
