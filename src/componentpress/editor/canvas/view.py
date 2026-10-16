"""Interactive scene projection for visual component editing."""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGraphicsLineItem

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, LineNode, Node
from componentpress.domain.geometry import Affine, local_bounds
from componentpress.domain.tree import effectively_locked_ids, locations
from componentpress.editor.theme import is_dark_theme, theme_palette


def _component_rect(width_mm: float, height_mm: float) -> QRectF:
    return QRectF(0, 0, width_mm * 96.0 / 25.4, height_mm * 96.0 / 25.4)


def _set_pixmap_component_geometry(item: QGraphicsPixmapItem, image: QImage, width_mm: float, height_mm: float) -> QRectF:
    item.setPixmap(QPixmap.fromImage(image))
    bounds = item.boundingRect()
    target = _component_rect(width_mm, height_mm)
    item.setTransform(QTransform.fromScale(target.width() / bounds.width(), target.height() / bounds.height()))
    return target


def _set_fit_scrollbars(view: QGraphicsView, fitting: bool) -> None:
    policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fitting else Qt.ScrollBarPolicy.ScrollBarAsNeeded
    view.setHorizontalScrollBarPolicy(policy)
    view.setVerticalScrollBarPolicy(policy)


def _scale_view(view: QGraphicsView, factor: float) -> None:
    current = view.transform().m11()
    target = min(8.0, max(0.1, current * factor))
    view.scale(target / current, target / current)


class NodeOverlay(QGraphicsRectItem):
    def __init__(self, node_id: str, is_group: bool, locked: bool, rect: QRectF, z: float, line_endpoint: QPointF | None = None):
        super().__init__(rect)
        self.node_id = node_id
        self.is_group = is_group
        self.locked = locked
        self.line_endpoint = line_endpoint
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(z)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.update_pen()

    def update_pen(self) -> None:
        palette = theme_palette()
        dark = is_dark_theme()
        color = QColor("#F28B82" if dark else "#B3261E") if self.locked else palette.highlight().color() if self.isSelected() else palette.color(palette.ColorRole.Mid)
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

    def handle_points(self) -> tuple[QPointF, ...]:
        rect = self.rect()
        x1, y1, x2, y2 = rect.left(), rect.top(), rect.right(), rect.bottom()
        xm, ym = (x1 + x2) / 2, (y1 + y2) / 2
        return tuple(QPointF(x, y) for x, y in ((x1,y1),(xm,y1),(x2,y1),(x2,ym),(x2,y2),(xm,y2),(x1,y2),(x1,ym)))

