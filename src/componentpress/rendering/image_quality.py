"""Print-size quality measurements for component and inline HTML images."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.html_resources import html_image_sources
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.geometry import Affine
from componentpress.domain.project import ProjectDefinition
from componentpress.project_io.paths import validate_relative_path
from .geometry import HTML_DPI, MM_PER_INCH, image_fit_geometry
from .resources import ProjectResourceLoader


@dataclass(frozen=True)
class ImageQuality:
    node_id: str
    source: str
    pixel_width: int
    pixel_height: int
    width_mm: float
    height_mm: float
    dpi_x: float
    dpi_y: float

    @property
    def warning(self) -> bool:
        return min(self.dpi_x, self.dpi_y) < 300

    def diagnostic(self, owner: Path, *, source: str | None = None, sheet: str | None = None) -> Diagnostic | None:
        if not self.warning:
            return None
        return Diagnostic(
            "IMAGE_DPI_LOW",
            f"изображение {self.source}: {self.pixel_width}×{self.pixel_height} px, "
            f"эффективное разрешение {self.dpi_x:.1f}×{self.dpi_y:.1f} dpi (порог 300)",
            owner,
            f"elements.{self.node_id}.image",
            severity="warning",
            source=source,
            sheet=sheet,
            node_id=self.node_id,
            message_key="ui.image_quality_diagnostic",
            params={"source": self.source, "width": self.pixel_width, "height": self.pixel_height,
                    "dpi_x": self.dpi_x, "dpi_y": self.dpi_y},
        )


class _InlineImages(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str | None, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._image(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._image(tag, attrs)

    def _image(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = dict(attrs)
        source = values.get("src")
        if source:
            self.items.append((source, values.get("width"), values.get("height")))


def _inline_mm(value: str | None, available_mm: float, intrinsic_px: int) -> float:
    if not value:
        return intrinsic_px * MM_PER_INCH / HTML_DPI
    text = value.strip()
    if text.endswith("%"):
        try:
            return available_mm * float(text[:-1]) / 100
        except ValueError:
            return intrinsic_px * MM_PER_INCH / HTML_DPI
    try:
        return float(text) * MM_PER_INCH / HTML_DPI
    except ValueError:
        return intrinsic_px * MM_PER_INCH / HTML_DPI


def _inline_path(source: str, owner: Path, node_id: str) -> str:
    parts = urlsplit(source)
    field = f"elements.{node_id}.html.img.src"
    if parts.scheme or parts.netloc or parts.query or parts.fragment or parts.path.startswith("/"):
        raise ProjectError(Diagnostic("RESOURCE_URL_UNSUPPORTED", "разрешены только локальные пути проекта", owner, field))
    return validate_relative_path(unquote(parts.path))


def _axis_scale(world: Affine) -> tuple[float, float]:
    return (world.a * world.a + world.c * world.c) ** 0.5, (world.b * world.b + world.d * world.d) ** 0.5


def _image_quality(node: ImageNode, world: Affine, loader: ProjectResourceLoader,
                   owner: Path) -> ImageQuality | None:
    if node.optional and not node.source:
        return None
    loaded = loader.image(node.source, owner=owner, field=f"elements.{node.id}.source")
    image = loaded.image
    geometry = image_fit_geometry(image.width(), image.height(), 0, 0, node.width_mm, node.height_mm, node.fit)
    _x, _y, width, height, _sx, _sy, source_width, source_height = geometry
    scale_x, scale_y = _axis_scale(world)
    return ImageQuality(node.id, node.source, image.width(), image.height(), width, height,
                        source_width * MM_PER_INCH / (width * scale_x),
                        source_height * MM_PER_INCH / (height * scale_y))


def _inline_quality(node: HtmlNode, html: str, world: Affine, loader: ProjectResourceLoader,
                    owner: Path) -> list[ImageQuality]:
    sources = html_image_sources(html)
    parser = _InlineImages()
    parser.feed(html)
    parser.close()
    results = []
    for index, (source, width, height) in enumerate(parser.items):
        results.append(_inline_item(node, source, sources, index, width, height, world, loader, owner))
    return results


def _inline_item(node: HtmlNode, source: str, sources: tuple[str, ...], index: int,
                 width: str | None, height: str | None, world: Affine,
                 loader: ProjectResourceLoader, owner: Path) -> ImageQuality:
    relative = _inline_path(source, owner, node.id)
    field = f"elements.{node.id}.html.img.src"
    image = loader.image(relative, owner=owner, field=field).image
    width_mm, height_mm = _inline_size(node, width, height, image.width(), image.height())
    if index < len(sources) and source != sources[index]:
        relative = _inline_path(sources[index], owner, node.id)
        image = loader.image(relative, owner=owner, field=field).image
    return _inline_image_quality(node, relative, width_mm, height_mm, image.width(), image.height(), world)


def _inline_size(node: HtmlNode, width: str | None, height: str | None,
                 pixel_width: int, pixel_height: int) -> tuple[float, float]:
    width_mm, height_mm = _inline_mm(width, node.width_mm, pixel_width), _inline_mm(height, node.height_mm, pixel_height)
    if width is not None and height is None:
        height_mm = width_mm * pixel_height / pixel_width
    elif height is not None and width is None:
        width_mm = height_mm * pixel_width / pixel_height
    return width_mm, height_mm


def _inline_image_quality(node: HtmlNode, source: str, width_mm: float, height_mm: float,
                          pixel_width: int, pixel_height: int, world: Affine) -> ImageQuality:
    scale_x, scale_y = _axis_scale(world)
    return ImageQuality(node.id, source, pixel_width, pixel_height, width_mm, height_mm,
                        pixel_width * MM_PER_INCH / (width_mm * scale_x),
                        pixel_height * MM_PER_INCH / (height_mm * scale_y))


def _resolved_html(node: HtmlNode, project: ProjectDefinition, owner: Path, resolved: bool) -> str:
    if resolved:
        return node.html
    from componentpress.bindings.static import resolve_static

    return resolve_static(node.html, project.variables, html=True, owner=owner, field=f"elements.{node.id}.html")


def _node_quality(node: Node, world: Affine, project: ProjectDefinition,
                  loader: ProjectResourceLoader, owner: Path, resolved: bool) -> list[ImageQuality]:
    if isinstance(node, ImageNode):
        result = _image_quality(node, world, loader, owner)
        return [] if result is None else [result]
    if isinstance(node, HtmlNode):
        return _inline_quality(node, _resolved_html(node, project, owner, resolved), world, loader, owner)
    return []


def _visit(nodes: tuple[Node, ...], parent: Affine, project: ProjectDefinition,
           loader: ProjectResourceLoader, owner: Path, resolved: bool, results: list[ImageQuality]) -> None:
    for node in nodes:
        world = parent.then(Affine.node(node))
        results.extend(_node_quality(node, world, project, loader, owner, resolved))
        if isinstance(node, GroupNode):
            _visit(node.children, world, project, loader, owner, resolved, results)


def measure_component_images(component: ComponentDefinition, project: ProjectDefinition,
                             loader: ProjectResourceLoader, owner: Path, *,
                             bindings_resolved: bool = True) -> tuple[ImageQuality, ...]:
    results: list[ImageQuality] = []
    _visit(component.elements, Affine(), project, loader, owner, bindings_resolved, results)
    return tuple(results)
