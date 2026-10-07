"""Resolve the deliberately small public binding language for one data row."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
from typing import Callable

from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.render_plan import ResolvedComponent
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot, FormulaState, value_to_text
from componentpress.bindings.html_policy import validate_html
from componentpress.project_io.paths import validate_relative_path
from componentpress.rendering.resources import ProjectResourceLoader

from .grammar import Reference, parse_references
from .icons import expand_html_icons, expand_icons


_VARIABLE = re.compile(r'''\{\{\s*vars\s*\[\s*(["'])([^"'\\]+)\1\s*\]\s*\}\}''')
_ALLOWED_AST = (nodes.Template, nodes.Output, nodes.Getitem, nodes.Name, nodes.Const)


@dataclass(frozen=True)
class BindingLocation:
    owner: Path
    field: str
    node_id: str
    source: str
    sheet: str


class BindingResolver:
    def __init__(self, root: Path, project: ProjectDefinition, loader: ProjectResourceLoader | None = None):
        self.root = root
        self.project = project
        self.loader = loader if loader is not None else ProjectResourceLoader(root)
        self.environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    def _expression(self, kind: str, name: str, context: dict[str, object]) -> str:
        expression = f'{kind}[{name!r}]'
        parsed = self.environment.parse("{{ " + expression + " }}")
        for node in parsed.find_all(nodes.Node):
            if not isinstance(node, _ALLOWED_AST):
                raise RuntimeError(f"unsafe internal binding AST: {type(node).__name__}")
        return self.environment.from_string("{{ " + expression + " }}").render(context)

    @staticmethod
    def _span(text: str, reference: Reference) -> tuple[int, int]:
        if reference.kind == "variable":
            match = _VARIABLE.match(text, reference.offset)
            if match is None:
                raise ValueError("invalid variable span")
            return reference.offset, match.end()
        end = text.find("}", reference.offset + 1)
        return reference.offset, end + 1

    def _cell_text(self, cell: CellValue, location: BindingLocation, column: str) -> str:
        if cell.formula_state is FormulaState.MISSING:
            raise ProjectError(Diagnostic(
                "FORMULA_RESULT_UNAVAILABLE", "нет сохранённого результата формулы", location.owner, location.field,
                source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
            ))
        if cell.formula_state is FormulaState.ERROR:
            raise ProjectError(Diagnostic(
                "FORMULA_ERROR", f"ошибка Excel: {cell.value}", location.owner, location.field,
                source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
            ))
        try:
            return value_to_text(cell.value)
        except ValueError as exc:
            raise ProjectError(Diagnostic(
                "CELL_VALUE_INVALID", str(exc), location.owner, location.field,
                source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
            )) from exc

    def _replace(
        self,
        template: str,
        row: DataRow | None,
        location: BindingLocation,
        transform_column: Callable[[str, CellValue], str],
        transform_variable: Callable[[str], str],
    ) -> str:
        references = parse_references(template)
        pieces: list[str] = []
        cursor = 0
        for reference in references:
            start, end = self._span(template, reference)
            # Escape markers are syntax only in the original template. Values
            # inserted from Excel/project variables are opaque data and must
            # never be unescaped or parsed again.
            pieces.append(template[cursor:start].replace(r"\{", "{").replace(r"\}", "}"))
            if reference.kind == "column":
                if row is None:
                    raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", f"столбец {reference.name!r} без строки данных", location.owner, location.field, node_id=location.node_id))
                cell = row.values.get(reference.name)
                if cell is None:
                    raise ProjectError(Diagnostic(
                        "COLUMN_UNKNOWN", f"нет столбца {reference.name!r}", location.owner, location.field,
                        source=location.source, sheet=location.sheet, node_id=location.node_id,
                    ))
                raw = self._cell_text(cell, location, reference.name)
                # The sandbox evaluates only the generated constant-key access.
                evaluated = self._expression("row", reference.name, {"row": {reference.name: raw}})
                replacement = transform_column(evaluated, cell)
            else:
                if reference.name not in self.project.variables:
                    raise ProjectError(Diagnostic("VARIABLE_UNKNOWN", f"нет переменной {reference.name!r}", location.owner, location.field, node_id=location.node_id))
                evaluated = self._expression("vars", reference.name, {"vars": self.project.variables})
                replacement = transform_variable(evaluated)
            pieces.append(replacement)
            cursor = end
        pieces.append(template[cursor:].replace(r"\{", "{").replace(r"\}", "}"))
        return "".join(pieces)

    def resolve_text(self, template: str, row: DataRow | None, location: BindingLocation, *, icons: bool) -> str:
        def column(value: str, cell: CellValue) -> str:
            if not icons:
                return escape(value, quote=False)
            return expand_icons(
                value, self.project.icons, loader=self.loader, owner=location.owner, field=location.field,
                source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
            )
        return self._replace(template, row, location, column, lambda value: escape(value, quote=False))

    def resolve_path(self, template: str, row: DataRow | None, location: BindingLocation) -> str:
        source_cell = self._first_column_cell(template, row)
        value = self._replace(template, row, location, lambda text, _cell: text, lambda text: text)
        try:
            validate_relative_path(value)
        except ProjectError as exc:
            raise self._resource_error(exc, location, source_cell) from exc
        try:
            self.loader.image(value, owner=location.owner, field=location.field)
        except ProjectError as exc:
            raise self._resource_error(exc, location, source_cell) from exc
        return value

    def resolve_html(self, template: str, row: DataRow | None, location: BindingLocation) -> str:
        parser = _BindingHtmlParser(self, row, location)
        parser.feed(template)
        parser.close()
        html = "".join(parser.output)
        validate_html(html, location.owner, location.field)
        # One common resource boundary for static, variable and column paths.
        for source, cell in parser.resources:
            try:
                self.loader.image(source, owner=location.owner, field=f"{location.field}.img.src")
            except ProjectError as exc:
                attr_location = BindingLocation(
                    location.owner, f"{location.field}.img.src", location.node_id,
                    location.source, location.sheet,
                )
                raise self._resource_error(exc, attr_location, cell) from exc
        return html

    @staticmethod
    def _first_column_cell(template: str, row: DataRow | None) -> CellValue | None:
        if row is None:
            return None
        for reference in parse_references(template):
            if reference.kind == "column":
                return row.values.get(reference.name)
        return None

    @staticmethod
    def _resource_error(exc: ProjectError, location: BindingLocation, cell: CellValue | None) -> ProjectError:
        return ProjectError(Diagnostic(
            exc.diagnostic.code, exc.diagnostic.message, location.owner, location.field,
            source=location.source or None, sheet=location.sheet or None,
            cell=cell.coordinate if cell is not None else None, node_id=location.node_id,
        ))

    def resolve_component(self, component: ComponentDefinition, data: DataSheetSnapshot | None, row: DataRow | None, *, owner: Path | None = None) -> ResolvedComponent:
        if component.data is not None and data is None:
            raise ProjectError(Diagnostic("DATA_NOT_LOADED", "данные компонента не загружены"))
        if component.data is None and row is not None:
            raise ProjectError(Diagnostic("DATA_UNEXPECTED", "статический компонент не использует строки Excel"))
        source = component.data.source if component.data else ""
        sheet = component.data.sheet if component.data else ""
        owner = owner or self.root / f"components/{component.id}.yaml"

        def visit(nodes_: tuple[Node, ...]) -> tuple[Node, ...]:
            resolved: list[Node] = []
            for node in nodes_:
                if isinstance(node, ImageNode):
                    location = BindingLocation(owner, f"elements.{node.id}.source", node.id, source, sheet)
                    path_template = node.source
                    if node.source_mode == "column":
                        cell = self._column_cell(node.source_column or "", row, location)
                        path_template = self._cell_text(cell, location, node.source_column or "")
                        try:
                            validate_relative_path(path_template)
                            self.loader.image(path_template, owner=owner, field=location.field)
                        except ProjectError as exc:
                            raise self._resource_error(exc, location, cell) from exc
                        resolved_path = path_template
                    else:
                        resolved_path = self.resolve_path(path_template, row, location)
                    resolved.append(node.model_copy(update={
                        "source": resolved_path,
                        "source_mode": "manual", "source_column": None,
                    }))
                elif isinstance(node, HtmlNode):
                    location = BindingLocation(owner, f"elements.{node.id}.html", node.id, source, sheet)
                    html = node.html
                    if node.content_mode == "column":
                        cell = self._column_cell(node.content_column or "", row, location)
                        html = self._cell_text(cell, location, node.content_column or "")
                        try:
                            validate_html(html, owner, location.field, allow_images=False)
                        except ProjectError as exc:
                            raise ProjectError(Diagnostic(
                                exc.diagnostic.code, exc.diagnostic.message, owner, location.field,
                                source=source, sheet=sheet, cell=cell.coordinate, node_id=node.id,
                            )) from exc
                        if parse_references(html):
                            raise ProjectError(Diagnostic("COLUMN_TEMPLATE_FORBIDDEN", "содержимое Excel не может содержать подстановки", owner, location.field, source=source, sheet=sheet, cell=cell.coordinate, node_id=node.id))
                        html = expand_html_icons(
                            html, self.project.icons, loader=self.loader, owner=owner,
                            field=location.field, source=source, sheet=sheet,
                            cell=cell.coordinate, node_id=node.id,
                        )
                        try:
                            validate_html(html, owner, location.field)
                        except ProjectError as exc:
                            raise ProjectError(Diagnostic(
                                exc.diagnostic.code, exc.diagnostic.message, owner, location.field,
                                source=source, sheet=sheet, cell=cell.coordinate, node_id=node.id,
                            )) from exc
                    else:
                        html = self.resolve_html(html, row, location)
                    resolved.append(node.model_copy(update={"html": html, "content_mode": "manual", "content_column": None}))
                elif isinstance(node, GroupNode):
                    resolved.append(node.model_copy(update={"children": visit(node.children)}))
            return tuple(resolved)

        model = component.model_copy(update={"elements": visit(component.elements)})
        return ResolvedComponent(model, component.id, row, data.version if data else None)

    @staticmethod
    def _column_cell(column: str, row: DataRow | None, location: BindingLocation) -> CellValue:
        if row is None:
            raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", f"столбец {column!r} без строки Excel", location.owner, location.field, node_id=location.node_id))
        normalized = unicodedata.normalize("NFKC", column.strip()).casefold()
        cell = next((value for name, value in row.values.items() if unicodedata.normalize("NFKC", name.strip()).casefold() == normalized), None)
        if cell is None:
            raise ProjectError(Diagnostic("COLUMN_UNKNOWN", f"нет столбца {column!r}", location.owner, location.field, source=location.source, sheet=location.sheet, node_id=location.node_id))
        return cell

    def _column_value(self, column: str, row: DataRow | None, location: BindingLocation) -> str:
        cell = self._column_cell(column, row, location)
        return self._cell_text(cell, location, column)


class _BindingHtmlParser(HTMLParser):
    def __init__(self, resolver: BindingResolver, row: DataRow | None, location: BindingLocation):
        super().__init__(convert_charrefs=False)
        self.resolver = resolver
        self.row = row
        self.location = location
        self.output: list[str] = []
        self.resources: list[tuple[str, CellValue | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rendered: list[str] = []
        for name, value in attrs:
            if value is None:
                rendered.append(name)
                continue
            if tag.lower() == "img" and name.lower() == "src":
                attr_location = BindingLocation(self.location.owner, f"{self.location.field}.img.src", self.location.node_id, self.location.source, self.location.sheet)
                source_cell = self.resolver._first_column_cell(value, self.row)
                value = self.resolver._replace(value, self.row, attr_location, lambda text, _cell: text, lambda text: text)
                try:
                    validate_relative_path(value)
                except ProjectError as exc:
                    raise self.resolver._resource_error(exc, attr_location, source_cell) from exc
                self.resources.append((value, source_cell))
            rendered.append(f'{name}="{escape(value, quote=True)}"')
        suffix = (" " + " ".join(rendered)) if rendered else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.output[-1] = self.output[-1][:-1] + " />"

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(self.resolver.resolve_text(data, self.row, self.location, icons=True))

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")
