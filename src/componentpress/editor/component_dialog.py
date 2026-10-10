"""Component creation and properties dialog used by the compact editor."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLineEdit, QVBoxLayout,
)

from componentpress.domain.component import ComponentDefinition, DataBinding


PRESETS: tuple[tuple[str, float, float], ...] = (
    ("Poker / MTG — 63 × 88 мм", 63, 88),
    ("Bridge / Standard American — 57 × 89 мм", 57, 89),
    ("European — 60 × 92 мм", 60, 92),
    ("Mini Euro — 44 × 69 мм", 44, 69),
    ("Mini American — 42 × 65 мм", 42, 65),
    ("Tarot — 71 × 120 мм", 71, 120),
    ("Квадрат — 45 × 45 мм", 45, 45),
    ("Квадрат — 50 × 50 мм", 50, 50),
    *((f"Жетон — {size} × {size} мм", size, size) for size in (15, 20, 25, 30, 45, 50)),
)


class ComponentDialog(QDialog):
    def __init__(self, parent=None, *, component: ComponentDefinition | None = None, sheets: tuple[str, ...] = ()):
        super().__init__(parent)
        self.setWindowTitle("Свойства компонента" if component else "Новый компонент")
        self.setMinimumWidth(360)
        self.name = QLineEdit(component.name if component else "")
        self.name.setObjectName("componentName")
        self.sheet = QComboBox()
        self.sheet.setObjectName("componentSheet")
        self.sheet.addItem("Без привязки к Excel", None)
        binding = component.data.sheet if component and component.data else None
        for sheet in sheets:
            self.sheet.addItem(sheet, sheet)
        if binding and self.sheet.findData(binding) < 0:
            self.sheet.addItem(f"{binding} (не найден)", binding)
        self.sheet.setCurrentIndex(max(0, self.sheet.findData(binding)))
        self.size = QComboBox()
        self.size.setObjectName("componentPreset")
        self.size.addItem("Свой размер…", None)
        selected_index = 0
        for label, width, height in PRESETS:
            self.size.addItem(label, (width, height))
            if component and component.size_mm.width == width and component.size_mm.height == height:
                selected_index = self.size.count() - 1
        self.width = self._millimetres("componentWidth", component.size_mm.width if component else 63)
        self.height = self._millimetres("componentHeight", component.size_mm.height if component else 88)
        if component is None:
            self.width.setDecimals(0)
            self.height.setDecimals(0)
        form = QFormLayout()
        form.addRow("Название", self.name)
        form.addRow("Лист Excel", self.sheet)
        form.addRow("Размер", self.size)
        form.addRow("Ширина, мм", self.width)
        form.addRow("Высота, мм", self.height)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.size.currentIndexChanged.connect(self._preset_changed)
        self.name.setFocus()
        if selected_index:
            self.size.setCurrentIndex(selected_index)

    @staticmethod
    def _millimetres(name: str, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setRange(0.1, 10000)
        spin.setDecimals(3)
        spin.setSingleStep(1)
        spin.setSuffix(" мм")
        spin.setValue(value)
        return spin

    def _preset_changed(self, index: int) -> None:
        size = self.size.itemData(index)
        custom = size is None
        self.width.setEnabled(custom)
        self.height.setEnabled(custom)
        if size is not None:
            self.width.setValue(size[0])
            self.height.setValue(size[1])

    def definition(self, component: ComponentDefinition | None = None) -> dict:
        result = {"name": self.name.text().strip(), "width": self.width.value(), "height": self.height.value()}
        sheet = self.sheet.currentData()
        if sheet:
            result["data"] = component.data.model_copy(update={"sheet": sheet}) if component and component.data else DataBinding(sheet=sheet)
        else:
            result["data"] = None
        return result
