"""Interactive scene projection for visual component editing."""

from __future__ import annotations

import math
from uuid import uuid4

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import QLabel, QFrame, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGraphicsLineItem

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, LineNode, Node
from componentpress.domain.geometry import Affine, local_bounds
from componentpress.domain.tree import effectively_locked_ids, locations
from componentpress.editor.theme import is_dark_theme, theme_palette
from componentpress.editor.canvas_rulers import CanvasRuler
from componentpress.diagnostics.logging import log_event
from componentpress.editor.canvas_gestures import CanvasGesture, DrawGesture, MoveGesture, NudgeGesture, ResizeGesture
from componentpress.i18n import tr


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


def _overlay_bounds(node, scale_x, scale_y):
    left, top, width, height = local_bounds(node)
    if isinstance(node, LineNode):
        left, width = _minimum_line_axis(left, width, scale_x)
        top, height = _minimum_line_axis(top, height, scale_y)
    return left, top, width, height


def _minimum_line_axis(origin, size, scale):
    if size * scale >= 8:
        return origin, size
    widened = 8 / scale
    return origin - (widened - size) / 2, widened


def _overlay_transform(world, scale_x, scale_y):
    return QTransform(world.a, world.c * scale_y / scale_x, world.b * scale_x / scale_y, world.d, 0, 0)


def _line_endpoint(node, left, top, scale_x, scale_y):
    if not isinstance(node, LineNode):
        return None
    return QPointF((node.dx_mm - left) * scale_x, (node.dy_mm - top) * scale_y)


def _screen_handle_direction(canvas, overlay, handle):
    dx, dy = ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0))[handle]
    center = canvas.viewportTransform().map(overlay.mapToScene(QPointF()))
    x_axis = _screen_axis(canvas, overlay, center, dx, horizontal=True)
    y_axis = _screen_axis(canvas, overlay, center, dy, horizontal=False)
    if x_axis.isNull():
        return y_axis
    if y_axis.isNull():
        return x_axis
    x_axis /= math.hypot(x_axis.x(), x_axis.y())
    y_axis /= math.hypot(y_axis.x(), y_axis.y())
    return x_axis + y_axis


def _screen_axis(canvas, overlay, center, direction, *, horizontal):
    if not direction:
        return QPointF()
    point = QPointF(direction, 0) if horizontal else QPointF(0, direction)
    return canvas.viewportTransform().map(overlay.mapToScene(point)) - center


def _near_group_border(rect, point, tolerance):
    distances = (abs(point.x() - rect.left()), abs(point.x() - rect.right()),
                 abs(point.y() - rect.top()), abs(point.y() - rect.bottom()))
    return min(distances) <= tolerance


def _placement_rect(x, y, width, height, scale_x, scale_y):
    return QRectF(x * scale_x, y * scale_y, width * scale_x, height * scale_y)


def _resized_overlay(gesture: ResizeGesture, overlay: NodeOverlay, point: QPointF) -> QRectF:
    rect = QRectF(gesture.rect)
    local = overlay.mapFromScene(point)
    dx, dy = local.x() - gesture.local_start.x(), local.y() - gesture.local_start.y()
    if gesture.handle in (0, 6, 7):
        rect.setLeft(min(rect.right() - 1, rect.left() + dx))
    if gesture.handle in (2, 3, 4):
        rect.setRight(max(rect.left() + 1, rect.right() + dx))
    if gesture.handle in (0, 1, 2):
        rect.setTop(min(rect.bottom() - 1, rect.top() + dy))
    if gesture.handle in (4, 5, 6):
        rect.setBottom(max(rect.top() + 1, rect.bottom() + dy))
    return rect


def _resize_dimensions(rect, start, node, world_x, world_y):
    sx, sy = 1 / world_x, 1 / world_y
    width, height = rect.width() * sx, rect.height() * sy
    if isinstance(node, LineNode):
        width = abs(node.dx_mm) + (rect.width() - start.width()) * sx
        height = abs(node.dy_mm) + (rect.height() - start.height()) * sy
        if width <= 0.001 and height <= 0.001:
            width = 0.001
    width *= math.hypot(node.transform.m11, node.transform.m21)
    height *= math.hypot(node.transform.m12, node.transform.m22)
    if not isinstance(node, LineNode):
        width, height = max(0.001, width), max(0.001, height)
    return width, height


