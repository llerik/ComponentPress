from PySide6.QtCore import QObject, Signal


class BuildSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
