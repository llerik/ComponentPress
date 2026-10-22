from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QLabel, QScrollArea
from componentpress.application.contracts import BuildResult
from componentpress.domain.project import PrintSettings
from componentpress.editor.build_dialog import BuildDialog
from componentpress.editor.main_window import MainWindow
from componentpress.editor.program_settings_dialog import ProgramSettingsDialog
from componentpress.i18n import set_language, validate_catalogs
from componentpress.platforms.services import PlatformServices


@pytest.mark.parametrize("locale", ("ru", "en"))
def test_build_dialog_shows_settings_mode_scope_and_after_action(qtbot, locale):
    set_language(locale)
    validate_catalogs()
    dialog = BuildDialog(PrintSettings(dpi=450), "test", "Forest card")
    qtbot.addWidget(dialog)
    started, cancelled = [], []
    dialog.startRequested.connect(lambda: started.append(True))
    dialog.cancelRequested.connect(lambda: cancelled.append(True))
    assert "450" in dialog.settings_label.text()
    assert "Forest card" in dialog.scope_label.text()
    assert dialog.after_action == "none"
    dialog.action_button.menu().actions()[0].trigger()
    assert dialog.after_action == "pdf"
    dialog.start_button.click()
    assert started == [True]
    dialog.begin()
    dialog.start_button.click()
    assert cancelled == [True]
    dialog.finish()
    assert not dialog.isVisible()


def test_program_settings_are_grouped_and_print_scope_is_explicit(qtbot):
    from componentpress.user_preferences import ProgramPreferences

    dialog = ProgramSettingsDialog(ProgramPreferences(), lambda: None)
    qtbot.addWidget(dialog)
    assert {group.title() for group in dialog.findChildren(QGroupBox)} == {
        "Интерфейс", "Печать", "Производительность", "Интеграция с Windows",
    }
    assert any("всем проектам" in label.text() for label in dialog.findChildren(QLabel))


@pytest.mark.parametrize(
    ("locale", "language_label", "restart_tip", "zoom_label"),
    (
        ("ru", "Язык интерфейса", "Новый язык интерфейса включится после перезапуска ComponentPress.", "Масштаб 100%"),
        ("en", "Interface language", "The interface language will change after restarting ComponentPress.", "100% zoom"),
    ),
)
def test_program_settings_language_tooltip_and_width(qtbot, locale, language_label, restart_tip, zoom_label):
    from componentpress.i18n import tr
    from componentpress.user_preferences import ProgramPreferences
    from componentpress.editor.theme import apply_theme, theme_palette

    set_language(locale)
    validate_catalogs()
    apply_theme("dark")
    dialog = ProgramSettingsDialog(ProgramPreferences(), lambda: None)
    qtbot.addWidget(dialog)
    assert dialog.layout().itemAt(0).widget().horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    dialog.show()
    qtbot.wait(10)
    assert dialog.minimumWidth() >= dialog.sizeHint().width()
    assert dialog.layout().itemAt(0).widget().horizontalScrollBar().maximum() == 0
    minimum = dialog.minimumWidth()
    dialog.resize(minimum - 1, dialog.height())
    assert dialog.width() >= minimum
    assert dialog.findChild(QScrollArea).horizontalScrollBar().maximum() == 0

    assert dialog.layout().itemAt(0).widget().widget().findChildren(QGroupBox)
    interface_group = next(group for group in dialog.findChildren(QGroupBox) if group.title() == tr("ui.settings_interface"))
    assert interface_group.layout().labelForField(dialog.language).text() == language_label
    assert not any(restart_tip in label.text() for label in dialog.findChildren(QLabel))
    dialog.language.setCurrentIndex(1)
    dialog.language.activated.emit(1)
    qtbot.wait(10)
    assert dialog._language_tooltip.text() == restart_tip
    image = dialog._language_tooltip.grab().toImage()
    colors = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
    assert colors.count(theme_palette().color(theme_palette().ColorRole.ToolTipBase)) > colors.count(
        theme_palette().color(theme_palette().ColorRole.ToolTipText)
    )
    qtbot.waitUntil(lambda: not dialog._language_tooltip.isVisible(), timeout=2500)
    assert tr("ui.zoom_actual") == zoom_label
    assert "96" in tr("ui.zoom_actual_tip")

    dialog.close()


def _window(tmp_path, qtbot, monkeypatch, action="pdf"):
    from test_stage14_ui import make_window

    _root, window, _service = make_window(tmp_path, qtbot)
    opened = []
    monkeypatch.setattr(window, "save_all", lambda: True)
    window.controller.build = SimpleNamespace(cancel=lambda _job: True)
    monkeypatch.setattr(window.controller, "start_build", lambda _request, **_kwargs: "build-job")
    monkeypatch.setattr(PlatformServices, "open_path", lambda path: opened.append(path) or True)
    window._start_build(False)
    dialog = window._build_progress
    menu_action = next(item for item in dialog.action_button.menu().actions() if item.data() == action)
    menu_action.trigger()
    dialog.start_button.click()
    return window, dialog, opened


def test_success_closes_dialog_then_opens_selected_pdf_once(tmp_path, qtbot, monkeypatch):
    window, dialog, opened = _window(tmp_path, qtbot, monkeypatch)
    pdf = tmp_path / "print.pdf"
    pdf.write_bytes(b"pdf")
    window._on_build_finished(BuildResult("build-job", "succeeded", pdf_path=pdf, output_directory=tmp_path))
    assert not dialog.isVisible()
    qtbot.wait(10)
    assert opened == [pdf]
    window.close()


def test_failed_build_closes_dialog_without_opening(tmp_path, qtbot, monkeypatch):
    window, dialog, opened = _window(tmp_path, qtbot, monkeypatch, action="folder")
    window._on_build_finished(BuildResult("build-job", "failed", output_directory=tmp_path))
    assert not dialog.isVisible()
    assert opened == []
    window.close()


def test_user_close_cancels_and_suppresses_late_success_action(tmp_path, qtbot, monkeypatch):
    window, dialog, opened = _window(tmp_path, qtbot, monkeypatch)
    cancels = []
    monkeypatch.setattr(window.controller, "cancel_build", lambda job: cancels.append(job) or False)
    dialog.close()
    assert cancels == ["build-job"]
    pdf = tmp_path / "print.pdf"
    pdf.write_bytes(b"pdf")
    window._on_build_finished(BuildResult("build-job", "succeeded", pdf_path=pdf, output_directory=tmp_path))
    qtbot.wait(10)
    assert opened == []
    assert not dialog.isVisible()
    window.close()
