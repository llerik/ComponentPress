"""Accessible two-state switch for the current copy mode."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionFocusRect
from componentpress.i18n import tr


class CopyModeSwitch(QCheckBox):
    def __init__(self, parent=None) -> None:
        super().__init__("Prod", parent)
        self.setObjectName("dataMode")
        self.setAccessibleName(tr("ui.copy_mode"))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tr("ui.copy_mode_value", mode="Prod"))
        self.setAccessibleDescription(tr("ui.copy_mode_value", mode="Prod"))
        self.toggled.connect(self._update_label)

    @property
    def mode(self) -> str:
        return "test" if self.isChecked() else "prod"

    def sizeHint(self) -> QSize:
        return QSize(60 + self.fontMetrics().horizontalAdvance(self.text()), 30)

    def hitButton(self, position) -> bool:
        return self.rect().contains(position)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self.rect().adjusted(4, 5, -self.width() + 48, -5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#8FB59A" if self.isChecked() else "#C58F8F"))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob_x = track.right() - 17 if self.isChecked() else track.left() + 1
        painter.setBrush(QColor("white"))
        painter.drawEllipse(knob_x, track.top() + 2, 16, 16)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(52, 0, self.width() - 54, self.height(), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hasFocus():
            focus = QStyleOptionFocusRect()
            focus.initFrom(self)
            focus.rect = self.rect().adjusted(1, 1, -1, -1)
            self.style().drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, focus, painter, self)

    def _update_label(self, checked: bool) -> None:
        mode = "Test" if checked else "Prod"
        self.setText(mode)
        self.setToolTip(tr("ui.copy_mode_value", mode=mode))
        self.setAccessibleDescription(tr("ui.copy_mode_value", mode=mode))
        self.updateGeometry()
        self.update()
