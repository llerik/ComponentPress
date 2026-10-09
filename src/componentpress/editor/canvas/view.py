"""Interactive scene projection for visual component editing."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, Node
from componentpress.domain.tree import effectively_locked_ids, node_bounds


class NodeOverlay(QGraphicsRectItem):
    def __init__(self, node_id: str, is_group: bool, locked: bool, rect: QRectF, z: float):
        super().__init__(rect)
        self.node_id = node_id
        self.is_group = is_group
        self.locked = locked
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(z)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.update_pen()

    def update_pen(self) -> None:
        color = QColor("#B3261E") if self.locked else QColor("#0B78D0") if self.isSelected() else QColor(30, 30, 30, 150)
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
    nudgeRequested = Signal(object, float, float)

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
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
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
        self._nudge_key: int | None = None
        self._nudge_ids: tuple[str, ...] = ()
        self._nudge_delta = QPointF()
        self._nudge_starts: dict[str, QPointF] = {}
        self._world_per_mm_x = 96.0 / 25.4
        self._world_per_mm_y = 96.0 / 25.4
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
        self._syncing_selection = True
        self._overlays.clear()
        self.scene().clear()
        self._pixmap_item = self.scene().addPixmap(QPixmap.fromImage(image))
        self._pixmap_item.setZValue(0)
        self._sync_model_overlays(component, selected)
        scene = self.scene()
        scene.setSceneRect(self._pixmap_item.boundingRect().adjusted(-24, -24, 24, 24))
        first = not self._has_image
        self._has_image = True
        if first or fit:
            self.fit_page()
        else:
            self.zoomChanged.emit(self.zoom_percent)

    def set_component_model(self, component: ComponentDefinition, selected: tuple[str, ...] = ()) -> None:
        """Refresh hit targets and lock indicators even if preview rendering failed."""
        self._component = component
        self._sync_model_overlays(component, selected)
        self.viewport().update()

    def _sync_model_overlays(self, component: ComponentDefinition, selected: tuple[str, ...]) -> None:
        locked_ids = effectively_locked_ids(component)
        scene = self.scene()
        self._syncing_selection = True
        for item in tuple(self._overlays.values()):
            if item.scene() is scene:
                scene.removeItem(item)
        self._overlays.clear()
        if self._pixmap_item is None:
            self._syncing_selection = False
            return
        sx = self._pixmap_item.boundingRect().width() / component.size_mm.width
        sy = self._pixmap_item.boundingRect().height() / component.size_mm.height
        order = 0

        def add(nodes: tuple[Node, ...]) -> None:
            nonlocal order
            for node in nodes:
                order += 1
                x, y, width, height = node_bounds(component, node.id)
                overlay = NodeOverlay(
                    node.id,
                    isinstance(node, GroupNode),
                    node.id in locked_ids,
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

    def set_image(self, image: QImage, *, fit: bool = False) -> None:
        """Compatibility helper for image-only callers."""
        scene = self.scene()
        scene.clear()
        self._overlays.clear()
        self._component = None
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

    def clear_document(self) -> None:
        self.scene().clear()
        self._overlays.clear()
        self._pixmap_item = None
        self._component = None
        self._has_image = False
        self.selectionChanged.emit(())
        self.viewport().update()

    def release_preview(self) -> None:
        """Drop the high-resolution backing image while retaining scene/model state."""
        if self._pixmap_item is not None and self._pixmap_item.scene() is self.scene():
            self.scene().removeItem(self._pixmap_item)
        self._pixmap_item = None
        self._has_image = False
        self.viewport().update()

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
            x_mm = point.x() / self._world_per_mm_x
            y_mm = point.y() / self._world_per_mm_y
            width_mm, height_mm = (30.0, 30.0) if tool == "image" else (40.0, 15.0)
            width_mm = max(0.001, min(width_mm, self._component.size_mm.width - x_mm))
            height_mm = max(0.001, min(height_mm, self._component.size_mm.height - y_mm))
            sx = self._world_per_mm_x
            sy = self._world_per_mm_y
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
        if item.locked or any(overlay.locked for overlay in selected):
            self._drag_start = None
            self._drag_positions = {}
            event.accept()
            return
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
        sx = 1 / self._world_per_mm_x
        sy = 1 / self._world_per_mm_y
        if self._resize_id is not None and self._resize_rect is not None:
            rect = self._overlays[self._resize_id].rect()
            component_before = self._component
            node_id = self._resize_id
            self.resizeRequested.emit(self._resize_id, rect.width() * sx, rect.height() * sy)
            if self._component is component_before and node_id in self._overlays:
                self._overlays[node_id].setRect(self._resize_rect)
        elif abs(delta.x()) > 0.01 or abs(delta.y()) > 0.01:
            component_before = self._component
            starts = dict(self._drag_positions)
            self.moveRequested.emit(self.selected_ids, delta.x() * sx, delta.y() * sy)
            if self._component is component_before:
                for node_id, start in starts.items():
                    if node_id in self._overlays:
                        self._overlays[node_id].setPos(start)
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
        steps = {
            Qt.Key.Key_Left: (-0.1, 0.0), Qt.Key.Key_Right: (0.1, 0.0),
            Qt.Key.Key_Up: (0.0, -0.1), Qt.Key.Key_Down: (0.0, 0.1),
        }
        if event.key() in steps and self._component is not None and self._pixmap_item is not None and self.selected_ids:
            ids = self.selected_ids
            if any(self._overlays[node_id].locked for node_id in ids):
                event.accept()
                return
            if self._nudge_key is None:
                self._nudge_key = event.key()
                self._nudge_ids = ids
                self._nudge_starts = {node_id: self._overlays[node_id].pos() for node_id in ids}
                self._nudge_delta = QPointF()
            if event.key() != self._nudge_key or ids != self._nudge_ids:
                self._finish_nudge()
                event.accept()
                return
            if event.isAutoRepeat() is False or self._nudge_key is not None:
                amount = 1.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.1
                dx, dy = steps[event.key()]
                if dx:
                    dx *= amount / 0.1
                if dy:
                    dy *= amount / 0.1
                sx = self._world_per_mm_x
                sy = self._world_per_mm_y
                self._nudge_delta += QPointF(dx * sx, dy * sy)
                for node_id, start in self._nudge_starts.items():
                    self._overlays[node_id].setPos(start + self._nudge_delta)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == self._nudge_key and not event.isAutoRepeat():
            self._finish_nudge()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _finish_nudge(self) -> None:
        if self._nudge_key is None:
            return
        if self._component is not None and self._pixmap_item is not None and (self._nudge_delta.x() or self._nudge_delta.y()):
            sx = 1 / self._world_per_mm_x
            sy = 1 / self._world_per_mm_y
            component_before = self._component
            starts = dict(self._nudge_starts)
            self.nudgeRequested.emit(self._nudge_ids, self._nudge_delta.x() * sx, self._nudge_delta.y() * sy)
            if self._component is component_before:
                for node_id, start in starts.items():
                    if node_id in self._overlays:
                        self._overlays[node_id].setPos(start)
        self._nudge_key = None
        self._nudge_ids = ()
        self._nudge_delta = QPointF()
        self._nudge_starts.clear()

    def focusOutEvent(self, event) -> None:
        self._finish_nudge()
        super().focusOutEvent(event)

    def fit_page(self) -> None:
        if not self._has_image or self._pixmap_item is None:
            return
        self.fitInView(self._pixmap_item.sceneBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.zoomChanged.emit(self.zoom_percent)

    def actual_size(self) -> None:
        if self._component is None:
            return
        self.resetTransform()
        self.zoomChanged.emit(100)

    def zoom_by(self, factor: float) -> None:
        if self._component is None:
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
