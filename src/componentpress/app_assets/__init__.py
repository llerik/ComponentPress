"""Application-owned visual assets."""

from importlib.resources import files
from weakref import ref

import shiboken6
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

_ICON_TARGETS = {}
_ICON_CACHE = {}


def load_icon(resource_name: str) -> QIcon:
    """Load a packaged PNG icon after QApplication has been created."""
    if not isinstance(QApplication.instance(), QApplication):
        raise RuntimeError("Для загрузки иконки сначала необходимо создать QApplication")
    data = files(__package__).joinpath(resource_name).read_bytes()
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        raise RuntimeError(f"Не удалось загрузить встроенную иконку {resource_name}")
    return QIcon(pixmap)


def load_application_icon() -> QIcon:
    """Load the application icon after QApplication has been created."""
    return load_icon("componentpress.png")


def set_themed_icon(target, resource_name: str) -> None:
    """Set an app icon that follows the active light or dark palette."""
    target_id = id(target)
    _ICON_TARGETS[target_id] = (
        ref(target, lambda _ref, identifier=target_id: _ICON_TARGETS.pop(identifier, None)),
        resource_name,
    )
    target.setIcon(_icon_for_palette(resource_name))


def _icon_for_palette(resource_name: str) -> QIcon:
    application = QApplication.instance()
    dark = bool(application and application.property("componentpressDarkTheme"))
    key = (resource_name, dark)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    if not dark:
        _ICON_CACHE[key] = load_icon(resource_name)
        return _ICON_CACHE[key]
    from componentpress.editor.theme import theme_palette

    icon = load_icon(resource_name)
    pixmap = icon.pixmap(QSize(50, 50))
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), theme_palette().color(theme_palette().ColorRole.Text))
    painter.end()
    result = QIcon(pixmap)
    _ICON_CACHE[key] = result
    return result


def refresh_themed_icons() -> None:
    application = QApplication.instance()
    if application is None:
        return
    for target_id, (target_ref, resource_name) in tuple(_ICON_TARGETS.items()):
        target = target_ref()
        if target is None or not shiboken6.isValid(target):
            _ICON_TARGETS.pop(target_id, None)
            continue
        target.setIcon(_icon_for_palette(resource_name))


__all__ = ["load_application_icon", "load_icon", "refresh_themed_icons", "set_themed_icon"]
