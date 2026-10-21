"""Local-only storage for window and dock layout preferences."""

from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication


class UiState:
    def __init__(self, settings: QSettings | None = None) -> None:
        self.settings = settings or QSettings("ComponentPress", "ComponentPress")

    def save(self, window) -> None:
        settings = self.settings
        settings.beginGroup("mainWindow")
        settings.setValue("geometry", window.saveGeometry())
        settings.setValue("rightTab", window.right_tabs.currentIndex())
        settings.setValue("rightSplitterSizes", window.right_splitter.sizes())
        settings.setValue("hideEventProjectPath", window.hide_event_project_path.isChecked())
        if window.session is not None:
            settings.setValue("state", window.saveState())
            for dock in window.docks:
                settings.setValue(f"docks/{dock.objectName()}", dock.isVisible())
        settings.endGroup()
        settings.sync()

    def restore(self, window) -> None:
        settings = self.settings
        settings.beginGroup("mainWindow")
        geometry = settings.value("geometry")
        state = settings.value("state")
        if geometry is not None and not window.restoreGeometry(geometry):
            window.resize(1280, 800)
        frame = window.frameGeometry()
        visible = any(screen.availableGeometry().intersects(frame) for screen in QGuiApplication.screens())
        if not visible:
            window.resize(1280, 800)
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                window.move(screen.availableGeometry().center() - window.rect().center())
        if state is not None:
            window.restoreState(state)
        splitter_sizes = settings.value("rightSplitterSizes")
        if isinstance(splitter_sizes, (list, tuple)) and len(splitter_sizes) == 2:
            sizes = [int(size) for size in splitter_sizes]
            if any(sizes):
                window.right_splitter.setSizes(sizes)
        window._saved_dock_visibility = {
            dock.objectName(): settings.value(f"docks/{dock.objectName()}", True, type=bool)
            for dock in window.docks
        }
        index = settings.value("rightTab", 0, type=int)
        if 0 <= index < window.right_tabs.count():
            window.right_tabs.setCurrentIndex(index)
        window.hide_event_project_path.setChecked(
            settings.value("hideEventProjectPath", True, type=bool)
        )
        settings.endGroup()
