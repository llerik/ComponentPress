"""Print-size quality measurements for component and inline HTML images."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.project_io.paths import validate_relative_path
from componentpress.project_io.yaml_codec import html_image_sources
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


def measure_component_images(
    component: ComponentDefinition,
    project: ProjectDefinition,
    loader: ProjectResourceLoader,
    owner: Path,
    *,
    bindings_resolved: bool = True,
) -> tuple[ImageQuality, ...]:
    results: list[ImageQuality] = []

    def visit(nodes: tuple[Node, ...]) -> None:
        for node in nodes:
            if isinstance(node, ImageNode):
                loaded = loader.image(node.source, owner=owner, field=f"elements.{node.id}.source")
                tx, ty, tw, th, sx, sy, sw, sh = image_fit_geometry(
                    loaded.image.width(), loaded.image.height(), 0, 0,
                    node.width_mm, node.height_mm, node.fit,
                )
                del tx, ty, sx, sy
                results.append(ImageQuality(
                    node.id, node.source, loaded.image.width(), loaded.image.height(), tw, th,
                    sw * MM_PER_INCH / tw, sh * MM_PER_INCH / th,
                ))
            elif isinstance(node, HtmlNode):
                html = node.html
                if not bindings_resolved:
                    from componentpress.bindings.static import resolve_static

                    html = resolve_static(html, project.variables, html=True, owner=owner, field=f"elements.{node.id}.html")
                sources = html_image_sources(html)
                parser = _InlineImages()
                parser.feed(html)
                parser.close()
                for index, (source, width, height) in enumerate(parser.items):
                    relative = _inline_path(source, owner, node.id)
                    loaded = loader.image(relative, owner=owner, field=f"elements.{node.id}.html.img.src")
                    pixel_width, pixel_height = loaded.image.width(), loaded.image.height()
                    width_mm = _inline_mm(width, node.width_mm, pixel_width)
                    height_mm = _inline_mm(height, node.height_mm, pixel_height)
                    if width is not None and height is None:
                        height_mm = width_mm * pixel_height / pixel_width
                    elif height is not None and width is None:
                        width_mm = height_mm * pixel_width / pixel_height
                    if index < len(sources) and source != sources[index]:
                        relative = _inline_path(sources[index], owner, node.id)
                        loaded = loader.image(relative, owner=owner, field=f"elements.{node.id}.html.img.src")
                        pixel_width, pixel_height = loaded.image.width(), loaded.image.height()
                    results.append(ImageQuality(
                        node.id, relative, pixel_width, pixel_height, width_mm, height_mm,
                        pixel_width * MM_PER_INCH / width_mm,
                        pixel_height * MM_PER_INCH / height_mm,
                    ))
            elif isinstance(node, GroupNode):
                visit(node.children)

    visit(component.elements)
    return tuple(results)
