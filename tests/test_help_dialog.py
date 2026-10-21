"""Offline help window navigation and localized menu behavior."""

import pytest
from PySide6.QtCore import QUrl, Qt
from PySide6.QtTest import QTest

from componentpress.bootstrap import project_service
from componentpress.editor.controllers import ProjectController
from componentpress.editor.main_window import MainWindow
from componentpress.editor import main_window as main_window_module
from componentpress.i18n import language, set_language, tr, validate_catalogs
from componentpress.user_preferences import ProgramPreferences


@pytest.mark.parametrize("locale", ("ru", "en"))
def test_help_menu_opens_reusable_searchable_offline_guide(qtbot, monkeypatch, tmp_path, locale):
    previous = language()
    set_language(locale)
    try:
        validate_catalogs()
        window = MainWindow(ProjectController(project_service()))
        qtbot.addWidget(window)
        window.show()
        assert window.project_menu.isEnabled()
        assert window.import_project_archive_action.isEnabled()
        window.help_action.trigger()
        dialog = window._help_dialog
        qtbot.waitUntil(dialog.isVisible)
        assert not dialog.isModal()
        assert dialog.sections.count() == 8
        dialog.search.setText("Excel")
        assert dialog.sections.count() == 1
        assert dialog.sections.item(0).text() == tr("help.data.title", locale=locale)
        assert "Excel" in dialog.browser.toPlainText()
        dialog.search.clear()
        assert dialog.sections.count() == 8
        assert dialog.sections.currentItem().text() == tr("help.data.title", locale=locale)
        dialog._open_link(QUrl("help://canvas"))
        assert dialog.sections.currentItem().text() == tr("help.canvas.title", locale=locale)
        assert dialog.back_button.isEnabled()
        dialog.back_button.click()
        assert dialog.sections.currentItem().text() == tr("help.data.title", locale=locale)
        dialog.forward_button.click()
        assert dialog.sections.currentItem().text() == tr("help.canvas.title", locale=locale)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()
        window.help_action.trigger()
        assert window._help_dialog is dialog
        assert dialog.isVisible()
    finally:
        set_language(previous)


def test_recent_projects_are_deduplicated_bounded_and_restored_without_project(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window_module, "load_preferences", lambda: ProgramPreferences())
    monkeypatch.setattr(main_window_module, "save_preferences", lambda _preferences: True)
    window = MainWindow(ProjectController(project_service()))
    qtbot.addWidget(window)
    roots = [tmp_path / f"project-{index}" for index in range(12)]
    for root in roots:
        root.mkdir()
        window._remember_project(root)
    window._remember_project(roots[-1])
    assert len(window._program_preferences.recent_projects) == 10
    assert window._program_preferences.recent_projects[0] == str(roots[-1].resolve())
    assert len({item.casefold() for item in window._program_preferences.recent_projects}) == 10
    assert window.recent_projects_menu.isEnabled()
    assert window.import_project_archive_action.isEnabled()
