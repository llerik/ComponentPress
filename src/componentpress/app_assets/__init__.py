"""Application-owned visual assets."""

from importlib.resources import files

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication


_ICON_RESOURCE = "componentpress.png"


def load_application_icon() -> QIcon:
    """Load the application icon after QApplication has been created."""
    if not isinstance(QApplication.instance(), QApplication):
        raise RuntimeError("Для загрузки иконки сначала необходимо создать QApplication")
    data = files(__package__).joinpath(_ICON_RESOURCE).read_bytes()
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        raise RuntimeError("Не удалось загрузить встроенную иконку ComponentPress")
    return QIcon(pixmap)


__all__ = ["load_application_icon"]
