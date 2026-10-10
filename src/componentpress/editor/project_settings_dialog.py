"""Validated project-wide settings editor."""

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from componentpress.application.sessions import ProjectSession


class ProjectSettingsDialog(QDialog):
    def __init__(self, session: ProjectSession, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки проекта")
        self._picked_file: Path | None = None
        model = session.snapshot.model
        self.name = QLineEdit(model.name)
        self.version = QLineEdit(model.version)
        self.prod = QLineEdit(model.copies_columns.prod)
        self.test = QLineEdit(model.copies_columns.test)
        self.source = QLineEdit(model.data_source.path if model.data_source else "")
        self.source.setReadOnly(True)
        choose = QPushButton("Выбрать…")
        clear = QPushButton("Очистить")
        choose.clicked.connect(self._choose_file)
        clear.clicked.connect(self._clear_source)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source, 1)
        source_layout.addWidget(choose)
        source_layout.addWidget(clear)
        form = QFormLayout()
        form.addRow("Название", self.name)
        form.addRow("Версия", self.version)
        form.addRow("Книга Excel", source_row)
        form.addRow("Столбец тиража Prod", self.prod)
        form.addRow("Столбец тиража Test", self.test)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Изменения сохраняются после нажатия «ОК»."))
        layout.addWidget(buttons)

    def _choose_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Выбрать книгу Excel", "", "Excel (*.xlsx)")
        if filename:
            self._picked_file = Path(filename)
            self.source.setText(self._picked_file.name)

    def _clear_source(self) -> None:
        self._picked_file = None
        self.source.clear()

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(), "version": self.version.text().strip(),
            "prod": self.prod.text().strip(), "test": self.test.text().strip(),
            "source": self.source.text().strip(), "source_file": self._picked_file,
        }
