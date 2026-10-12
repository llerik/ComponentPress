"""Application-owned visual assets."""

from importlib.resources import files

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication


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


__all__ = ["load_application_icon", "load_icon"]
