"""Tree-order painting in millimetres."""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGradient, QPainter, QPen, QBrush, QTransform

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, Node, ShapeNode
from .geometry import HTML_DPI, MM_PER_INCH, image_fit_geometry
from .html_document import prepare_html
from .resources import ProjectResourceLoader


# PySide 6.11 resolves this fillRect overload lazily. Finish its converter before
# worker threads can observe a partially initialized QGradient.Preset binding.
_GRADIENT_PRESET = QGradient.Preset


@dataclass
class _ComponentPainter:
    painter: QPainter
    component: ComponentDefinition
    loader: ProjectResourceLoader
    owner: Path
    variables: dict[str, str]
    bindings_resolved: bool

    def paint(self) -> None:
        bounds = QRectF(0, 0, self.component.size_mm.width, self.component.size_mm.height)
        self.painter.save()
        try:
            self.painter.setClipRect(bounds, Qt.ClipOperation.ReplaceClip)
            self.painter.fillRect(bounds, QColor(self.component.background))
            self.nodes(self.component.elements, 0, 0)
        finally:
            self.painter.restore()

    def nodes(self, nodes: tuple[Node, ...], offset_x: float, offset_y: float) -> None:
        for node in nodes:
            self.node(node, offset_x, offset_y)

    def node(self, node: Node, offset_x: float, offset_y: float) -> None:
        self.painter.save()
        try:
            self._transform(node, offset_x, offset_y)
            self._draw(node)
        finally:
            self.painter.restore()

    def _transform(self, node: Node, offset_x: float, offset_y: float) -> None:
        self.painter.translate(offset_x + node.x_mm, offset_y + node.y_mm)
        transform = node.transform
        matrix = QTransform(transform.m11, transform.m21, transform.m12, transform.m22, 0, 0)
        self.painter.setTransform(matrix, True)

    def _draw(self, node: Node) -> None:
        if isinstance(node, ImageNode):
            self._image(node)
        elif isinstance(node, HtmlNode):
            self._html(node)
        elif isinstance(node, ShapeNode):
            self._shape(node)
        elif isinstance(node, LineNode):
            self._line(node)
        elif isinstance(node, ConditionalGroupNode):
            if node.condition_matches is True:
                self.nodes(node.children, 0, 0)
        elif isinstance(node, GroupNode):
            self.nodes(node.children, 0, 0)
        else:
            raise ProjectError(Diagnostic("NODE_UNSUPPORTED", f"тип узла {type(node).__name__} не поддерживается", self.owner))

    def _resolve(self, value: str, *, html: bool, field: str) -> str:
        if self.bindings_resolved:
            return value
        from componentpress.bindings.static import resolve_static

        return resolve_static(value, self.variables, html=html, owner=self.owner, field=field)

    def _image(self, node: ImageNode) -> None:
        source = self._resolve(node.source, html=False, field=f"elements.{node.id}.source")
        if source:
            _draw_image(self.painter, node, self.loader, self.owner, 0, 0, source)

    def _html(self, node: HtmlNode) -> None:
        html = self._resolve(node.html, html=True, field=f"elements.{node.id}.html")
        prepared = prepare_html(node, html, self.loader, self.owner)
        self.painter.save()
        try:
            self.painter.setClipRect(QRectF(0, 0, node.width_mm, node.height_mm), Qt.ClipOperation.IntersectClip)
            self.painter.scale(MM_PER_INCH / HTML_DPI, MM_PER_INCH / HTML_DPI)
            prepared.draw(self.painter)
        finally:
            self.painter.restore()

    def _shape(self, node: ShapeNode) -> None:
        self._set_pen(node.stroke, node.stroke_width_mm, node.stroke_style)
        self.painter.setBrush(QBrush(QColor(node.fill)) if node.fill is not None else Qt.BrushStyle.NoBrush)
        rect = QRectF(0, 0, node.width_mm, node.height_mm)
        (self.painter.drawRect if node.type == "rectangle" else self.painter.drawEllipse)(rect)

    def _line(self, node: LineNode) -> None:
        self._set_pen(node.stroke, node.stroke_width_mm, node.stroke_style)
        self.painter.drawLine(0, 0, node.dx_mm, node.dy_mm)

    def _set_pen(self, color: str, width_mm: float, style_name: str) -> None:
        style = {"solid": Qt.PenStyle.SolidLine, "dash": Qt.PenStyle.DashLine, "dot": Qt.PenStyle.DotLine}[style_name]
        pen = QPen(QColor(color), 0, style)
        pen.setWidthF(width_mm)
        self.painter.setPen(pen)


def _draw_image(painter: QPainter, node: ImageNode, loader: ProjectResourceLoader, owner: Path,
                x: float, y: float, source: str) -> None:
    image = loader.image(source, owner=owner, field=f"elements.{node.id}.source").image
    target = QRectF(x, y, node.width_mm, node.height_mm)
    painter.save()
    try:
        painter.setClipRect(target, Qt.ClipOperation.IntersectClip)
        geometry = image_fit_geometry(image.width(), image.height(), x, y, node.width_mm, node.height_mm, node.fit)
        tx, ty, tw, th, sx, sy, sw, sh = geometry
        painter.drawImage(QRectF(tx, ty, tw, th), image, QRectF(sx, sy, sw, sh))
    finally:
        painter.restore()


def paint_component(painter: QPainter, component: ComponentDefinition, loader: ProjectResourceLoader,
                    owner: Path, variables: dict[str, str], *, bindings_resolved: bool = False) -> None:
    _ComponentPainter(painter, component, loader, owner, variables, bindings_resolved).paint()
