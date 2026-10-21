"""Settings and progress for one user-requested project build."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QMenu, QProgressBar,
    QPushButton, QToolButton, QVBoxLayout,
)

from componentpress.application.contracts import BuildProgress
from componentpress.domain.project import PrintSettings
from componentpress.i18n import tr


class BuildDialog(QDialog):
    startRequested = Signal()
    cancelRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, settings: PrintSettings, mode: str, scope: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("ui.build_dialog_title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._terminal = False
        self._closing = False
        self._running = False
        layout = QVBoxLayout(self)
        self._add_build_summary(layout, mode, scope)
        self._add_progress(layout)
        self._add_after_action(layout)
        self._add_start_button(layout)
        self.update_settings(settings)

    def _add_build_summary(self, layout, mode, scope):
        form = QFormLayout()
        self.settings_label = QLabel(self)
        self.settings_label.setWordWrap(True)
        self.mode_label = QLabel(tr("ui.build_mode_test" if mode == "test" else "ui.build_mode_prod"))
        self.scope_label = QLabel(scope)
        form.addRow(tr("ui.build_print_settings"), self.settings_label)
        form.addRow(tr("ui.build_mode"), self.mode_label)
        form.addRow(tr("ui.build_scope"), self.scope_label)
        layout.addLayout(form)
        self.settings_button = QPushButton(tr("ui.build_change_settings"))
        self.settings_button.clicked.connect(self.settingsRequested)
        layout.addWidget(self.settings_button)

    def _add_progress(self, layout):
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel(tr("ui.build_ready"), self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _add_after_action(self, layout):
        self.action_button = QToolButton(self)
        self.action_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.action_button.setText(tr("ui.build_after_none"))
        self.action_button.setAccessibleName(tr("ui.build_after_accessible", action=self.action_button.text()))
        menu = QMenu(self.action_button)
        for data, key in (("pdf", "ui.build_after_pdf"), ("folder", "ui.build_after_folder"), ("none", "ui.build_after_none")):
            action = menu.addAction(tr(key))
            action.setCheckable(True)
            action.setData(data)
            action.triggered.connect(lambda _checked=False, value=data, label=action.text(): self._select_action(value, label))
        menu.actions()[-1].setChecked(True)
        self.action_button.setMenu(menu)
        self.after_action = "none"
        layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignLeft)

    def _add_start_button(self, layout):
        buttons = QHBoxLayout()
        self.start_button = QPushButton(tr("ui.build_start"), self)
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self.start_button.clicked.connect(self._activate_button)

    def _activate_button(self) -> None:
        if self._running:
            self.cancelRequested.emit()
        else:
            self.startRequested.emit()

    def _select_action(self, value: str, label: str) -> None:
        self.after_action = value
        for action in self.action_button.menu().actions():
            action.setChecked(action.data() == value)
        self.action_button.setText(label)
        self.action_button.setAccessibleName(tr("ui.build_after_accessible", action=label))

    def update_settings(self, settings: PrintSettings) -> None:
        self.settings_label.setText(tr(
            "ui.build_print_summary", paper=settings.paper,
            orientation=tr("ui.portrait" if settings.orientation == "portrait" else "ui.landscape"),
            dpi=settings.dpi, margin=settings.margin_mm, gap=settings.gap_mm,
            cut=tr("ui.enabled" if settings.cut_lines else "ui.disabled"),
            width=settings.cut_line_width_mm,
        ))

    def begin(self) -> None:
        self._running = True
        self.start_button.setText(tr("ui.build_cancel"))
        self.settings_button.setEnabled(False)
        self.action_button.setEnabled(False)
        self.progress.show()
        self.progress.setRange(0, 0)
        self.status.setText(tr("ui.build_waiting"))

    def update_progress(self, value: BuildProgress) -> None:
        self.status.setText(value.message or tr(f"ui.build_phase_{value.phase}"))
        if value.total > 0:
            self.progress.setRange(0, value.total)
            self.progress.setValue(min(value.completed, value.total))
        else:
            self.progress.setRange(0, 0)

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def cancelling(self, message: str) -> None:
        self.start_button.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.action_button.setEnabled(False)
        self.status.setText(message)

    def finish(self) -> str:
        self._terminal = True
        self._closing = True
        action = self.after_action
        self.close()
        return action

    def closeEvent(self, event) -> None:
        if self._terminal:
            event.accept()
        elif not self._running:
            event.accept()
        else:
            event.ignore()
            if not self._closing:
                self._closing = True
                self.cancelRequested.emit()

    def reject(self) -> None:
        if self._terminal:
            return super().reject()
        if not self._running:
            return super().reject()
        if not self._closing:
            self._closing = True
            self.cancelRequested.emit()
