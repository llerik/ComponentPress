"""Program-wide preferences dialog."""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLabel, QPushButton, QSpinBox, QVBoxLayout,
)

from componentpress.domain.project import PrintSettings
from componentpress.i18n import tr
from componentpress.user_preferences import ProgramPreferences


class ProgramSettingsDialog(QDialog):
    def __init__(self, preferences: ProgramPreferences, on_register, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("ui.program_settings"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.language = QComboBox()
        self.language.setObjectName("programLanguage")
        self.language.addItem(tr("language.ru"), "ru")
        self.language.addItem(tr("language.en"), "en")
        self.language.setCurrentIndex(0 if preferences.language == "ru" else 1)
        self.theme = QComboBox()
        self.theme.setObjectName("programTheme")
        for value in ("system", "light", "dark"):
            self.theme.addItem(tr(f"ui.theme.{value}"), value)
        self.theme.setCurrentIndex(self.theme.findData(preferences.theme))
        self.paper = QComboBox()
        self.paper.addItems(("A4", "Letter"))
        self.paper.setCurrentText(preferences.print_settings.paper)
        self.orientation = QComboBox()
        self.orientation.addItem(tr("ui.portrait"), "portrait")
        self.orientation.addItem(tr("ui.landscape"), "landscape")
        self.orientation.setCurrentIndex(0 if preferences.print_settings.orientation == "portrait" else 1)
        self.margin = self._decimal(preferences.print_settings.margin_mm, 0, 1000)
        self.gap = self._decimal(preferences.print_settings.gap_mm, 0, 1000)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 2400)
        self.dpi.setValue(preferences.print_settings.dpi)
        self.cut_lines = QComboBox()
        self.cut_lines.addItem(tr("ui.enabled"), True)
        self.cut_lines.addItem(tr("ui.disabled"), False)
        self.cut_lines.setCurrentIndex(0 if preferences.print_settings.cut_lines else 1)
        self.cut_width = self._decimal(preferences.print_settings.cut_line_width_mm, 0.01, 10)
        self.workers = QSpinBox()
        self.workers.setRange(1, 64)
        self.workers.setValue(preferences.png_workers)
        for key, widget in (
            ("ui.language", self.language), ("ui.theme", self.theme),
            ("ui.paper", self.paper), ("ui.orientation", self.orientation),
            ("ui.margins", self.margin), ("ui.gap", self.gap), ("ui.dpi", self.dpi),
            ("ui.cut_lines", self.cut_lines), ("ui.cut_line_width", self.cut_width),
            ("ui.png_workers", self.workers),
        ):
            form.addRow(tr(key), widget)
        layout.addLayout(form)
        layout.addWidget(QLabel(tr("ui.language_restart")))
        layout.addWidget(QLabel(tr("ui.registration_separate")))
        registration = QPushButton(tr("ui.register_cpress"))
        registration.clicked.connect(on_register)
        layout.addWidget(registration)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _decimal(value: float, low: float, high: float) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(low, high)
        widget.setDecimals(2)
        widget.setValue(value)
        return widget

    def preferences(self) -> ProgramPreferences:
        settings = PrintSettings(
            paper=self.paper.currentText(), orientation=self.orientation.currentData(),
            margin_mm=self.margin.value(), gap_mm=self.gap.value(), dpi=self.dpi.value(),
            cut_lines=self.cut_lines.currentData(), cut_line_width_mm=self.cut_width.value(),
        )
        return ProgramPreferences(
            language=self.language.currentData(), theme=self.theme.currentData(),
            print_settings=settings, png_workers=self.workers.value(),
        )
