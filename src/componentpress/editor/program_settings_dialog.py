"""Program-wide preferences dialog."""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QPushButton, QScrollArea, QSpinBox, QToolTip, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QPoint, Qt

from componentpress.domain.project import PrintSettings
from componentpress.i18n import tr
from componentpress.user_preferences import ProgramPreferences


class ProgramSettingsDialog(QDialog):
    def __init__(self, preferences: ProgramPreferences, on_register, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("ui.program_settings"))
        layout = QVBoxLayout(self)
        layout.addWidget(self._settings_scroll(preferences, on_register), 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _settings_scroll(self, preferences, on_register) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        self._add_interface_settings(content_layout, preferences)
        self._add_print_settings(content_layout, preferences.print_settings)
        self._add_performance_settings(content_layout, preferences.png_workers)
        self._add_windows_settings(content_layout, on_register)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        scroll.setMinimumWidth(
            content.sizeHint().width() + 2 * scroll.frameWidth() + scroll.verticalScrollBar().sizeHint().width()
        )
        return scroll

    def _add_interface_settings(self, content_layout, preferences):
        self.language = QComboBox()
        self.language.setObjectName("programLanguage")
        self.language.addItem(tr("language.ru"), "ru")
        self.language.addItem(tr("language.en"), "en")
        self.language.setCurrentIndex(0 if preferences.language == "ru" else 1)
        self.language.activated.connect(self._show_language_tooltip)
        self.theme = QComboBox()
        self.theme.setObjectName("programTheme")
        for value in ("system", "light", "dark"):
            self.theme.addItem(tr(f"ui.theme.{value}"), value)
        self.theme.setCurrentIndex(self.theme.findData(preferences.theme))

        group = QGroupBox(tr("ui.settings_interface"))
        form = QFormLayout(group)
        form.addRow(tr("about.language"), self.language)
        form.addRow(tr("ui.theme"), self.theme)
        content_layout.addWidget(group)

    def _show_language_tooltip(self, _index: int) -> None:
        point = self.language.mapToGlobal(QPoint(0, self.language.height()))
        QToolTip.showText(point, tr("ui.language_restart"), self.language)

    def _add_print_settings(self, content_layout, settings):
        self._create_print_widgets(settings)
        self._layout_print_settings(content_layout)

    def _create_print_widgets(self, settings):
        self.paper = QComboBox()
        self.paper.addItems(("A4", "Letter"))
        self.paper.setCurrentText(settings.paper)
        self.orientation = QComboBox()
        self.orientation.addItem(tr("ui.portrait"), "portrait")
        self.orientation.addItem(tr("ui.landscape"), "landscape")
        self.orientation.setCurrentIndex(0 if settings.orientation == "portrait" else 1)
        self.margin = self._decimal(settings.margin_mm, 0, 1000)
        self.gap = self._decimal(settings.gap_mm, 0, 1000)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 2400)
        self.dpi.setValue(settings.dpi)
        self.cut_lines = QComboBox()
        self.cut_lines.addItem(tr("ui.enabled"), True)
        self.cut_lines.addItem(tr("ui.disabled"), False)
        self.cut_lines.setCurrentIndex(0 if settings.cut_lines else 1)
        self.cut_width = self._decimal(settings.cut_line_width_mm, 0.01, 10)

    def _layout_print_settings(self, content_layout):
        group = QGroupBox(tr("ui.settings_print"))
        group_form = QFormLayout(group)
        for key, widget in (
            ("ui.paper", self.paper), ("ui.orientation", self.orientation),
            ("ui.margins", self.margin), ("ui.gap", self.gap), ("ui.dpi", self.dpi),
            ("ui.cut_lines", self.cut_lines), ("ui.cut_line_width", self.cut_width),
        ):
            group_form.addRow(tr(key), widget)
        group_form.addRow(QLabel(tr("ui.print_settings_global")))
        content_layout.addWidget(group)

    def _add_performance_settings(self, content_layout, workers):
        self.workers = QSpinBox()
        self.workers.setRange(1, 64)
        self.workers.setValue(workers)
        group = QGroupBox(tr("ui.settings_performance"))
        QFormLayout(group).addRow(tr("ui.png_workers"), self.workers)
        content_layout.addWidget(group)

    def _add_windows_settings(self, content_layout, on_register):
        integration = QGroupBox(tr("ui.settings_windows"))
        integration_layout = QVBoxLayout(integration)
        registration = QPushButton(tr("ui.register_cpress"))
        registration.clicked.connect(on_register)
        integration_layout.addWidget(QLabel(tr("ui.registration_separate")))
        integration_layout.addWidget(registration)
        content_layout.addWidget(integration)

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
