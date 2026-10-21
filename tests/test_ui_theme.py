from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip

from componentpress.app_assets import load_icon, set_themed_icon
from componentpress.bootstrap import project_controller
from componentpress.editor.main_window import MainWindow
from componentpress.editor.theme import apply_theme, is_dark_theme, theme_palette
from componentpress.i18n import language, set_language, validate_catalogs


@pytest.fixture
def isolated_theme(tmp_path: Path, qapp, monkeypatch):
    from qdarktheme import _style_loader
    from qdarktheme._template import filter as theme_filter
    from componentpress.editor import theme as theme_module
    from componentpress.user_preferences import user_settings

    original_language = language()
    original_palette = qapp.palette()
    original_tooltip_palette = QToolTip.palette()
    original_stylesheet = qapp.styleSheet()
    original_dark = bool(qapp.property("componentpressDarkTheme"))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    settings = user_settings()
    settings.clear()
    cache = tmp_path / "theme-cache"
    cache.mkdir()
    monkeypatch.setattr(theme_filter, "get_cash_root_path", lambda _version: cache)
    monkeypatch.setattr(_style_loader, "get_cash_root_path", lambda _version: cache)
    monkeypatch.setattr(theme_module, "_CACHE_CONFIGURED", False)
    yield settings
    apply_theme("dark" if original_dark else "light")
    qapp.setStyleSheet(original_stylesheet)
    qapp.setPalette(original_palette)
    QToolTip.setPalette(original_tooltip_palette)
    qapp.setProperty("componentpressDarkTheme", original_dark)
    set_language(original_language)


@pytest.mark.parametrize(
    ("locale", "labels"),
    (
        ("ru", {"system": "Системная", "light": "Светлая", "dark": "Тёмная"}),
        ("en", {"system": "System", "light": "Light", "dark": "Dark"}),
    ),
)
def test_program_settings_save_theme_without_file_or_toolbar_control(isolated_theme, qtbot, locale, labels) -> None:
    from PySide6.QtCore import QTimer

    settings = isolated_theme
    validate_catalogs()
    set_language(locale)
    window = MainWindow(project_controller())
    qtbot.addWidget(window)

    assert window.settings_menu.title() == ("Настройки" if locale == "ru" else "Settings")
    assert not hasattr(window, "theme_menu")
    assert not hasattr(window, "theme_button")

    def accept_settings():
        dialog = QApplication.activeModalWidget()
        assert dialog is not None
        for mode, label in (("system", labels["system"]), ("light", labels["light"]), ("dark", labels["dark"])):
            index = dialog.theme.findData(mode)
            assert dialog.theme.itemText(index) == label
        dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
        dialog.accept()

    QTimer.singleShot(0, accept_settings)
    window._show_program_settings()
    assert settings.value("theme") == "dark"
    assert window._theme == "dark"
    assert QApplication.instance().styleSheet()

    reopened = MainWindow(project_controller())
    qtbot.addWidget(reopened)
    assert reopened._theme == "dark"


def test_invalid_saved_theme_falls_back_to_system(isolated_theme, qtbot) -> None:
    isolated_theme.setValue("program/preferences", '{"language":"ru","theme":"unknown"}')
    window = MainWindow(project_controller())
    qtbot.addWidget(window)

    assert window._theme == "system"


def test_system_theme_follows_os_and_manual_theme_ignores_it(isolated_theme, qapp, qtbot) -> None:
    hints = qapp.styleHints()
    original_scheme = hints.colorScheme()
    hints.setColorScheme(Qt.ColorScheme.Light)
    apply_theme("system")
    assert not is_dark_theme()

    hints.setColorScheme(Qt.ColorScheme.Dark)
    qtbot.waitUntil(is_dark_theme)
    hints.setColorScheme(Qt.ColorScheme.Light)
    qtbot.waitUntil(lambda: not is_dark_theme())

    apply_theme("dark")
    hints.setColorScheme(Qt.ColorScheme.Light)
    qapp.processEvents()
    assert is_dark_theme()
    hints.setColorScheme(original_scheme)


def test_canvas_background_uses_the_new_palette_on_first_theme_change(isolated_theme, qapp) -> None:
    from componentpress.editor.canvas import ComponentCanvas

    apply_theme("light")
    canvas = ComponentCanvas()
    apply_theme("dark")
    qapp.processEvents()
    assert canvas.backgroundBrush().color() == theme_palette().color(theme_palette().ColorRole.Dark)
    apply_theme("light")
    qapp.processEvents()
    assert canvas.backgroundBrush().color() == theme_palette().color(theme_palette().ColorRole.Dark)


@pytest.mark.parametrize("mode", ("light", "dark"))
def test_command_icon_remains_visible_and_tracks_palette(isolated_theme, qapp, mode) -> None:
    button = QPushButton()
    apply_theme("light")
    set_themed_icon(button, "save.png")
    apply_theme(mode)
    pixmap = button.icon().pixmap(QSize(22, 22))

    assert not pixmap.isNull()
    opaque = [pixmap.toImage().pixelColor(x, y) for x in range(22) for y in range(22)
              if pixmap.toImage().pixelColor(x, y).alpha() > 127]
    assert opaque
    foreground = theme_palette().color(theme_palette().ColorRole.Text)
    if mode == "dark":
        assert any(color == QColor(foreground) for color in opaque)
    else:
        expected = load_icon("save.png").pixmap(QSize(22, 22))
        assert not expected.isNull()
        assert expected.toImage() == pixmap.toImage()


@pytest.mark.parametrize("mode", ("light", "dark"))
def test_tooltip_palette_tracks_theme(isolated_theme, qapp, mode) -> None:
    apply_theme(mode)

    for role in (QPalette.ColorRole.ToolTipBase, QPalette.ColorRole.ToolTipText):
        assert QToolTip.palette().color(QPalette.ColorGroup.Inactive, role) == qapp.palette().color(
            QPalette.ColorGroup.Inactive, role
        )


def test_copy_mode_info_uses_the_info_asset(isolated_theme, qtbot) -> None:
    from componentpress.bootstrap import project_controller

    window = MainWindow(project_controller())
    qtbot.addWidget(window)
    assert window.mode_info_button.text() == ""
    assert not window.mode_info_button.icon().isNull()
