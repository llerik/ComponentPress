"""Accessible two-state switch for the current copy mode."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionFocusRect
from componentpress.i18n import tr


class CopyModeSwitch(QCheckBox):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("dataMode")
        self.setAccessibleName(tr("ui.copy_mode"))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._labels = (tr("ui.copy_mode_prod"), tr("ui.copy_mode_test"))
        self._label_width = max(self.fontMetrics().horizontalAdvance(label) for label in self._labels)
        self.setText(self._labels[0])
        self.setToolTip(tr("ui.copy_mode_value", mode=self._labels[0]))
        self.setAccessibleDescription(tr("ui.copy_mode_value", mode=self._labels[0]))
        self.toggled.connect(self._update_label)

    @property
    def mode(self) -> str:
        return "test" if self.isChecked() else "prod"

    def sizeHint(self) -> QSize:
        return QSize(60 + self._label_width, 30)

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
        label = self._labels[1 if checked else 0]
        self.setText(label)
        self.setToolTip(tr("ui.copy_mode_value", mode=label))
        self.setAccessibleDescription(tr("ui.copy_mode_value", mode=label))
        self.updateGeometry()
        self.update()