def _resize_anchor(handle):
    x = 1.0 if handle in (0, 6, 7) else 0.0 if handle in (2, 3, 4) else 0.5
    y = 1.0 if handle in (0, 1, 2) else 0.0 if handle in (4, 5, 6) else 0.5
    return x, y


def _nudge_step(key):
    return {
        Qt.Key.Key_Left: (-1.0, 0.0), Qt.Key.Key_Right: (1.0, 0.0),
        Qt.Key.Key_Up: (0.0, -1.0), Qt.Key.Key_Down: (0.0, 1.0),
    }.get(key)


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
        self._configure_view()
        self._initialize_canvas_state()
        self._create_rulers()
        self.scene().selectionChanged.connect(self._scene_selection_changed)

    def _create_rulers(self) -> None:
        self._ruler_size = 24
        self.setViewportMargins(self._ruler_size, self._ruler_size, 0, 0)
        self.corner = QLabel(tr("ui.millimetres"), self)
        self.corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.horizontal_ruler = CanvasRuler(self, True)
        self.vertical_ruler = CanvasRuler(self, False)
        self.horizontalScrollBar().valueChanged.connect(self._update_rulers)
        self.verticalScrollBar().valueChanged.connect(self._update_rulers)
        self._update_rulers()

    def _update_rulers(self, *_args) -> None:
        viewport = self.viewport().geometry()
        self.corner.setGeometry(0, 0, viewport.x(), viewport.y())
        self.horizontal_ruler.setGeometry(viewport.x(), 0, viewport.width(), viewport.y())
        self.vertical_ruler.setGeometry(0, viewport.y(), viewport.x(), viewport.height())
        position = getattr(self, "_cursor_view_pos", None)
        point = self.mapToScene(position) if position is not None else None
        self.horizontal_ruler.set_cursor(point.x() / self._world_per_mm_x if point is not None else None)
        self.vertical_ruler.set_cursor(point.y() / self._world_per_mm_y if point is not None else None)

    def _configure_view(self):
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

    def _initialize_canvas_state(self):
        self._fit_mode = True
        self._fitting = False
        self._deferred_model: tuple[ComponentDefinition, tuple[str, ...], object] | None = None
        self._deferred_document: tuple[QImage, ComponentDefinition, bool, tuple[str, ...]] | None = None
        self._has_image = False
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._preview_pixels: QImage | None = None
        self._component: ComponentDefinition | None = None
        self._overlays: dict[str, NodeOverlay] = {}
        self._tool: str | None = None
        self._gesture: CanvasGesture | None = None
        self._finishing_gesture = False
        self._dispatch_context = None
        self._edit_context: object = None
        self._gesture_operation_id: str | None = None
        self._syncing_selection = False
        self._world_per_mm_x = 96.0 / 25.4
        self._world_per_mm_y = 96.0 / 25.4

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

    @property
    def available_node_ids(self) -> frozenset[str]:
        return frozenset(self._overlays)

    @property
    def gesture_active(self) -> bool:
        return self._gesture is not None or self._finishing_gesture

    def set_tool(self, tool: str | None) -> None:
        if self._gesture is not None:
            self.cancel_gesture()
        self._tool = tool
        self._update_hover_cursor()
        if tool:
            self.clear_selection()

    def cancel_tool(self) -> None:
        if self._tool is None:
            return
        self._tool = None
        self.cancel_gesture()
        self._update_hover_cursor()
        self.toolCancelled.emit()

    def set_document(
        self,
        image: QImage,
        component: ComponentDefinition,
        *,
        fit: bool = False,
        selected: tuple[str, ...] = (),
    ) -> None:
        if self._defer_document_update(image, component, fit, selected):
            return
        self._component = component
        self._set_document_scene(image, component, selected)
        self._fit_document(fit)

    def _defer_document_update(self, image, component, fit, selected):
        if not self.gesture_active:
            return False
        if self._gesture and component != self._gesture.component:
            self.cancel_gesture()
            return False
        self._deferred_document = image, component, fit, selected
        return True

    def _set_document_scene(self, image, component, selected):
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
        self._set_scene_rect()
        self._has_image = True

    def _fit_document(self, fit):
        if not self._has_image or self._pixmap_item is None:
            return
        if fit or self._fit_mode:
            self.fit_page()
        else:
            self.zoomChanged.emit(self.zoom_percent)

    def _set_scene_rect(self):
        if self._pixmap_item is not None:
            rect = self._pixmap_item.sceneBoundingRect().adjusted(-24, -24, 24, 24)
            self.scene().setSceneRect(rect)

    def set_component_model(self, component: ComponentDefinition, selected: tuple[str, ...] = (), *, context=None) -> None:
        """Refresh hit targets and lock indicators even if preview rendering failed."""
        if self._defer_or_cancel_model(component, selected, context):
            return
        self._edit_context = context if context is not None else self._edit_context
        self._component = component
        self._sync_model_overlays(component, selected)
        self.viewport().update()

    def _defer_or_cancel_model(self, component, selected, context) -> bool:
        if not self.gesture_active:
            return False
        changed_context = context is not None and context != self._gesture_context()
        changed_model = context is None and self._gesture is not None and component != self._gesture.component
        if (changed_context or changed_model) and not self._finishing_gesture:
            self.cancel_gesture()
            return False
        self._deferred_model = component, selected, context
        return True

    def _gesture_context(self):
        return None if self._gesture is None else self._gesture.context

    def set_preview_pending(self, pending: bool) -> None:
        if self._pixmap_item is not None:
            self.viewport().update()

    def _sync_model_overlays(self, component: ComponentDefinition, selected: tuple[str, ...]) -> None:
        self._syncing_selection = True
        self._clear_overlays()
        if self._pixmap_item is None:
            self._syncing_selection = False
            return
        index, locked = locations(component), effectively_locked_ids(component)
        self._add_node_overlays(component.elements, index, locked, 0)
        for node_id in selected:
            if node_id in self._overlays:
                self._overlays[node_id].setSelected(True)
        self._set_scene_rect()
        self._syncing_selection = False
        self.viewport().update()

    def _clear_overlays(self):
        scene = self.scene()
        for item in tuple(self._overlays.values()):
            if item.scene() is scene:
                scene.removeItem(item)
        self._overlays.clear()

    def _add_node_overlays(self, nodes, index, locked, order):
        for node in nodes:
            order += 1
            self._add_node_overlay(node, index[node.id], locked, order)
            if isinstance(node, GroupNode):
                order = self._add_node_overlays(node.children, index, locked, order)
        return order

    def _add_node_overlay(self, node, location, locked, order):
        left, top, width, height = _overlay_bounds(location.node, self._world_per_mm_x, self._world_per_mm_y)
        bounds = QRectF(left * self._world_per_mm_x, top * self._world_per_mm_y,
                        width * self._world_per_mm_x, height * self._world_per_mm_y)
        transform = _overlay_transform(location.world, self._world_per_mm_x, self._world_per_mm_y)
        endpoint = _line_endpoint(location.node, left, top, self._world_per_mm_x, self._world_per_mm_y)
        overlay = NodeOverlay(node.id, isinstance(node, GroupNode), node.id in locked, bounds, float(order), endpoint)
        overlay.setPos(location.world.x * self._world_per_mm_x, location.world.y * self._world_per_mm_y)
        overlay.setTransform(transform)
        self.scene().addItem(overlay)
        self._overlays[node.id] = overlay

    def set_image(self, image: QImage, *, fit: bool = False) -> None:
        """Compatibility helper for image-only callers."""
        self.cancel_gesture()
        scene = self.scene()
        scene.clear()
        self._overlays.clear()
        self._component = None
        self._edit_context = None
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
        self.cancel_gesture()
        self.scene().clear()
        self._overlays.clear()
        self._pixmap_item = None
        self._preview_pixels = None
        self._component = None
        self._edit_context = None
        self._has_image = False
        self.selectionChanged.emit(())
        self.viewport().update()

    def release_preview(self) -> None:
        """Drop the high-resolution backing image while retaining scene/model state."""
        self.cancel_gesture()
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
            if abs(center.x() - viewport_point.x()) <= 6 and abs(center.y() - viewport_point.y()) <= 6
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

    def cancel_gesture(self) -> None:
        gesture = self._gesture
        if gesture is None or self._finishing_gesture:
            return
        self._gesture = None
        self._restore_gesture_overlay(gesture)
        self._end_gesture(gesture, "cancelled")

    def report_gesture_outcome(self, accepted: bool) -> None:
        if self._finishing_gesture:
            self._gesture_outcome = "succeeded" if accepted else "failed"

    @property
    def dispatch_context(self):
        return self._dispatch_context

    def _restore_gesture_overlay(self, gesture) -> None:
        if isinstance(gesture, DrawGesture):
            if gesture.preview.scene() is self.scene():
                self.scene().removeItem(gesture.preview)
        elif isinstance(gesture, ResizeGesture):
            overlay = self._overlays.get(gesture.node_id)
            if overlay is not None:
                overlay.setRect(gesture.rect)
        elif isinstance(gesture, (MoveGesture, NudgeGesture)):
            self._restore_positions(gesture.starts)

    def _restore_positions(self, positions) -> None:
        for node_id, point in positions.items():
            overlay = self._overlays.get(node_id)
            if overlay is not None:
                overlay.setPos(point)

    def _flush_deferred_state(self) -> None:
        if self._deferred_model is not None:
            self._flush_deferred_model()
        if self._deferred_document is not None and not self.gesture_active:
            image, component, fit, selected = self._deferred_document
            self._deferred_document = None
            self.set_document(image, component, fit=fit, selected=selected)

    def _flush_deferred_model(self) -> None:
        if self._deferred_model is None or self.gesture_active:
            return
        component, selected, context = self._deferred_model
        self._deferred_model = None
        still_present = tuple(node_id for node_id in self.selected_ids if node_id in locations(component))
        self.set_component_model(component, still_present or selected, context=context)

    def _log_gesture(self, status, context=None) -> None:
        if self._gesture_operation_id is None:
            self._gesture_operation_id = uuid4().hex
        phase = "preparing" if status == "started" else status
        try:
            log_event("canvas.gesture", operation="edit", operation_id=self._gesture_operation_id,
                      phase=phase, status=status)
        except Exception:
            pass

    def _cursor_for_handle(self, overlay: NodeOverlay, handle: int) -> Qt.CursorShape:
        direction = _screen_handle_direction(self, overlay, handle)
        angle = math.degrees(math.atan2(direction.y(), direction.x())) % 180
        cursors = (
            (0, Qt.CursorShape.SizeHorCursor), (45, Qt.CursorShape.SizeFDiagCursor),
            (90, Qt.CursorShape.SizeVerCursor), (135, Qt.CursorShape.SizeBDiagCursor),
        )
        return min(cursors, key=lambda item: min(abs(angle - item[0]), 180 - abs(angle - item[0])))[1]

    def _update_hover_cursor(self, point: QPointF | None = None) -> None:
        if self._tool is not None:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            return
        viewport_point = self._cursor_point(point)
        cursor = self._handle_cursor_at(viewport_point) or self._move_cursor_at(viewport_point)
        self.viewport().setCursor(cursor or Qt.CursorShape.ArrowCursor)

    def _cursor_point(self, point):
        return QPointF(point) if point is not None else QPointF(self.viewport().mapFromGlobal(QCursor.pos()))

    def _handle_cursor_at(self, point):
        candidates = []
        for overlay in self._overlays.values():
            handle = self._handle_hit(overlay, point)
            if handle is not None:
                distance = (self._handle_centers(overlay)[handle] - point).manhattanLength()
                candidates.append((distance, -overlay.zValue(), overlay, handle))
        if not candidates:
            return None
        _distance, _z, overlay, handle = min(candidates, key=lambda value: value[:2])
        return self._cursor_for_handle(overlay, handle)

    def _move_cursor_at(self, point):
        item = self._overlay_at(self.mapToScene(point.toPoint()))
        if item is None or item.locked:
            return None
        selected = [overlay for overlay in self._overlays.values() if overlay.isSelected()]
        if item.isSelected() and any(overlay.locked for overlay in selected):
            return None
        return Qt.CursorShape.SizeAllCursor

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        selected = self._selected_unlocked_overlays()
        if not selected:
            return
        painter.save()
        painter.resetTransform()
        for overlay in selected:
            for center in self._handle_centers(overlay):
                self._draw_handle_marker(painter, center)
        painter.restore()

    def _selected_unlocked_overlays(self):
        return [item for item in self._overlays.values() if item.isSelected() and not item.locked]

    def _draw_handle_marker(self, painter, center):
        tile = QImage(8, 8, QImage.Format.Format_ARGB32)
        tile.fill(QColor("#0B78D0"))
        self._whiten_matching_pixels(tile, center)
        painter.drawImage(QPointF(center.x() - 4, center.y() - 4), tile)

    def _whiten_matching_pixels(self, tile, center):
        if self._pixmap_item is None or not self._pixmap_item.isVisible() or self._preview_pixels is None:
            return
        dpr = self._pixmap_item.pixmap().devicePixelRatio()
        for y in range(8):
            for x in range(8):
                px, py = self._preview_pixel(center, x, y, dpr)
                if 0 <= px < self._preview_pixels.width() and 0 <= py < self._preview_pixels.height():
                    if self._preview_pixels.pixelColor(px, py) == QColor("#0B78D0"):
                        tile.setPixelColor(x, y, QColor("white"))

    def _preview_pixel(self, center, x, y, dpr):
        point = self.mapToScene(round(center.x() + x - 4), round(center.y() + y - 4))
        local = self._pixmap_item.mapFromScene(point)
        return round(local.x() * dpr), round(local.y() * dpr)

    def _overlay_at(self, point: QPointF) -> NodeOverlay | None:
        overlays = [item for item in self.scene().items(point) if isinstance(item, NodeOverlay)]
        selected = next((item for item in overlays if item.is_group and item.isSelected()), None)
        if selected is not None:
            return selected
        return self._group_border_at(overlays, point) or next(iter(overlays), None)

    def _group_border_at(self, overlays, point):
        tolerance = 7.0 / max(0.1, self.transform().m11())
        for item in overlays:
            if item.is_group and _near_group_border(item.sceneBoundingRect(), point, tolerance):
                return item
        return None

    def _page_contains(self, point: QPointF) -> bool:
        return self._pixmap_item is not None and self._pixmap_item.sceneBoundingRect().contains(point)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._finishing_gesture:
            event.accept()
            return
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            self._right_click(event, point)
        elif event.button() == Qt.MouseButton.LeftButton and self._tool is not None:
            self._begin_tool_gesture(event, point)
        elif event.button() == Qt.MouseButton.LeftButton:
            self._begin_node_gesture(event, point)
        else:
            super().mousePressEvent(event)

    def _right_click(self, event, point):
        item = self._overlay_at(point)
        if item is not None:
            self._select_context_item(item)
            self.nodeContextMenuRequested.emit(item.node_id, event.position().toPoint())
        event.accept()

    def _select_context_item(self, item):
        if item.isSelected():
            return
        self._syncing_selection = True
        self.scene().clearSelection()
        item.setSelected(True)
        self._syncing_selection = False
        self.selectionChanged.emit(self.selected_ids)

    def _begin_tool_gesture(self, event, point):
        if not self._page_contains(point) or self._component is None or self._pixmap_item is None:
            self.cancel_tool()
        elif self._tool in {"rectangle", "ellipse", "line"}:
            self._begin_draw(point)
        else:
            self._place_tool(point)
        event.accept()

    def _begin_draw(self, point):
        preview = self._draw_item(point)
        gesture = DrawGesture(QPointF(point), self._tool, preview, self._component, self._edit_context)
        self._start_gesture(gesture)

    def _draw_item(self, point):
        pen = QPen(QColor("#14A44D"), 0, Qt.PenStyle.DashLine)
        if self._tool == "line":
            item = self.scene().addLine(point.x(), point.y(), point.x(), point.y(), pen)
        else:
            item = self.scene().addRect(QRectF(point, point), pen)
        item.setZValue(100000)
        return item

    def _place_tool(self, point):
        x, y = point.x() / self._world_per_mm_x, point.y() / self._world_per_mm_y
        width, height = self._placement_size(x, y)
        preview = None
        if width is not None and height is not None:
            preview = self.scene().addRect(
                _placement_rect(x, y, width, height, self._world_per_mm_x, self._world_per_mm_y),
                QPen(QColor("#14A44D"), 0, Qt.PenStyle.DashLine),
            )
        self.placementRequested.emit(self._tool, x, y)
        if preview is not None:
            self.scene().removeItem(preview)

    def _placement_size(self, x, y):
        if self._tool == "image":
            size = min(30.0, 0.75 * min(self._component.size_mm.width, self._component.size_mm.height),
                       self._component.size_mm.width - x, self._component.size_mm.height - y)
            return (size, size) if size >= 0.001 else (None, None)
        width, height = 40.0, 15.0
        width = max(0.001, min(width, self._component.size_mm.width - x))
        height = max(0.001, min(height, self._component.size_mm.height - y))
        return width, height

    def _begin_node_gesture(self, event, point):
        overlay, handle = self._hit_overlay(event.position())
        item = overlay or self._overlay_at(point)
        if item is None:
            self.clear_selection()
        else:
            self._select_pressed_item(item, overlay, event)
            self._begin_node_drag(item, handle, event.position(), point)
        event.accept()

    def _hit_overlay(self, point):
        candidates = []
        for overlay in self._overlays.values():
            handle = self._handle_hit(overlay, point)
            if handle is not None:
                center = self._handle_centers(overlay)[handle]
                distance = (center - point).manhattanLength()
                candidates.append((distance, -overlay.zValue(), overlay, handle))
        if not candidates:
            return None, None
        _distance, _z, overlay, handle = min(candidates, key=lambda value: value[:2])
        return overlay, handle

    def _select_pressed_item(self, item, hit_overlay, event):
        self._syncing_selection = True
        if hit_overlay is not item:
            self._select_by_modifiers(item, event.modifiers())
        self._syncing_selection = False
        self.selectionChanged.emit(self.selected_ids)
        self.viewport().update()

    def _select_by_modifiers(self, item, modifiers):
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            item.setSelected(not item.isSelected())
        elif not item.isSelected():
            self.scene().clearSelection()
            item.setSelected(True)

    def _begin_node_drag(self, item, hit_handle, point, scene_point):
        selected = [overlay for overlay in self._overlays.values() if overlay.isSelected()]
        handle = hit_handle if hit_handle is not None else self._handle_hit(item, point)
        if item.locked or (handle is None and any(overlay.locked for overlay in selected)):
            return
        starts = {overlay.node_id: overlay.pos() for overlay in selected}
        gesture = self._resize_gesture(item, handle, scene_point) if handle is not None else None
        gesture = gesture or MoveGesture(QPointF(scene_point), starts, self._component, self._edit_context)
        self._start_gesture(gesture)

    def _resize_gesture(self, item, handle, point):
        if item.is_group and self._group_has_locked_child(item.node_id):
            return None
        return ResizeGesture(QPointF(point), item.node_id, handle, QRectF(item.rect()), item.mapFromScene(point),
                             self._component, self._edit_context)

    def _group_has_locked_child(self, node_id):
        if self._component is None:
            return False
        index = locations(self._component)
        locked = effectively_locked_ids(self._component)
        return any(node in locked and self._is_descendant(index, node, node_id) for node in index)

    def _start_gesture(self, gesture):
        self._gesture = gesture
        self._gesture_operation_id = uuid4().hex
        self._log_gesture("started")

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._cursor_view_pos = event.position().toPoint()
        self._update_rulers()
        gesture = self._gesture
        if gesture is None or not event.buttons() & Qt.MouseButton.LeftButton:
            self._update_hover_cursor(event.position())
            super().mouseMoveEvent(event)
        else:
            self._update_gesture(gesture, self.mapToScene(event.position().toPoint()))
            event.accept()

    def _update_gesture(self, gesture, point):
        if isinstance(gesture, DrawGesture):
            self._update_draw(gesture, point)
        elif isinstance(gesture, ResizeGesture):
            self._update_resize(gesture, point)
        elif isinstance(gesture, MoveGesture):
            self._update_move(gesture, point)

    def _update_draw(self, gesture, point):
        if self._tool == "line":
            gesture.preview.setLine(gesture.start.x(), gesture.start.y(), point.x(), point.y())
        else:
            gesture.preview.setRect(QRectF(gesture.start, point).normalized())

    def _update_move(self, gesture, point):
        delta = point - gesture.start
        for node_id, start in gesture.starts.items():
            overlay = self._overlays.get(node_id)
            if overlay is not None:
                overlay.setPos(start + delta)
        self.viewport().update()

    def _update_resize(self, gesture, point):
        overlay = self._overlays.get(gesture.node_id)
        if overlay is None:
            self.cancel_gesture()
            return
        overlay.setRect(_resized_overlay(gesture, overlay, point))
        self.viewport().update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        gesture = self._gesture
        if gesture is None:
            super().mouseReleaseEvent(event)
        elif isinstance(gesture, DrawGesture):
            self._finish_draw(gesture, event)
            event.accept()
        elif isinstance(gesture, ResizeGesture):
            self._finish_resize(gesture)
            event.accept()
        elif isinstance(gesture, MoveGesture):
            self._finish_move(gesture, event)
            event.accept()

    def _finish_draw(self, gesture, event):
        end = self.mapToScene(event.position().toPoint())
        start = gesture.start
        self.scene().removeItem(gesture.preview)
        if abs(end.x() - start.x()) > 0.01 or abs(end.y() - start.y()) > 0.01:
            sx, sy = self._world_per_mm_x, self._world_per_mm_y
            self._dispatch_gesture(gesture, lambda: self.drawRequested.emit(
                gesture.tool, start.x() / sx, start.y() / sy, end.x() / sx, end.y() / sy,
            ))
        else:
            self._gesture = None
            self._end_gesture(gesture, "succeeded")

    def _finish_resize(self, gesture):
        values = self._resize_request(gesture)
        if values is None:
            self.cancel_gesture()
            return
        self._restore_gesture_overlay(gesture)
        self._dispatch_gesture(gesture, lambda: self.resizeRequested.emit(*values))

    def _resize_request(self, gesture):
        overlay = self._overlays.get(gesture.node_id)
        node = locations(self._component).get(gesture.node_id) if self._component else None
        if overlay is None or node is None:
            return None
        width, height = _resize_dimensions(overlay.rect(), gesture.rect, node.node, self._world_per_mm_x, self._world_per_mm_y)
        anchor_x, anchor_y = _resize_anchor(gesture.handle)
        return gesture.node_id, width, height, anchor_x, anchor_y

    def _finish_move(self, gesture, event):
        point = self.mapToScene(event.position().toPoint())
        dx, dy = point.x() - gesture.start.x(), point.y() - gesture.start.y()
        if abs(dx) <= 0.01 and abs(dy) <= 0.01:
            self._gesture = None
            self._end_gesture(gesture, "succeeded")
            return
        self._restore_gesture_overlay(gesture)
        self._dispatch_gesture(gesture, lambda: self.moveRequested.emit(
            tuple(gesture.starts), dx / self._world_per_mm_x, dy / self._world_per_mm_y,
        ))

    def _dispatch_gesture(self, gesture, request):
        self._gesture = None
        self._finishing_gesture = True
        self._dispatch_context = gesture.context
        self._gesture_outcome = "succeeded"
        try:
            request()
        finally:
            self._end_gesture(gesture, self._gesture_outcome)

    def _end_gesture(self, gesture, status):
        self._finishing_gesture = False
        self._dispatch_context = None
        self._log_gesture(status)
        self._gesture_operation_id = None
        self._flush_deferred_state()
        self._update_hover_cursor()
        self.visualGestureFinished.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and (self.gesture_active or self._tool is not None):
            self.cancel_gesture()
            if self._tool is not None:
                self.cancel_tool()
            event.accept()
        elif event.key() == Qt.Key.Key_Delete and self.selected_ids:
            self.deleteRequested.emit()
            event.accept()
        elif not self._start_or_update_nudge(event):
            super().keyPressEvent(event)
        else:
            event.accept()

    def _start_or_update_nudge(self, event):
        step = _nudge_step(event.key())
        if step is None or self._component is None or self._pixmap_item is None or not self.selected_ids:
            return False
        return self._apply_nudge_step(event, step)

    def _apply_nudge_step(self, event, step):
        ids = self.selected_ids
        if any(self._overlays[node_id].locked for node_id in ids):
            return True
        gesture = self._gesture
        if not isinstance(gesture, NudgeGesture):
            if gesture is not None:
                self.cancel_gesture()
            gesture = self._begin_nudge(event.key(), ids)
        elif gesture.key != event.key() or gesture.node_ids != ids:
            self._finish_nudge()
            return True
        self._add_nudge_delta(gesture, event)
        return True

    def _begin_nudge(self, key, ids):
        starts = {node_id: self._overlays[node_id].pos() for node_id in ids}
        gesture = NudgeGesture(key, ids, starts, self._component, self._edit_context)
        self._start_gesture(gesture)
        return gesture

    def _add_nudge_delta(self, gesture, event):
        amount = 1.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.1
        dx, dy = _nudge_step(event.key())
        gesture.delta += QPointF(dx * amount * self._world_per_mm_x, dy * amount * self._world_per_mm_y)
        self._restore_positions({key: point + gesture.delta for key, point in gesture.starts.items()})

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if isinstance(self._gesture, NudgeGesture) and event.key() == self._gesture.key and not event.isAutoRepeat():
            self._finish_nudge()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def _finish_nudge(self):
        gesture = self._gesture
        if not isinstance(gesture, NudgeGesture):
            return
        if gesture.delta.isNull():
            self._gesture = None
            self._end_gesture(gesture, "succeeded")
            return
        dx, dy = gesture.delta.x() / self._world_per_mm_x, gesture.delta.y() / self._world_per_mm_y
        self._restore_positions(gesture.starts)
        self._dispatch_gesture(gesture, lambda: self.nudgeRequested.emit(gesture.node_ids, dx, dy))

    def focusOutEvent(self, event) -> None:
        if isinstance(self._gesture, NudgeGesture):
            self._finish_nudge()
        elif not self._finishing_gesture:
            self.cancel_gesture()
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
        self._update_rulers()
        self.zoomChanged.emit(self.zoom_percent)

    def actual_size(self) -> None:
        if self._component is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        self.resetTransform()
        if self._pixmap_item is not None:
            self.centerOn(self._pixmap_item.sceneBoundingRect().center())
        self._update_rulers()
        self.zoomChanged.emit(100)

    def zoom_by(self, factor: float) -> None:
        if self._component is None:
            return
        self._fit_mode = False
        _set_fit_scrollbars(self, False)
        _scale_view(self, factor)
        self._update_rulers()
        self.zoomChanged.emit(self.zoom_percent)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_rulers()
        if self._fit_mode and self._has_image and not self._fitting:
            self.fit_page()

    def leaveEvent(self, event) -> None:
        self._cursor_view_pos = None
        self._update_rulers()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)
