"""Element editors and the nested project resource picker."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from collections.abc import Callable

from PySide6.QtCore import QDir
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)


class ResourcePickerDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.selected_path = ""
        self.setWindowTitle("Изображение проекта")
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
        import_button = QPushButton("Импортировать PNG/JPEG…")
        layout.addWidget(import_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        import_button.clicked.connect(self._import)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        self.tree.doubleClicked.connect(lambda _index: self._accept_selection())

    def _relative(self, path: Path) -> str:
        return PurePosixPath(path.relative_to(self.root)).as_posix()

    def _accept_selection(self) -> None:
        path = Path(self.model.filePath(self.tree.currentIndex()))
        if not path.is_file():
            QMessageBox.information(self, "Выбор изображения", "Выберите PNG или JPEG.")
            return
        self.selected_path = self._relative(path)
        self.accept()

    def _import(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Импорт изображения", "", "Изображения (*.png *.jpg *.jpeg)")
        if not source:
            return
        folder, accepted = QInputDialog.getText(
            self,
            "Папка ресурсов",
            "Вложенная папка внутри assets/images (можно оставить пустой)",
        )
        if not accepted:
            return
        try:
            self.selected_path = self.importer(Path(source), folder.strip().replace("\\", "/"))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка импорта", str(exc))
            return
        self.accept()


class ImageAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None, *, columns: tuple[str, ...] = ()):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.setWindowTitle("Новое изображение")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("Изображение")
        self.name.setObjectName("newImageName")
        self.content_mode = QComboBox()
        self.content_mode.addItem("Файл проекта", "manual")
        self.content_mode.addItem("Столбец Excel", "column")
        self.content_mode.setEnabled(bool(columns))
        self.content_column = QComboBox()
        self.content_column.addItems(list(columns))
        row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setObjectName("newImageSource")
        choose = QPushButton("Выбрать…")
        row.addWidget(self.source, 1)
        row.addWidget(choose)
        self.fit = QComboBox()
        self.fit.addItems(["contain", "cover", "stretch"])
        form.addRow("Название", self.name)
        form.addRow("Источник", self.content_mode)
        form.addRow("Ресурс", row)
        form.addRow("Столбец", self.content_column)
        form.addRow("Режим", self.fit)
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
            QMessageBox.information(self, "Новое изображение", "Введите название.")
            return
        if self.content_mode.currentData() == "manual" and not self.source.text().strip():
            QMessageBox.information(self, "Новое изображение", "Выберите изображение.")
            return
        if self.content_mode.currentData() == "column" and not self.content_column.currentText():
            QMessageBox.information(self, "Новое изображение", "Проверьте Excel и выберите столбец.")
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
        self.setWindowTitle("Новый HTML-текст")
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("HTML-текст")
        self.name.setObjectName("newHtmlName")
        self.content_mode = QComboBox()
        self.content_mode.addItem("Ручной HTML", "manual")
        self.content_mode.addItem("Столбец Excel", "column")
        self.content_mode.setEnabled(bool(columns))
        self.content_column = QComboBox()
        self.content_column.addItems(list(columns))
        self.font = QLineEdit("Arial")
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(0.001, 1000)
        self.font_size.setValue(10)
        self.color = QLineEdit("#111111")
        form.addRow("Название", self.name)
        form.addRow("Источник", self.content_mode)
        form.addRow("Столбец", self.content_column)
        form.addRow("Шрифт", self.font)
        form.addRow("Размер, pt", self.font_size)
        form.addRow("Цвет", self.color)
        layout.addLayout(form)
        self.html = QPlainTextEdit("<p>Текст</p>")
        self.html.setObjectName("newHtmlSource")
        layout.addWidget(self.html, 1)
        insert = QPushButton("Вставить изображение…")
        layout.addWidget(insert)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        insert.clicked.connect(self._insert_image)
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
        width, accepted = QInputDialog.getInt(self, "Размер изображения", "Ширина, px", 16, 1, 4096)
        if not accepted:
            return
        height, accepted = QInputDialog.getInt(self, "Размер изображения", "Высота, px", 16, 1, 4096)
        if not accepted:
            return
        align, accepted = QInputDialog.getItem(self, "Выравнивание", "Положение", ["", "middle", "top", "bottom"], 0, False)
        if not accepted:
            return
        suffix = f' align="{align}"' if align else ""
        self.html.textCursor().insertText(
            f'<img src="{picker.selected_path}" width="{width}" height="{height}"{suffix} />'
        )

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.information(self, "Новый HTML-текст", "Введите название.")
            return
        if self.content_mode.currentData() == "column" and not self.content_column.currentText():
            QMessageBox.information(self, "Новый HTML-текст", "Проверьте Excel и выберите столбец.")
            return
        self.accept()

    def _sync_content_mode(self, _index: int = 0) -> None:
        manual = self.content_mode.currentData() == "manual"
        self.content_column.setEnabled(not manual)
        self.html.setEnabled(manual)
