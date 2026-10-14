"""Read-only component viewport using the editor's shared size and zoom rules."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QLabel, QFrame

from componentpress.domain.component import ComponentDefinition

from .view import _component_rect, _scale_view, _set_fit_scrollbars, _set_pixmap_component_geometry


class ComponentPreview(QGraphicsView):
    """A scrollable view of the current rendered component, with independent zoom."""

    zoomChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewImage")
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._component_rect = _component_rect(0, 0)
        self._fit_mode = True
        self._fitting = False
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._status.hide()
        self._status.setParent(self.viewport())

    @property
    def zoom_percent(self) -> int:
        return max(1, round(self.transform().m11() * 100))

    def text(self) -> str:
        return self._status.text()

    def setText(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))
        self._status.setGeometry(self.viewport().rect())
        self._status.raise_()

    def clear_frame(self, message: str) -> None:
        if self._pixmap_item is not None:
            self.scene().removeItem(self._pixmap_item)
            self._pixmap_item = None
        self._component_rect = _component_rect(0, 0)
        self.scene().setSceneRect(self._component_rect)
        self.setText(message)

    def set_frame(self, image: QImage, component: ComponentDefinition, *, fit: bool = False) -> None:
        first = self._pixmap_item is None
        if first:
            self._pixmap_item = self.scene().addPixmap(QPixmap())
            self._pixmap_item.setZValue(0)
        self._component_rect = _set_pixmap_component_geometry(
            self._pixmap_item, image, component.size_mm.width, component.size_mm.height,
        )
        self.scene().setSceneRect(self._component_rect)
        self.setText("")
        if first or fit or self._fit_mode:
            self.fit_page()
        else:
            self.centerOn(self._component_rect.center())

    def fit_page(self) -> None:
        if self._pixmap_item is None:
            return
        self._fit_mode = True
        _set_fit_scrollbars(self, True)
        self._fitting = True
        try:
            self.fitInView(self._component_rect, Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            self._fitting = False
        self.zoomChanged.emit(self.zoom_percent)

    def actual_size(self) -> None:
        if self._pixmap_item is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        self.resetTransform()
        self.centerOn(self._component_rect.center())
        self.zoomChanged.emit(100)

    def zoom_by(self, factor: float) -> None:
        if self._pixmap_item is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        _scale_view(self, factor)
        self.zoomChanged.emit(self.zoom_percent)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._status.setGeometry(self.viewport().rect())
        if self._fit_mode and self._pixmap_item is not None and not self._fitting:
            self.fit_page()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)
