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
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.setWindowTitle("Новое изображение")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("Изображение")
        self.name.setObjectName("newImageName")
        row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setObjectName("newImageSource")
        choose = QPushButton("Выбрать…")
        row.addWidget(self.source, 1)
        row.addWidget(choose)
        self.fit = QComboBox()
        self.fit.addItems(["contain", "cover", "stretch"])
        form.addRow("Название", self.name)
        form.addRow("Ресурс", row)
        form.addRow("Режим", self.fit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        choose.clicked.connect(self._choose)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

    def _choose(self) -> None:
        picker = ResourcePickerDialog(self.root, self.importer, self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.source.setText(picker.selected_path)

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.information(self, "Новое изображение", "Введите название.")
            return
        if not self.source.text().strip():
            QMessageBox.information(self, "Новое изображение", "Выберите изображение.")
            return
        self.accept()


class HtmlAddDialog(QDialog):
    def __init__(self, root: Path, importer: Callable[[Path, str], str], parent=None):
        super().__init__(parent)
        self.root = root
        self.importer = importer
        self.setWindowTitle("Новый HTML-текст")
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit("HTML-текст")
        self.name.setObjectName("newHtmlName")
        self.font = QLineEdit("Arial")
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(0.001, 1000)
        self.font_size.setValue(10)
        self.color = QLineEdit("#111111")
        form.addRow("Название", self.name)
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
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

    def _insert_image(self) -> None:
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
        self.accept()
