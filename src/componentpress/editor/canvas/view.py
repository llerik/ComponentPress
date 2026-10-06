"""Interactive scene projection for visual component editing."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, Node
from componentpress.domain.tree import node_bounds


class NodeOverlay(QGraphicsRectItem):
    def __init__(self, node_id: str, is_group: bool, rect: QRectF, z: float):
        super().__init__(rect)
        self.node_id = node_id
        self.is_group = is_group
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(z)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.update_pen()

    def update_pen(self) -> None:
        color = QColor("#0B78D0") if self.isSelected() else QColor(30, 30, 30, 150)
        pen = QPen(color, 0)
        if self.is_group:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsRectItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update_pen()
        return result

    def paint(self, painter: QPainter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        if self.isSelected() and not self.is_group:
            size = 7.0 / max(0.1, painter.transform().m11())
            corner = self.rect().bottomRight()
            painter.fillRect(QRectF(corner.x() - size, corner.y() - size, size, size), QColor("#0B78D0"))


class ComponentCanvas(QGraphicsView):
    zoomChanged = Signal(int)
    selectionChanged = Signal(object)
    moveRequested = Signal(object, float, float)
    resizeRequested = Signal(str, float, float)
    placementRequested = Signal(str, float, float)
    toolCancelled = Signal()
    deleteRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("componentCanvas")
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(Qt.GlobalColor.lightGray)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._has_image = False
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._component: ComponentDefinition | None = None
        self._overlays: dict[str, NodeOverlay] = {}
        self._tool: str | None = None
        self._drag_start: QPointF | None = None
        self._drag_positions: dict[str, QPointF] = {}
        self._resize_id: str | None = None
        self._resize_rect: QRectF | None = None
        self._syncing_selection = False
        self.scene().selectionChanged.connect(self._scene_selection_changed)

    @property
    def zoom_percent(self) -> int:
        return max(1, round(self.transform().m11() * 100))

    @property
    def active_tool(self) -> str | None:
        return self._tool

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.node_id for item in self._overlays.values() if item.isSelected())

    def set_tool(self, tool: str | None) -> None:
        self._tool = tool
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if tool else Qt.CursorShape.ArrowCursor)
        if tool:
            self.clear_selection()

    def cancel_tool(self) -> None:
        if self._tool is not None:
            self._tool = None
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self.toolCancelled.emit()

    def set_document(
        self,
        image: QImage,
        component: ComponentDefinition,
        *,
        fit: bool = False,
        selected: tuple[str, ...] = (),
    ) -> None:
        self._component = component
        scene = self.scene()
        self._syncing_selection = True
        scene.clear()
        self._overlays.clear()
        pixmap = QPixmap.fromImage(image)
        self._pixmap_item = scene.addPixmap(pixmap)
        self._pixmap_item.setZValue(0)
        sx = image.width() / component.size_mm.width
        sy = image.height() / component.size_mm.height
        order = 0

        def add(nodes: tuple[Node, ...]) -> None:
            nonlocal order
            for node in nodes:
                order += 1
                x, y, width, height = node_bounds(component, node.id)
                overlay = NodeOverlay(
                    node.id,
                    isinstance(node, GroupNode),
                    QRectF(x * sx, y * sy, width * sx, height * sy),
                    float(order),
                )
                scene.addItem(overlay)
                self._overlays[node.id] = overlay
                if isinstance(node, GroupNode):
                    add(node.children)

        add(component.elements)
        for node_id in selected:
            if node_id in self._overlays:
                self._overlays[node_id].setSelected(True)
        scene.setSceneRect(self._pixmap_item.boundingRect().adjusted(-24, -24, 24, 24))
        self._syncing_selection = False
        first = not self._has_image
        self._has_image = True
        if first or fit:
            self.fit_page()
        else:
            self.zoomChanged.emit(self.zoom_percent)

    def set_image(self, image: QImage, *, fit: bool = False) -> None:
        """Compatibility helper for image-only callers."""
        scene = self.scene()
        scene.clear()
        self._pixmap_item = scene.addPixmap(QPixmap.fromImage(image))
        scene.setSceneRect(self._pixmap_item.boundingRect().adjusted(-24, -24, 24, 24))
        first = not self._has_image
        self._has_image = True
        if first or fit:
            self.fit_page()

    def select_ids(self, node_ids: tuple[str, ...]) -> None:
        self._syncing_selection = True
        for node_id, item in self._overlays.items():
            item.setSelected(node_id in node_ids)
        self._syncing_selection = False

    def clear_selection(self) -> None:
        self.select_ids(())
        self.selectionChanged.emit(())

    def _scene_selection_changed(self) -> None:
        if not self._syncing_selection:
            self.selectionChanged.emit(self.selected_ids)

    def _overlay_at(self, point: QPointF) -> NodeOverlay | None:
        overlays = [item for item in self.scene().items(point) if isinstance(item, NodeOverlay)]
        # Once a group is selected from the tree, dragging anywhere inside its
        # calculated bounds must move the group instead of selecting a child.
        for item in overlays:
            if item.is_group and item.isSelected():
                return item
        tolerance = 7.0 / max(0.1, self.transform().m11())
        for item in overlays:
            if not item.is_group:
                continue
            rect = item.sceneBoundingRect()
            near_border = min(
                abs(point.x() - rect.left()), abs(point.x() - rect.right()),
                abs(point.y() - rect.top()), abs(point.y() - rect.bottom()),
            ) <= tolerance
            if near_border:
                return item
        for item in overlays:
            return item
        return None

    def _page_contains(self, point: QPointF) -> bool:
        return self._pixmap_item is not None and self._pixmap_item.sceneBoundingRect().contains(point)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self._tool is not None:
            tool = self._tool
            if not self._page_contains(point) or self._component is None or self._pixmap_item is None:
                self.cancel_tool()
                event.accept()
                return
            x_mm = point.x() * self._component.size_mm.width / self._pixmap_item.pixmap().width()
            y_mm = point.y() * self._component.size_mm.height / self._pixmap_item.pixmap().height()
            width_mm, height_mm = (30.0, 30.0) if tool == "image" else (40.0, 15.0)
            width_mm = max(0.001, min(width_mm, self._component.size_mm.width - x_mm))
            height_mm = max(0.001, min(height_mm, self._component.size_mm.height - y_mm))
            sx = self._pixmap_item.pixmap().width() / self._component.size_mm.width
            sy = self._pixmap_item.pixmap().height() / self._component.size_mm.height
            pending = self.scene().addRect(
                QRectF(x_mm * sx, y_mm * sy, width_mm * sx, height_mm * sy),
                QPen(QColor("#14A44D"), 0, Qt.PenStyle.DashLine),
            )
            pending.setZValue(100000)
            self.placementRequested.emit(tool, x_mm, y_mm)
            if pending.scene() is self.scene():
                self.scene().removeItem(pending)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        item = self._overlay_at(point)
        if item is None:
            self.clear_selection()
            event.accept()
            return
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._syncing_selection = True
        if ctrl:
            item.setSelected(not item.isSelected())
        elif not item.isSelected():
            self.scene().clearSelection()
            item.setSelected(True)
        self._syncing_selection = False
        self.selectionChanged.emit(self.selected_ids)
        selected = [overlay for overlay in self._overlays.values() if overlay.isSelected()]
        self._drag_start = point
        self._drag_positions = {overlay.node_id: overlay.pos() for overlay in selected}
        self._resize_id = None
        self._resize_rect = None
        if len(selected) == 1 and not selected[0].is_group:
            tolerance = 10.0 / max(0.1, self.transform().m11())
            corner = selected[0].sceneBoundingRect().bottomRight()
            if abs(point.x() - corner.x()) <= tolerance and abs(point.y() - corner.y()) <= tolerance:
                self._resize_id = selected[0].node_id
                self._resize_rect = QRectF(selected[0].rect())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        delta = point - self._drag_start
        if self._resize_id is not None and self._resize_rect is not None:
            overlay = self._overlays[self._resize_id]
            rect = QRectF(self._resize_rect)
            rect.setWidth(max(1.0, rect.width() + delta.x()))
            rect.setHeight(max(1.0, rect.height() + delta.y()))
            overlay.setRect(rect)
        else:
            for node_id, start in self._drag_positions.items():
                self._overlays[node_id].setPos(start + delta)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None or self._component is None or self._pixmap_item is None:
            super().mouseReleaseEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        delta = point - self._drag_start
        sx = self._component.size_mm.width / self._pixmap_item.pixmap().width()
        sy = self._component.size_mm.height / self._pixmap_item.pixmap().height()
        if self._resize_id is not None and self._resize_rect is not None:
            rect = self._overlays[self._resize_id].rect()
            self.resizeRequested.emit(self._resize_id, rect.width() * sx, rect.height() * sy)
        elif abs(delta.x()) > 0.01 or abs(delta.y()) > 0.01:
            self.moveRequested.emit(self.selected_ids, delta.x() * sx, delta.y() * sy)
        self._drag_start = None
        self._drag_positions = {}
        self._resize_id = None
        self._resize_rect = None
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._tool is not None:
            self.cancel_tool()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete and self.selected_ids:
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def fit_page(self) -> None:
        if not self._has_image or self._pixmap_item is None:
            return
        self.fitInView(self._pixmap_item.sceneBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.zoomChanged.emit(self.zoom_percent)

    def actual_size(self) -> None:
        if not self._has_image:
            return
        self.resetTransform()
        self.zoomChanged.emit(100)

    def zoom_by(self, factor: float) -> None:
        if not self._has_image:
            return
        current = self.transform().m11()
        target = min(8.0, max(0.1, current * factor))
        self.scale(target / current, target / current)
        self.zoomChanged.emit(self.zoom_percent)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)
