"""Numeric editors without stepping controls or trailing zeroes."""

from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox


class NumericSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setKeyboardTracking(False)

    def textFromValue(self, value: float) -> str:
        text = self.locale().toString(value, "f", self.decimals())
        point = self.locale().decimalPoint()
        return text.rstrip("0").rstrip(point) if point in text else text

    def wheelEvent(self, event) -> None:
        event.ignore()
