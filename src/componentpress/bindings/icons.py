"""Expansion of controlled project icon labels in spreadsheet text."""

from __future__ import annotations

from html import escape
from pathlib import Path
import re

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.project import IconDefinition
from componentpress.rendering.geometry import mm_to_html_pixels
from componentpress.rendering.resources import ProjectResourceLoader


_ICON = re.compile(r"(?<!\w)(\\?)(ic_[A-Za-z0-9_]+)(?!\w)", re.UNICODE)


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