class ComponentCanvas(QGraphicsView):
    zoomChanged = Signal(int)
    visualGestureFinished = Signal()
    selectionChanged = Signal(object)
    nodeContextMenuRequested = Signal(str, QPoint)
    moveRequested = Signal(object, float, float)
    resizeRequested = Signal(str, float, float, float, float)
    lineResizeRequested = Signal(str, float, float)
    placementRequested = Signal(str, float, float)
    drawRequested = Signal(str, float, float, float, float)
    toolCancelled = Signal()
    deleteRequested = Signal()
    nudgeRequested = Signal(object, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("componentCanvas")
        self.setScene(QGraphicsScene(self))
        palette = theme_palette()
        self.setBackgroundBrush(palette.color(palette.ColorRole.Dark))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.viewport().setMouseTracking(True)
        self._fit_mode = True
        self._fitting = False
        self._deferred_model: tuple[ComponentDefinition, tuple[str, ...]] | None = None
        self._has_image = False
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._preview_pixels: QImage | None = None
        self._component: ComponentDefinition | None = None
        self._overlays: dict[str, NodeOverlay] = {}
        self._tool: str | None = None
        self._draw_start: QPointF | None = None
        self._draw_preview = None
        self._drag_start: QPointF | None = None
        self._drag_positions: dict[str, QPointF] = {}
        self._resize_id: str | None = None
        self._resize_rect: QRectF | None = None
        self._resize_handle: int | None = None
        self._resize_local_start: QPointF | None = None
        self._line_resize_id: str | None = None
        self._syncing_selection = False
        self._nudge_key: int | None = None
        self._nudge_ids: tuple[str, ...] = ()
        self._nudge_delta = QPointF()
        self._nudge_starts: dict[str, QPointF] = {}
        self._world_per_mm_x = 96.0 / 25.4
        self._world_per_mm_y = 96.0 / 25.4
        self.scene().selectionChanged.connect(self._scene_selection_changed)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if hasattr(self, "_overlays") and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            palette = theme_palette()
            self.setBackgroundBrush(palette.color(palette.ColorRole.Dark))
            for overlay in self._overlays.values():
                overlay.update_pen()

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
        self._update_hover_cursor()
        if tool:
            self.clear_selection()

    def cancel_tool(self) -> None:
        if self._tool is not None:
            self._tool = None
            self._draw_start = None
            if self._draw_preview is not None and self._draw_preview.scene() is self.scene():
                self.scene().removeItem(self._draw_preview)
            self._draw_preview = None
            self._update_hover_cursor()
            self._flush_deferred_model()
            self.visualGestureFinished.emit()
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
        for overlay in tuple(self._overlays.values()):
            self.scene().removeItem(overlay)
        self._overlays.clear()
        if self._pixmap_item is None:
            self._pixmap_item = self.scene().addPixmap(QPixmap())
            self._pixmap_item.setZValue(0)
        self._preview_pixels = image
        _set_pixmap_component_geometry(self._pixmap_item, image, component.size_mm.width, component.size_mm.height)
        self._sync_model_overlays(component, selected)
        scene = self.scene()
        scene.setSceneRect(self._pixmap_item.sceneBoundingRect().adjusted(-24, -24, 24, 24))
        first = not self._has_image
        self._has_image = True
        if first or fit or self._fit_mode:
            self.fit_page()
        else:
            self.zoomChanged.emit(self.zoom_percent)

    def set_component_model(self, component: ComponentDefinition, selected: tuple[str, ...] = ()) -> None:
        """Refresh hit targets and lock indicators even if preview rendering failed."""
        if self._gesture_active:
            self._deferred_model = (component, selected)
            return
        self._component = component
        self._sync_model_overlays(component, selected)
        self.viewport().update()

    def set_preview_pending(self, pending: bool) -> None:
        if self._pixmap_item is not None:
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
        sx = self._world_per_mm_x
        sy = self._world_per_mm_y
        order = 0

        def add(nodes: tuple[Node, ...]) -> None:
            nonlocal order
            for node in nodes:
                order += 1
                node_location = locations(component)[node.id]
                node_data = node_location.node
                left, top, width, height = local_bounds(node_data)
                if isinstance(node_data, LineNode):
                    if width * sx < 8:
                        left -= (8 / sx - width) / 2
                        width = 8 / sx
                    if height * sy < 8:
                        top -= (8 / sy - height) / 2
                        height = 8 / sy
                bounds = QRectF(left * sx, top * sy, width * sx, height * sy)
                world = node_location.world
                matrix = QTransform(world.a, world.c * sy / sx, world.b * sx / sy, world.d, 0, 0)
                endpoint = QPointF((node_data.dx_mm - left) * sx, (node_data.dy_mm - top) * sy) if isinstance(node_data, LineNode) else None
                overlay = NodeOverlay(
                    node.id,
                    isinstance(node, GroupNode),
                    node.id in locked_ids,
                    bounds,
                    float(order),
                    endpoint,
                )
                overlay.setPos(world.x * sx, world.y * sy)
                overlay.setTransform(matrix)
                scene.addItem(overlay)
                self._overlays[node.id] = overlay
                if isinstance(node, GroupNode):
                    add(node.children)

        add(component.elements)
        for node_id in selected:
            if node_id in self._overlays:
                self._overlays[node_id].setSelected(True)
        scene.setSceneRect(self._pixmap_item.sceneBoundingRect().adjusted(-24, -24, 24, 24))
        self._syncing_selection = False
        self.viewport().update()

    def set_image(self, image: QImage, *, fit: bool = False) -> None:
        """Compatibility helper for image-only callers."""
        scene = self.scene()
        scene.clear()
        self._overlays.clear()
        self._component = None
        self._pixmap_item = scene.addPixmap(QPixmap.fromImage(image))
        self._preview_pixels = image
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
        self.viewport().update()

    def clear_document(self) -> None:
        self.scene().clear()
        self._overlays.clear()
        self._pixmap_item = None
        self._preview_pixels = None
        self._component = None
        self._has_image = False
        self.selectionChanged.emit(())
        self.viewport().update()

    def release_preview(self) -> None:
        """Drop the high-resolution backing image while retaining scene/model state."""
        if self._pixmap_item is not None and self._pixmap_item.scene() is self.scene():
            self.scene().removeItem(self._pixmap_item)
        self._pixmap_item = None
        self._preview_pixels = None
        self._has_image = False
        self.viewport().update()

    def clear_selection(self) -> None:
        self.select_ids(())
        self.selectionChanged.emit(())

    def _scene_selection_changed(self) -> None:
        if not self._syncing_selection:
            self.viewport().update()
            self._update_hover_cursor()
            self.selectionChanged.emit(self.selected_ids)

    def _handle_centers(self, overlay: NodeOverlay) -> tuple[QPointF, ...]:
        centers = tuple(self.mapFromScene(overlay.mapToScene(point)) for point in overlay.handle_points())
        if any((a - b).manhattanLength() < 10 for i, a in enumerate(centers) for b in centers[i + 1:]):
            offsets = ((-12, -12), (0, -12), (12, -12), (12, 0), (12, 12), (0, 12), (-12, 12), (-12, 0))
            return tuple(QPointF(point.x() + dx, point.y() + dy) for point, (dx, dy) in zip(centers, offsets))
        return tuple(QPointF(point) for point in centers)

    def _handle_hit(self, overlay: NodeOverlay, viewport_point: QPointF) -> int | None:
        if overlay.locked or not overlay.isSelected():
            return None
        if overlay.is_group and self._component is not None:
            index = locations(self._component)
            locked = effectively_locked_ids(self._component)
            if any(
                node_id in locked and self._is_descendant(index, node_id, overlay.node_id)
                for node_id in index
            ):
                return None
        candidates = [
            (index, (center.x() - viewport_point.x()) ** 2 + (center.y() - viewport_point.y()) ** 2)
            for index, center in enumerate(self._handle_centers(overlay))
            if abs(center.x() - viewport_point.x()) <= 14 and abs(center.y() - viewport_point.y()) <= 14
        ]
        return min(candidates, key=lambda value: value[1])[0] if candidates else None

    @staticmethod
    def _is_descendant(index, node_id: str, ancestor_id: str) -> bool:
        parent_id = index[node_id].parent_id
        while parent_id is not None:
            if parent_id == ancestor_id:
                return True
            parent_id = index[parent_id].parent_id
        return False

    @property
    def _gesture_active(self) -> bool:
        return self._drag_start is not None or self._draw_start is not None or self._nudge_key is not None

    def _flush_deferred_model(self) -> None:
        if self._deferred_model is None or self._gesture_active:
            return
        component, selected = self._deferred_model
        self._deferred_model = None
        still_present = tuple(node_id for node_id in self.selected_ids if node_id in locations(component))
        self.set_component_model(component, still_present or selected)

    def _cursor_for_handle(self, overlay: NodeOverlay, handle: int) -> Qt.CursorShape:
        dx, dy = ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0))[handle]
        origin = overlay.mapToScene(QPointF())
        transform = self.viewportTransform()
        center = transform.map(origin)
        x_axis = transform.map(overlay.mapToScene(QPointF(dx, 0))) - center if dx else QPointF()
        y_axis = transform.map(overlay.mapToScene(QPointF(0, dy))) - center if dy else QPointF()
        if x_axis.isNull():
            direction = y_axis
        elif y_axis.isNull():
            direction = x_axis
        else:
            x_axis /= math.hypot(x_axis.x(), x_axis.y())
            y_axis /= math.hypot(y_axis.x(), y_axis.y())
            direction = x_axis + y_axis
        angle = math.degrees(math.atan2(direction.y(), direction.x())) % 180
        cursors = (
            (0, Qt.CursorShape.SizeHorCursor),
            (45, Qt.CursorShape.SizeFDiagCursor),
            (90, Qt.CursorShape.SizeVerCursor),
            (135, Qt.CursorShape.SizeBDiagCursor),
        )
        return min(cursors, key=lambda item: min(abs(angle - item[0]), 180 - abs(angle - item[0])))[1]

    def _update_hover_cursor(self, point: QPointF | None = None) -> None:
        if self._tool is not None:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            return
        viewport_point = QPointF(point) if point is not None else QPointF(self.viewport().mapFromGlobal(QCursor.pos()))
        cursor = Qt.CursorShape.ArrowCursor
        best = (float("inf"), float("inf"))
        for overlay in self._overlays.values():
            handle = self._handle_hit(overlay, viewport_point)
            if handle is None:
                continue
            center = self._handle_centers(overlay)[handle]
            candidate = ((center - viewport_point).manhattanLength(), -overlay.zValue())
            if candidate < best:
                best = candidate
                cursor = self._cursor_for_handle(overlay, handle)
        if cursor == Qt.CursorShape.ArrowCursor:
            item = self._overlay_at(self.mapToScene(viewport_point.toPoint()))
            if item is not None and not item.locked:
                selected = [overlay for overlay in self._overlays.values() if overlay.isSelected()]
                if not item.isSelected() or not any(overlay.locked for overlay in selected):
                    cursor = Qt.CursorShape.SizeAllCursor
        self.viewport().setCursor(cursor)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        selected = [item for item in self._overlays.values() if item.isSelected() and not item.locked]
        if not selected:
            return
        painter.save()
        painter.resetTransform()
        blue = QColor("#0B78D0")
        for overlay in selected:
            for center in self._handle_centers(overlay):
                tile = QImage(8, 8, QImage.Format.Format_ARGB32)
                tile.fill(blue)
                if self._pixmap_item is not None and self._pixmap_item.isVisible() and self._preview_pixels is not None:
                    dpr = self._pixmap_item.pixmap().devicePixelRatio()
                    for y in range(8):
                        for x in range(8):
                            scene_point = self.mapToScene(round(center.x() + x - 4), round(center.y() + y - 4))
                            local = self._pixmap_item.mapFromScene(scene_point)
                            px, py = round(local.x() * dpr), round(local.y() * dpr)
                            if 0 <= px < self._preview_pixels.width() and 0 <= py < self._preview_pixels.height():
                                if self._preview_pixels.pixelColor(px, py) == blue:
                                    tile.setPixelColor(x, y, QColor("white"))
                painter.drawImage(QPointF(center.x() - 4, center.y() - 4), tile)
        painter.restore()

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
        viewport_point = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            item = self._overlay_at(point)
            if item is not None:
                if not item.isSelected():
                    self._syncing_selection = True
                    self.scene().clearSelection()
                    item.setSelected(True)
                    self._syncing_selection = False
                    self.selectionChanged.emit(self.selected_ids)
                self.nodeContextMenuRequested.emit(item.node_id, viewport_point.toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._tool is not None:
            tool = self._tool
            if not self._page_contains(point) or self._component is None or self._pixmap_item is None:
                self.cancel_tool()
                event.accept()
                return
            x_mm = point.x() / self._world_per_mm_x
            y_mm = point.y() / self._world_per_mm_y
            if tool in ("rectangle", "ellipse", "line"):
                self._draw_start = point
                if tool == "line":
                    self._draw_preview = self.scene().addLine(point.x(), point.y(), point.x(), point.y(), QPen(QColor("#14A44D"), 0, Qt.PenStyle.DashLine))
                else:
                    self._draw_preview = self.scene().addRect(QRectF(point, point), QPen(QColor("#14A44D"), 0, Qt.PenStyle.DashLine))
                self._draw_preview.setZValue(100000)
                event.accept()
                return
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
        hit_overlay = None
        hit_handle = None
        nearest = (float("inf"), float("inf"))
        for overlay in self._overlays.values():
            index = self._handle_hit(overlay, viewport_point)
            if index is None:
                continue
            center = self._handle_centers(overlay)[index]
            distance = (center.x() - viewport_point.x()) ** 2 + (center.y() - viewport_point.y()) ** 2
            candidate = (distance, -overlay.zValue())
            if candidate < nearest:
                nearest = candidate
                hit_overlay, hit_handle = overlay, index
        item = hit_overlay or self._overlay_at(point)
        if item is None:
            self.clear_selection()
            event.accept()
            return
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._syncing_selection = True
        if hit_overlay is not item:
            if ctrl:
                item.setSelected(not item.isSelected())
            elif not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
        self._syncing_selection = False
        self.selectionChanged.emit(self.selected_ids)
        self.viewport().update()
        selected = [overlay for overlay in self._overlays.values() if overlay.isSelected()]
        self._resize_handle = hit_handle if hit_overlay is item else self._handle_hit(item, viewport_point)
        if item.locked or (self._resize_handle is None and any(overlay.locked for overlay in selected)):
            self._drag_start = None
            self._drag_positions = {}
            event.accept()
            return
        self._drag_start = point
        self._drag_positions = {overlay.node_id: overlay.pos() for overlay in selected}
        self._resize_id = None
        self._resize_rect = None
        self._line_resize_id = None
        self._resize_local_start = None
        if self._resize_handle is not None:
            self._resize_id = item.node_id
            self._resize_rect = QRectF(item.rect())
            self._resize_local_start = item.mapFromScene(point)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._drag_start is None and self._draw_start is None) or not (event.buttons() & Qt.MouseButton.LeftButton):
            self._update_hover_cursor(event.position())
            super().mouseMoveEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        if self._draw_start is not None and self._draw_preview is not None:
            start = self._draw_start
            if self._tool == "line":
                self._draw_preview.setLine(start.x(), start.y(), point.x(), point.y())
            else:
                self._draw_preview.setRect(QRectF(start, point).normalized())
            event.accept()
            return
        delta = point - self._drag_start
        if self._resize_id is not None and self._resize_rect is not None:
            overlay = self._overlays[self._resize_id]
            rect = QRectF(self._resize_rect)
            local = overlay.mapFromScene(point)
            dx = local.x() - self._resize_local_start.x()
            dy = local.y() - self._resize_local_start.y()
            handle = self._resize_handle
            if handle in (0, 6, 7): rect.setLeft(min(rect.right() - 1, rect.left() + dx))
            if handle in (2, 3, 4): rect.setRight(max(rect.left() + 1, rect.right() + dx))
            if handle in (0, 1, 2): rect.setTop(min(rect.bottom() - 1, rect.top() + dy))
            if handle in (4, 5, 6): rect.setBottom(max(rect.top() + 1, rect.bottom() + dy))
            overlay.setRect(rect)
            self.viewport().update()
        else:
            for node_id, start in self._drag_positions.items():
                self._overlays[node_id].setPos(start + delta)
            self.viewport().update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._draw_start is not None and self._component is not None:
            start = self._draw_start
            end = self.mapToScene(event.position().toPoint())
            sx, sy = self._world_per_mm_x, self._world_per_mm_y
            tool = self._tool
            if self._draw_preview is not None and self._draw_preview.scene() is self.scene():
                self.scene().removeItem(self._draw_preview)
            self._draw_preview = None
            self._draw_start = None
            if tool and (abs(end.x() - start.x()) > 0.01 or abs(end.y() - start.y()) > 0.01):
                self.drawRequested.emit(tool, start.x() / sx, start.y() / sy, end.x() / sx, end.y() / sy)
            self._flush_deferred_model()
            self._update_hover_cursor(event.position())
            self.visualGestureFinished.emit()
            event.accept()
            return
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
            handle = self._resize_handle
            anchor_x = 1.0 if handle in (0, 6, 7) else 0.0 if handle in (2, 3, 4) else 0.5
            anchor_y = 1.0 if handle in (0, 1, 2) else 0.0 if handle in (4, 5, 6) else 0.5
            width_mm, height_mm = rect.width() * sx, rect.height() * sy
            node = locations(self._component)[node_id].node
            if isinstance(node, LineNode):
                width_mm = abs(node.dx_mm) + (rect.width() - self._resize_rect.width()) * sx
                height_mm = abs(node.dy_mm) + (rect.height() - self._resize_rect.height()) * sy
                if width_mm <= 0.001 and height_mm <= 0.001:
                    width_mm = 0.001
            width_mm *= (node.transform.m11 ** 2 + node.transform.m21 ** 2) ** 0.5
            height_mm *= (node.transform.m12 ** 2 + node.transform.m22 ** 2) ** 0.5
            if not isinstance(node, LineNode):
                width_mm, height_mm = max(0.001, width_mm), max(0.001, height_mm)
            self.resizeRequested.emit(node_id, width_mm, height_mm, anchor_x, anchor_y)
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
        self._resize_handle = None
        self._resize_local_start = None
        self._flush_deferred_model()
        self._update_hover_cursor(event.position())
        self.visualGestureFinished.emit()
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
        component_before = self._component
        ids = self._nudge_ids
        delta = QPointF(self._nudge_delta)
        starts = dict(self._nudge_starts)
        self._nudge_key = None
        self._nudge_ids = ()
        self._nudge_delta = QPointF()
        self._nudge_starts.clear()
        if component_before is not None and self._pixmap_item is not None and (delta.x() or delta.y()):
            sx = 1 / self._world_per_mm_x
            sy = 1 / self._world_per_mm_y
            self.nudgeRequested.emit(ids, delta.x() * sx, delta.y() * sy)
            if self._component is component_before:
                for node_id, start in starts.items():
                    if node_id in self._overlays:
                        self._overlays[node_id].setPos(start)
        self._flush_deferred_model()
        self.visualGestureFinished.emit()

    def focusOutEvent(self, event) -> None:
        self._finish_nudge()
        super().focusOutEvent(event)

    def fit_page(self) -> None:
        if not self._has_image or self._pixmap_item is None:
            return
        self._fit_mode = True
        _set_fit_scrollbars(self, True)
        self._fitting = True
        try:
            self.fitInView(self._pixmap_item.sceneBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            self._fitting = False
        self.zoomChanged.emit(self.zoom_percent)

    def actual_size(self) -> None:
        if self._component is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        self.resetTransform()
        if self._pixmap_item is not None:
            self.centerOn(self._pixmap_item.sceneBoundingRect().center())
        self.zoomChanged.emit(100)

    def zoom_by(self, factor: float) -> None:
        if self._component is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        _scale_view(self, factor)
        self.zoomChanged.emit(self.zoom_percent)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_mode and self._has_image and not self._fitting:
            self.fit_page()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)
