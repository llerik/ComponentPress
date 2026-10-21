"""Application theme preference and PyQtDarkTheme integration."""

from pathlib import Path
from tempfile import gettempdir

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QToolTip

THEMES = ("system", "light", "dark")
_CURRENT_THEME = "system"
_SYSTEM_WATCHING = False
_CACHE_CONFIGURED = False


def apply_theme(theme: str) -> None:
    global _CACHE_CONFIGURED, _CURRENT_THEME, _SYSTEM_WATCHING
    import qdarktheme

    if not _CACHE_CONFIGURED:
        from qdarktheme import _style_loader
        from qdarktheme._template import filter as theme_filter

        def cache_root(version: str) -> Path:
            return Path(gettempdir()) / "ComponentPress" / "qdarktheme" / f"v{version}"

        theme_filter.get_cash_root_path = cache_root
        _style_loader.get_cash_root_path = cache_root
        _CACHE_CONFIGURED = True

    if theme not in THEMES:
        theme = "system"
    hints = QGuiApplication.styleHints()
    if _SYSTEM_WATCHING:
        hints.colorSchemeChanged.disconnect(_apply_system_theme)
        _SYSTEM_WATCHING = False
    _CURRENT_THEME = theme
    if theme == "system":
        hints.colorSchemeChanged.connect(_apply_system_theme)
        _SYSTEM_WATCHING = True
        _apply_system_theme()
    else:
        _apply_library_theme(theme)


def _apply_system_theme(*_args) -> None:
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme()
    _apply_library_theme("dark" if scheme == Qt.ColorScheme.Dark else "light")


def _apply_library_theme(mode: str) -> None:
    import qdarktheme

    custom_colors = {"primary": "#2478D4"}
    application = QApplication.instance()
    if application is not None:
        # PaletteChange handlers must see the new effective mode immediately.
        application.setProperty("componentpressDarkTheme", mode == "dark")
        application.setStyleSheet(
            qdarktheme.load_stylesheet(mode, default_theme="light", custom_colors=custom_colors)
        )
        palette = qdarktheme.load_palette(mode, default_theme="light", custom_colors=custom_colors)
        application.setPalette(palette)
        QToolTip.setPalette(palette)
    _publish_effective_theme(mode)


def _publish_effective_theme(mode: str) -> None:
    application = QApplication.instance()
    if application is None:
        return
    application.setProperty("componentpressDarkTheme", mode == "dark")
    from componentpress.app_assets import refresh_themed_icons

    refresh_themed_icons()
    refresh_theme_widgets()


def is_dark_theme() -> bool:
    application = QApplication.instance()
    return bool(application and application.property("componentpressDarkTheme"))


def theme_palette():
    import qdarktheme

    return qdarktheme.load_palette("dark" if is_dark_theme() else "light")


def style_warning(widget, active: bool = True) -> None:
    if not active:
        widget.setStyleSheet("padding: 4px" if widget.objectName() == "yamlDiagnostic" else "")
        return
    color = "#F28B82" if is_dark_theme() else "#B3261E"
    padding = "padding: 4px; " if widget.objectName() == "yamlDiagnostic" else ""
    widget.setStyleSheet(f"{padding}color: {color}")


def refresh_theme_widgets() -> None:
    application = QApplication.instance()
    if application is None:
        return
    for widget in application.allWidgets():
        if widget.objectName() == "yamlDiagnostic":
            style_warning(widget, bool(widget.text()))
        elif widget.objectName() == "imageQuality":
            style_warning(widget, bool(widget.property("componentpressWarning")))
