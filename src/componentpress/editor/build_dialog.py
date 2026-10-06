from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QSpinBox,
)

from componentpress.domain.project import PrintSettings


class BuildOptionsDialog(QDialog):
    def __init__(self, settings: PrintSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки печатной сборки")
        layout = QFormLayout(self)
        self.paper = QComboBox()
        self.paper.addItems(["A4", "Letter"])
        self.paper.setCurrentText(settings.paper)
        self.orientation = QComboBox()
        self.orientation.addItem("Книжная", "portrait")
        self.orientation.addItem("Альбомная", "landscape")
        self.orientation.setCurrentIndex(0 if settings.orientation == "portrait" else 1)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 1000)
        self.margin.setDecimals(2)
        self.margin.setValue(settings.margin_mm)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0, 1000)
        self.gap.setDecimals(2)
        self.gap.setValue(settings.gap_mm)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 2400)
        self.dpi.setValue(settings.dpi)
        self.cut_width = QDoubleSpinBox()
        self.cut_width.setRange(0.01, 10)
        self.cut_width.setDecimals(2)
        self.cut_width.setValue(settings.cut_line_width_mm)
        self.workers = QSpinBox()
        self.workers.setRange(1, 64)
        self.workers.setValue(2)
        for label, widget in (
            ("Бумага", self.paper),
            ("Ориентация", self.orientation),
            ("Поля, мм", self.margin),
            ("Зазор, мм", self.gap),
            ("DPI", self.dpi),
            ("Толщина линий реза, мм", self.cut_width),
            ("Потоки PNG", self.workers),
        ):
            layout.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def print_settings(self) -> PrintSettings:
        return PrintSettings(
            paper=self.paper.currentText(),
            orientation=self.orientation.currentData(),
            margin_mm=self.margin.value(),
            gap_mm=self.gap.value(),
            dpi=self.dpi.value(),
            cut_lines=True,
            cut_line_width_mm=self.cut_width.value(),
        )
