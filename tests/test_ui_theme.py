from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QPushButton

from componentpress.app_assets import load_icon, set_themed_icon
from componentpress.bootstrap import project_controller
from componentpress.editor.main_window import MainWindow
from componentpress.editor.theme import THEMES, apply_theme, is_dark_theme, theme_palette
from componentpress.i18n import language, set_language, validate_catalogs


@pytest.fixture
def isolated_theme(tmp_path: Path, qapp, monkeypatch):
    from qdarktheme import _style_loader
    from qdarktheme._template import filter as theme_filter
    from componentpress.editor import main_window as main_window_module, theme as theme_module

    original_language = language()
    original_palette = qapp.palette()
    original_stylesheet = qapp.styleSheet()
    original_dark = bool(qapp.property("componentpressDarkTheme"))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.clear()
    monkeypatch.setattr(main_window_module, "theme_settings", lambda: settings)
    cache = tmp_path / "theme-cache"
    cache.mkdir()
    monkeypatch.setattr(theme_filter, "get_cash_root_path", lambda _version: cache)
    monkeypatch.setattr(_style_loader, "get_cash_root_path", lambda _version: cache)
    monkeypatch.setattr(theme_module, "_CACHE_CONFIGURED", False)
    yield settings
    apply_theme("dark" if original_dark else "light")
    qapp.setStyleSheet(original_stylesheet)
    qapp.setPalette(original_palette)
    qapp.setProperty("componentpressDarkTheme", original_dark)
    set_language(original_language)


@pytest.mark.parametrize(
    ("locale", "labels"),
    (
        ("ru", {"system": "Системная", "light": "Светлая", "dark": "Тёмная"}),
        ("en", {"system": "System", "light": "Light", "dark": "Dark"}),
    ),
)
def test_theme_menu_applies_and_persists_all_modes(isolated_theme, qtbot, locale, labels) -> None:
    settings = isolated_theme
    validate_catalogs()
    set_language(locale)
    window = MainWindow(project_controller())
    qtbot.addWidget(window)

    assert tuple(window.theme_actions) == THEMES
    for mode in THEMES:
        action = window.theme_actions[mode]
        assert action.text() == labels[mode]
        action.trigger()
        assert settings.value("theme") == mode
        assert action.isChecked()
        assert window.theme_menu.isEnabled()
        assert QApplication.instance().styleSheet()

    reopened = MainWindow(project_controller())
    qtbot.addWidget(reopened)
    assert reopened._theme == "dark"
    assert reopened.theme_actions["dark"].isChecked()


def test_invalid_saved_theme_falls_back_to_system(isolated_theme, qtbot) -> None:
    isolated_theme.setValue("theme", "unknown")
    window = MainWindow(project_controller())
    qtbot.addWidget(window)

    assert window._theme == "system"
    assert window.theme_actions["system"].isChecked()


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
