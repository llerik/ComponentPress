"""Element editors and the nested project resource picker."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from collections.abc import Callable

from PySide6.QtCore import QDir, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)

from componentpress.i18n import tr
from componentpress.project_io.files import file_hash
from componentpress.project_io.paths import resolve_project_path
from componentpress.resources.repository import ResourceRepository
from componentpress.rendering.fonts import available_font_families


def format_error(error: Exception) -> str:
    message = str(error).strip()
    return message or tr("ui.operation_failed", error=type(error).__name__)


class ResourcePickerDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.selected_path = ""
        self.setWindowTitle(tr("ui.resource_picker"))
        self.resize(700, 480)
        layout = QVBoxLayout(self)
        self.model = QFileSystemModel(self)
        self.model.setNameFilters(["*.png", "*.jpg", "*.jpeg"])
        self.model.setNameFilterDisables(False)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        images = root / "assets" / "images"
        images.mkdir(parents=True, exist_ok=True)
        self.model.setRootPath(str(images))
        self.tree = QTreeView()
        self.tree.setObjectName("resourceTree")
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(str(images)))
        self.tree.setColumnWidth(0, 420)
        layout.addWidget(self.tree)
        import_button = QPushButton(tr("ui.import_png_jpeg"))
        layout.addWidget(import_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        import_button.clicked.connect(self._import)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        self.tree.doubleClicked.connect(lambda _index: self._accept_selection())


class ImageManagerDialog(QDialog):
    folderImportRequested = Signal(object, str)

    def __init__(self, root: Path, importer: Callable, replacer: Callable, parent=None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.replacer = replacer
        self.setWindowTitle(tr("ui.image_manager_title"))
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        self.images = QListWidget()
        self.images.setObjectName("projectImages")
        self.images.setIconSize(QSize(64, 64))
        layout.addWidget(self.images)
        actions = QHBoxLayout()
        self.add_button = QPushButton(tr("ui.image_add"))
        self.replace_button = QPushButton(tr("ui.image_replace"))
        self.folder_button = QPushButton(tr("ui.image_import_folder"))
        self.refresh_button = QPushButton(tr("ui.refresh"))
        for button in (self.add_button, self.replace_button, self.folder_button, self.refresh_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.import_progress = QLabel()
        self.import_progress.setObjectName("imageImportProgress")
        layout.addWidget(self.import_progress)
        self.add_button.clicked.connect(self._add_image)
        self.replace_button.clicked.connect(self._replace_image)
        self.folder_button.clicked.connect(self._import_folder)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.images.clear()
        try:
            paths = ResourceRepository.list_images(self.root)
            for path in paths:
                relative = path.relative_to(self.root).as_posix()
                image = QPixmap(str(path))
                size = f"{image.width()}×{image.height()} px" if not image.isNull() else tr("ui.image_unreadable")
                item = QListWidgetItem(f"{path.name} — {relative} — {size}")
                item.setData(Qt.ItemDataRole.UserRole, relative)
                if not image.isNull():
                    item.setIcon(QIcon(QPixmap(image.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))))
                self.images.addItem(item)
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_manager_title"), format_error(exc))
        self.replace_button.setEnabled(self.images.count() > 0)

    def _destination_folder(self) -> str | None:
        folder, accepted = QInputDialog.getText(self, tr("ui.image_destination"), tr("ui.image_subfolder"))
        return folder.replace("\\", "/").strip("/") if accepted else None

    def _add_image(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, tr("ui.image_add_title"), "", tr("ui.image_filter"))
        if not source:
            return
        folder = self._destination_folder()
        if folder is None:
            return
        try:
            self.importer(Path(source), folder)
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_add_failed"), format_error(exc))

    def _replace_image(self) -> None:
        item = self.images.currentItem()
        if item is None:
            return
        source, _ = QFileDialog.getOpenFileName(self, tr("ui.image_replace_title"), "", tr("ui.image_filter"))
        if not source:
            return
        relative = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, tr("ui.image_replace_confirm_title"), tr("ui.image_manager_confirm", path=relative)) != QMessageBox.StandardButton.Yes:
            return
        try:
            target = resolve_project_path(self.root, relative)
            self.replacer(relative, Path(source), file_hash(target))
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, tr("ui.image_replace_failed"), format_error(exc))

    def _import_folder(self) -> None:
        source = QFileDialog.getExistingDirectory(self, tr("ui.image_choose_folder"))
        if not source:
            return
        folder = self._destination_folder()
        if folder is not None:
            self.folderImportRequested.emit(Path(source), folder)

    def set_import_progress(self, completed: int, total: int, message: str = "") -> None:
        self.import_progress.setText(message or tr("ui.import_progress", completed=completed, total=total))

    def finish_import(self, paths: tuple[str, ...] | None, error: Exception | None) -> None:
        self.folder_button.setEnabled(True)
        if error is not None:
            self.set_import_progress(0, 0, "")
            QMessageBox.warning(self, tr("ui.image_import_title"), format_error(error))
            self.refresh()
            return
        self.refresh()
        self.set_import_progress(len(paths or ()), len(paths or ()), tr("ui.image_import_complete", count=len(paths or ())))

    def _relative(self, path: Path) -> str:
        return PurePosixPath(path.relative_to(self.root)).as_posix()

    def _accept_selection(self) -> None:
        path = Path(self.model.filePath(self.tree.currentIndex()))
        if not path.is_file():
            QMessageBox.information(self, tr("ui.image_select_title"), tr("ui.image_select_format"))
            return
        self.selected_path = self._relative(path)
        self.accept()

    def _import(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, tr("ui.image_import_one"), "", tr("ui.image_filter"))
        if not source:
            return
        folder, accepted = QInputDialog.getText(
            self,
            tr("ui.image_resource_folder"),
            tr("ui.image_resource_subfolder"),
        )
        if not accepted:
            return
        try:
            self.selected_path = self.importer(Path(source), folder.strip().replace("\\", "/"))
        except Exception as exc:
            QMessageBox.critical(self, tr("ui.image_import_error"), format_error(exc))
            return
        self.accept()


class ImageAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None, *, columns: tuple[str, ...] = ()):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.setWindowTitle(tr("ui.image_new"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(tr("ui.image"))
        self.name.setObjectName("newImageName")
        self.content_mode = QComboBox()
        self.content_mode.addItem(tr("ui.image_project_file"), "manual")
        self.content_mode.addItem(tr("ui.excel_column"), "column")
        self.content_mode.setEnabled(bool(columns))
        self.content_column = QComboBox()
        self.content_column.addItems(list(columns))
        row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setObjectName("newImageSource")
        choose = QPushButton(tr("ui.choose"))
        row.addWidget(self.source, 1)
        row.addWidget(choose)
        self.fit = QComboBox()
        self.fit.addItems(["contain", "cover", "stretch"])
        form.addRow(tr("ui.name"), self.name)
        form.addRow(tr("ui.source"), self.content_mode)
        form.addRow(tr("ui.image_resource"), row)
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

    def _choose(self) -> None:
        if self.content_mode.currentData() != "manual":
            return
        picker = ResourcePickerDialog(self.root, self.importer, self)
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


class HtmlAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None, *, columns: tuple[str, ...] = ()):
        super().__init__(parent)
        self.root = root
        self.importer = importer
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
        self.font = QComboBox()
        self.font.setObjectName("newHtmlFont")
        self.font.addItems(available_font_families(root))
        if self.font.findText("Arial") >= 0:
            self.font.setCurrentText("Arial")
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(0.1, 1000)
        self.font_size.setDecimals(1)
        self.font_size.setSingleStep(1.0)
        self.font_size.setValue(10)
        self.font_size.setObjectName("newHtmlFontSize")
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
        picker = ResourcePickerDialog(self.root, self.importer, self)
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
