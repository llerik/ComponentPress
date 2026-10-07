"""Expansion of controlled project icon labels in spreadsheet text."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.project import IconDefinition
from componentpress.rendering.geometry import mm_to_html_pixels
from componentpress.rendering.resources import ProjectResourceLoader


_ICON = re.compile(r"(?<!\w)(\\?)(ic_[A-Za-z0-9_]+)(?!\w)", re.UNICODE)


class _HtmlIconParser(HTMLParser):
    """Expand icon labels in HTML text while preserving markup and entities."""

    def __init__(
        self,
        icons: dict[str, IconDefinition],
        *,
        loader: ProjectResourceLoader | None,
        owner: Path,
        field: str,
        source: str,
        sheet: str,
        cell: str,
        node_id: str,
    ):
        super().__init__(convert_charrefs=False)
        self.icons = icons
        self.loader = loader
        self.owner = owner
        self.field = field
        self.source = source
        self.sheet = sheet
        self.cell = cell
        self.node_id = node_id
        self.output: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rendered = [
            name if value is None else f'{name}="{escape(value, quote=True)}"'
            for name, value in attrs
        ]
        suffix = (" " + " ".join(rendered)) if rendered else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.output[-1] = self.output[-1][:-1] + " />"

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(expand_icons(
            data,
            self.icons,
            loader=self.loader,
            owner=self.owner,
            field=self.field,
            source=self.source,
            sheet=self.sheet,
            cell=self.cell,
            node_id=self.node_id,
        ))

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")


def expand_icons(
    text: str,
    icons: dict[str, IconDefinition],
    *,
    loader: ProjectResourceLoader | None,
    owner: Path,
    field: str,
    source: str,
    sheet: str,
    cell: str,
    node_id: str,
) -> str:
    pieces: list[str] = []
    position = 0
    for match in _ICON.finditer(text):
        pieces.append(escape(text[position:match.start()], quote=False))
        escaped, name = match.groups()
        if escaped:
            pieces.append(escape(name, quote=False))
        else:
            icon = icons.get(name)
            if icon is None:
                raise ProjectError(Diagnostic(
                    "ICON_UNKNOWN", f"нет именованной иконки {name!r}", owner, field,
                    source=source, sheet=sheet, cell=cell, node_id=node_id,
                ))
            if loader is not None:
                try:
                    loader.image(icon.path, owner=owner, field=field)
                except ProjectError as exc:
                    raise ProjectError(Diagnostic(
                        exc.diagnostic.code, exc.diagnostic.message, owner, field,
                        source=source, sheet=sheet, cell=cell, node_id=node_id,
                    )) from exc
            width = mm_to_html_pixels(icon.width_mm)
            height = mm_to_html_pixels(icon.height_mm)
            pieces.append(
                f'<img src="{escape(icon.path, quote=True)}" '
                f'width="{width:.4f}" height="{height:.4f}" alt="{escape(name, quote=True)}" />'
            )
        position = match.end()
    pieces.append(escape(text[position:], quote=False))
    return "".join(pieces)


def expand_html_icons(
    html: str,
    icons: dict[str, IconDefinition],
    *,
    loader: ProjectResourceLoader | None,
    owner: Path,
    field: str,
    source: str,
    sheet: str,
    cell: str,
    node_id: str,
) -> str:
    """Expand icon labels in HTML text nodes without escaping the HTML tags."""
    parser = _HtmlIconParser(
        icons,
        loader=loader,
        owner=owner,
        field=field,
        source=source,
        sheet=sheet,
        cell=cell,
        node_id=node_id,
    )
    parser.feed(html)
    parser.close()
    return "".join(parser.output)
