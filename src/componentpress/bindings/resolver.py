"""Resolve the deliberately small public binding language for one data row."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import re
import unicodedata
from typing import Callable

from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.render_plan import ResolvedComponent
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot, FormulaState, cell_is_empty, value_to_text
from .html_policy import validate_html
from componentpress.project_io.paths import validate_relative_path
from componentpress.rendering.resources import ProjectResourceLoader

from .grammar import Reference, parse_references
from .icons import expand_html_icons, expand_icons
from .conditions import condition_matches


_VARIABLE = re.compile(r'''\{\{\s*vars\s*\[\s*(["'])([^"'\\]+)\1\s*\]\s*\}\}''')
_ALLOWED_AST = (nodes.Template, nodes.Output, nodes.Getitem, nodes.Name, nodes.Const)
_HTML_KEY = re.compile(r"\{[A-Za-z0-9_-]+\}")


def _template_literal(value: str) -> str:
    return value.replace(r"\{", "{").replace(r"\}", "}")


@dataclass(frozen=True)
class BindingLocation:
    owner: Path
    field: str
    node_id: str
    source: str
    sheet: str


@dataclass(frozen=True)
class _ResolutionContext:
    component: ComponentDefinition
    data: DataSheetSnapshot | None
    row: DataRow | None
    owner: Path
    source: str
    sheet: str

    def location(self, node: Node, field: str) -> BindingLocation:
        return BindingLocation(self.owner, field, node.id, self.source, self.sheet)


class BindingResolver:
    def __init__(self, root: Path, project: ProjectDefinition, loader: ProjectResourceLoader | None = None, *, validate_resources: bool = True):
        self.root = root
        self.project = project
        self.loader = loader if loader is not None else ProjectResourceLoader(root)
        self.validate_resources = validate_resources
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
        *, html_text: bool = False, transform_literal: Callable[[str], str] | None = None,
    ) -> str:
        references = parse_references(template)
        pieces: list[str] = []
        cursor = 0
        for reference in references:
            start, end = self._span(template, reference)
            pieces.extend((self._literal(template[cursor:start], transform_literal), self._resolve_reference(
                template, reference, row, location, transform_column, transform_variable, html_text,
            )))
            cursor = end
        pieces.append(self._literal(template[cursor:], transform_literal))
        return "".join(pieces)

    @staticmethod
    def _literal(value: str, transform: Callable[[str], str] | None) -> str:
        return transform(value) if transform else _template_literal(value)

    def _resolve_reference(self, template, reference, row, location, column, variable, html_text):
        if reference.kind == "variable":
            return self._variable_reference(reference, location, variable)
        start, end = self._span(template, reference)
        token = template[start:end]
        if html_text and _HTML_KEY.fullmatch(token):
            return self._expand_html_key(token, location)
        return self._column_reference(reference, row, location, column)

    def _variable_reference(self, reference, location, transform):
        if reference.name not in self.project.variables:
            raise ProjectError(Diagnostic("VARIABLE_UNKNOWN", f"нет переменной {reference.name!r}", location.owner, location.field, node_id=location.node_id))
        value = self._expression("vars", reference.name, {"vars": self.project.variables})
        return transform(value)

    def _column_reference(self, reference, row, location, transform):
        if row is None:
            raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", f"столбец {reference.name!r} без строки данных", location.owner, location.field, node_id=location.node_id))
        cell = row.values.get(reference.name)
        if cell is None:
            raise ProjectError(Diagnostic("COLUMN_UNKNOWN", f"нет столбца {reference.name!r}", location.owner, location.field, source=location.source, sheet=location.sheet, node_id=location.node_id))
        raw = self._cell_text(cell, location, reference.name)
        value = self._expression("row", reference.name, {"row": {reference.name: raw}})
        return transform(value, cell)

    def _expand_html_key(self, token: str, location: BindingLocation, *, cell: str = "") -> str:
        return expand_html_icons(
            token, self.project.icons, loader=self.loader if self.validate_resources else None,
            owner=location.owner, field=location.field, source=location.source,
            sheet=location.sheet, cell=cell, node_id=location.node_id,
        )

    def _expand_html_text(self, value: str, location: BindingLocation) -> str:
        return expand_html_icons(
            value, self.project.icons, loader=self.loader if self.validate_resources else None,
            owner=location.owner, field=location.field, source=location.source,
            sheet=location.sheet, cell="", node_id=location.node_id,
        )

    def _resolve_manual_html(self, template: str, row: DataRow | None, location: BindingLocation) -> str:
        return self._replace(
            template, row, location,
            lambda value, cell: self._resolve_html_cell(value, cell, location),
            lambda value: escape(value, quote=False),
            html_text=True, transform_literal=lambda value: self._expand_html_text(value, location),
        )

    def _resolve_html_cell(self, value: str, cell: CellValue, location: BindingLocation) -> str:
        return expand_icons(
            value, self.project.icons, loader=self.loader if self.validate_resources else None,
            owner=location.owner, field=location.field, source=location.source,
            sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
        )

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
        if self.validate_resources:
            try:
                self.loader.image(value, owner=location.owner, field=location.field)
            except ProjectError as exc:
                raise self._resource_error(exc, location, source_cell) from exc
        return value

    def resolve_html(self, template: str, row: DataRow | None, location: BindingLocation, *, content_mode: str = "manual") -> str:
        parser = _BindingHtmlParser(self, row, location, content_mode)
        parser.feed(template)
        parser.close()
        html = "".join(parser.output)
        validate_html(html, location.owner, location.field)
        # One common resource boundary for static, variable and column paths.
        if self.validate_resources:
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
            message_key=exc.diagnostic.message_key, params=exc.diagnostic.params,
            incident_id=exc.diagnostic.incident_id,
        ))

    def resolve_component(self, component: ComponentDefinition, data: DataSheetSnapshot | None, row: DataRow | None, *, owner: Path | None = None) -> ResolvedComponent:
        self._check_resolver_input(component, data, row)
        context = self._context(component, data, row, owner)
        self._check_condition_columns(component.elements, context)
        model = component.model_copy(update={"elements": self._resolve_nodes(component.elements, context)})
        return ResolvedComponent(model, component.id, row, data.version if data else None)

    @staticmethod
    def _check_resolver_input(component, data, row) -> None:
        if component.data is not None and data is None:
            raise ProjectError(Diagnostic("DATA_NOT_LOADED", "данные компонента не загружены"))
        if component.data is None and row is not None:
            raise ProjectError(Diagnostic("DATA_UNEXPECTED", "статический компонент не использует строки Excel"))

    def _context(self, component, data, row, owner):
        source = component.data.source if component.data else ""
        sheet = component.data.sheet if component.data else ""
        owner = owner or self.root / f"components/{component.id}.yaml"
        return _ResolutionContext(component, data, row, owner, source, sheet)

    def _check_condition_columns(self, nodes_: tuple[Node, ...], context: _ResolutionContext) -> None:
        for node in nodes_:
            if isinstance(node, ConditionalGroupNode):
                self._require_resolver_column(node.condition_column, node, context, "condition_column")
            if isinstance(node, GroupNode):
                self._check_condition_columns(node.children, context)

    def _require_resolver_column(self, column, node, context, field):
        location = context.location(node, f"elements.{node.id}.{field}")
        if context.data is None:
            raise ProjectError(Diagnostic("COLUMN_WITHOUT_DATA", "условной группе нужен источник Excel", location.owner, location.field, node_id=node.id))
        self._require_declared_column(column, location, context.data.headers)

    def _resolve_nodes(self, nodes_: tuple[Node, ...], context: _ResolutionContext) -> tuple[Node, ...]:
        return tuple(self._resolve_node(node, context) for node in nodes_)

    def _resolve_node(self, node: Node, context: _ResolutionContext) -> Node:
        if isinstance(node, ImageNode):
            return self._resolve_image(node, context)
        if isinstance(node, HtmlNode):
            return self._resolve_html_node(node, context)
        if isinstance(node, ConditionalGroupNode):
            return self._resolve_condition(node, context)
        if isinstance(node, GroupNode):
            return node.model_copy(update={"children": self._resolve_nodes(node.children, context)})
        return node

    def _resolve_condition(self, node: ConditionalGroupNode, context: _ResolutionContext) -> Node:
        matches = None if context.row is None else self._condition_value(node, context)
        return node.model_copy(update={
            "condition_matches": matches,
            "children": self._resolve_nodes(node.children, context),
        })

    def _condition_value(self, node: ConditionalGroupNode, context: _ResolutionContext) -> bool:
        location = context.location(node, f"elements.{node.id}.condition_column")
        cell = self._column_cell(node.condition_column, context.row, location)
        value = self._cell_text(cell, location, node.condition_column)
        return condition_matches(node.condition_operator, value, cell_is_empty(cell), node.condition_value)

    def _resolve_image(self, node: ImageNode, context: _ResolutionContext) -> Node:
        location = context.location(node, f"elements.{node.id}.source")
        path, _ = self._image_path(node, context, location)
        return node.model_copy(update={"source": path, "source_mode": "manual", "source_column": None})

    def _image_path(self, node: ImageNode, context: _ResolutionContext, location: BindingLocation) -> tuple[str, CellValue | None]:
        if node.source_mode == "manual":
            return self._manual_image_path(node, context, location)
        cell = self._column_cell(node.source_column or "", context.row, location)
        if node.optional and cell_is_empty(cell):
            self._cell_text(cell, location, node.source_column or "")
            return "", cell
        return self._column_image_path(self._cell_text(cell, location, node.source_column or ""), location, cell), cell

    def _manual_image_path(self, node, context, location):
        if node.optional and not node.source:
            return "", None
        return self.resolve_path(node.source, context.row, location), None

    def _column_image_path(self, filename: str, location: BindingLocation, cell: CellValue) -> str:
        if PurePosixPath(filename).name != filename or "/" in filename or "\\" in filename:
            raise ProjectError(Diagnostic("IMAGE_FILENAME_INVALID", "в столбце должно быть имя файла изображения", location.owner, location.field, source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id, message_key="error.image_excel_filename"))
        path = f"assets/images/{filename}"
        try:
            validate_relative_path(path)
            if self.validate_resources:
                self.loader.image(path, owner=location.owner, field=location.field)
        except ProjectError as exc:
            raise self._resource_error(exc, location, cell) from exc
        return path

    def _resolve_html_node(self, node: HtmlNode, context: _ResolutionContext) -> Node:
        location = context.location(node, f"elements.{node.id}.html")
        html = self._column_html(node, context, location) if node.content_mode == "column" else self.resolve_html(node.html, context.row, location)
        return node.model_copy(update={"html": html, "content_mode": "manual", "content_column": None})

    def _column_html(self, node: HtmlNode, context: _ResolutionContext, location: BindingLocation) -> str:
        cell = self._column_cell(node.content_column or "", context.row, location)
        html = self._cell_text(cell, location, node.content_column or "")
        self._validate_source_html(html, location, cell, allow_images=False)
        if "{{" in html:
            raise ProjectError(Diagnostic("COLUMN_TEMPLATE_FORBIDDEN", "содержимое Excel не может содержать подстановки", location.owner, location.field, source=location.source, sheet=location.sheet, cell=cell.coordinate, node_id=node.id))
        html = self._expand_column_html(html, location, cell)
        self._validate_source_html(html, location, cell, allow_images=True)
        return html

    def _expand_column_html(self, html: str, location: BindingLocation, cell: CellValue) -> str:
        try:
            return expand_html_icons(
                html, self.project.icons, loader=self.loader if self.validate_resources else None,
                owner=location.owner, field=location.field, source=location.source,
                sheet=location.sheet, cell=cell.coordinate, node_id=location.node_id,
            )
        except ProjectError as exc:
            raise self._resource_error(exc, location, cell) from exc

    def _validate_source_html(self, html: str, location: BindingLocation, cell: CellValue, *, allow_images: bool) -> None:
        try:
            validate_html(html, location.owner, location.field, allow_images=allow_images)
        except ProjectError as exc:
            raise self._resource_error(exc, location, cell) from exc

    def validate_columns(self, component: ComponentDefinition, data: DataSheetSnapshot, *, owner: Path | None = None) -> None:
        """Check declared column bindings even when the sheet has no data rows."""
        context = self._context(component, data, None, owner)
        self._validate_node_columns(component.elements, context)

    def _validate_node_columns(self, nodes_: tuple[Node, ...], context: _ResolutionContext) -> None:
        for node in nodes_:
            self._validate_node_column(node, context)
            if isinstance(node, GroupNode):
                self._validate_node_columns(node.children, context)

    def _validate_node_column(self, node: Node, context: _ResolutionContext) -> None:
        if isinstance(node, ImageNode) and node.source_mode == "column":
            self._require_declared_column(node.source_column or "", context.location(node, f"elements.{node.id}.source"), context.data.headers)
        if isinstance(node, HtmlNode) and node.content_mode == "column":
            self._require_declared_column(node.content_column or "", context.location(node, f"elements.{node.id}.html"), context.data.headers)
        if isinstance(node, ConditionalGroupNode):
            self._require_declared_column(node.condition_column, context.location(node, f"elements.{node.id}.condition_column"), context.data.headers)

    @staticmethod
    def _require_declared_column(column: str, location: BindingLocation, headers: tuple[str, ...]) -> None:
        normalized = unicodedata.normalize("NFKC", column.strip()).casefold()
        names = {unicodedata.normalize("NFKC", name.strip()).casefold() for name in headers}
        if normalized not in names:
            raise ProjectError(Diagnostic("COLUMN_UNKNOWN", f"нет столбца {column!r}", location.owner, location.field, source=location.source, sheet=location.sheet, node_id=location.node_id))

    def validate_static_resources(self, component: ComponentDefinition, *, owner: Path | None = None) -> None:
        """Validate manual resources even if a bound sheet currently has no rows."""
        context = self._context(component, None, None, owner)
        self._validate_static_nodes(component.elements, context)

    def _validate_static_nodes(self, nodes_: tuple[Node, ...], context: _ResolutionContext) -> None:
        for node in nodes_:
            if isinstance(node, ImageNode) and node.source_mode == "manual" and not (node.optional and not node.source):
                self.resolve_path(node.source, None, context.location(node, f"elements.{node.id}.source"))
            if isinstance(node, HtmlNode) and node.content_mode == "manual":
                self.resolve_html(node.html, None, context.location(node, f"elements.{node.id}.html"))
            if isinstance(node, GroupNode):
                self._validate_static_nodes(node.children, context)

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
    def __init__(self, resolver: BindingResolver, row: DataRow | None, location: BindingLocation, content_mode: str):
        super().__init__(convert_charrefs=False)
        self.resolver = resolver
        self.row = row
        self.location = location
        self.content_mode = content_mode
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
        if self.content_mode != "manual":
            self.output.append(self.resolver._expand_html_text(data, self.location))
            return
        self.output.append(self.resolver._resolve_manual_html(data, self.row, self.location))

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")
