"""Component creation and properties dialog used by the compact editor."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLineEdit, QToolButton, QVBoxLayout,
)

from componentpress.i18n import tr
from componentpress.domain.component import ComponentDefinition, DataBinding
from componentpress.app_assets import set_themed_icon
from componentpress.editor.numeric import NumericSpinBox


PRESETS: tuple[tuple[str, float, float], ...] = (
    ("ui.preset_poker", 63, 88),
    ("ui.preset_bridge", 57, 89),
    ("ui.preset_european", 60, 92),
    ("ui.preset_mini_euro", 44, 69),
    ("ui.preset_mini_american", 42, 65),
    ("ui.preset_tarot", 71, 120),
    ("ui.component_preset_square", 45, 45),
    ("ui.component_preset_square", 50, 50),
    *(("ui.component_preset_token", size, size) for size in (15, 20, 25, 30, 45, 50)),
)


class ComponentDialog(QDialog):
    def __init__(self, parent=None, *, component: ComponentDefinition | None = None, sheets: tuple[str, ...] = ()):
        super().__init__(parent)
        self.setWindowTitle(tr("ui.component_properties") if component else tr("ui.component_new"))
        self.setMinimumWidth(360)
        self.name = QLineEdit(component.name if component else "")
        self.name.setObjectName("componentName")
        self.sheet = QComboBox()
        self.sheet.setObjectName("componentSheet")
        self.sheet.addItem(tr("ui.no_sheet"), None)
        binding = component.data.sheet if component and component.data else None
        for sheet in sheets:
            self.sheet.addItem(sheet, sheet)
        if binding and self.sheet.findData(binding) < 0:
            self.sheet.addItem(tr("ui.component_sheet_missing", sheet=binding), binding)
        self.sheet.setCurrentIndex(max(0, self.sheet.findData(binding)))
        self.size = QComboBox()
        self.size.setObjectName("componentPreset")
        self.size.addItem(tr("ui.custom_size"), None)
        selected_index = 0
        for label, width, height in PRESETS:
            if label.startswith("ui."):
                label = tr(label, size=int(width))
            self.size.addItem(label, (width, height))
            if component and component.size_mm.width == width and component.size_mm.height == height:
                selected_index = self.size.count() - 1
        self.width = self._millimetres("componentWidth", component.size_mm.width if component else 63)
        self.height = self._millimetres("componentHeight", component.size_mm.height if component else 88)
        if component is None:
            self.width.setDecimals(0)
            self.height.setDecimals(0)
        form = QFormLayout()
        form.addRow(tr("ui.name"), self.name)
        form.addRow(tr("ui.component_sheet"), self.sheet)
        form.addRow(tr("ui.component_size"), self.size)
        form.addRow(tr("ui.width_mm"), self._dimension_row())
        form.addRow(tr("ui.height_mm"), self.height)
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

    def _dimension_row(self) -> QHBoxLayout:
        self.swap_dimensions = QToolButton()
        self.swap_dimensions.setObjectName("swapComponentDimensions")
        set_themed_icon(self.swap_dimensions, "rotation.png")
        self.swap_dimensions.setToolTip(tr("ui.swap_dimensions"))
        self.swap_dimensions.setAccessibleName(tr("ui.swap_dimensions"))
        self.swap_dimensions.clicked.connect(self._swap_dimensions)
        row = QHBoxLayout()
        row.addWidget(self.width, 1)
        row.addWidget(self.swap_dimensions)
        return row

    @staticmethod
    def _millimetres(name: str, value: float) -> NumericSpinBox:
        spin = NumericSpinBox()
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

    def _swap_dimensions(self) -> None:
        width, height = self.width.value(), self.height.value()
        self.size.setCurrentIndex(0)
        self.width.setValue(height)
        self.height.setValue(width)

    def definition(self, component: ComponentDefinition | None = None) -> dict:
        result = {"name": self.name.text().strip(), "width": self.width.value(), "height": self.height.value()}
        sheet = self.sheet.currentData()
        if sheet:
            result["data"] = component.data.model_copy(update={"sheet": sheet}) if component and component.data else DataBinding(sheet=sheet)
        else:
            result["data"] = None
        return result
