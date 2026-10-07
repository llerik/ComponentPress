"""Application service for immutable per-row component previews."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.application.ports import DataSourceReader
from componentpress.application.identifiers import instance_id
from componentpress.bindings.resolver import BindingResolver
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.copy_mode import copies_for_mode, validate_copy_mode
from componentpress.domain.render_plan import ResolvedComponent
from componentpress.domain.values import DataRow, DataSheetSnapshot
from componentpress.project_io.paths import check_exact_case, resolve_project_path
from componentpress.rendering.exporter import RenderResult, render_component


class PreviewService:
    """Keeps data versions separate from editable YAML and rejects stale reads."""

    def __init__(self, reader: DataSourceReader):
        self.reader = reader
        self._data: dict[tuple[object, ...], DataSheetSnapshot] = {}
        self._versions: dict[tuple[object, ...], int] = {}
        self._requested: dict[tuple[Path, str], int] = {}

    def begin_refresh(self, root: Path, component_id: str) -> int:
        key = (root.resolve(), component_id)
        request = self._requested.get(key, 0) + 1
        self._requested[key] = request
        return request

    def _model(self, snapshot: ProjectSnapshot, component_id: str, component: ComponentDefinition | None) -> tuple[DocumentSnapshot, ComponentDefinition]:
        document = snapshot.documents.get(component_id)
        if document is None:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
        return document, component or document.model

    def _cache_key(self, snapshot: ProjectSnapshot, component_id: str, component: ComponentDefinition) -> tuple[object, ...] | None:
        binding = component.data
        if binding is None:
            return None
        source = snapshot.model.data_source
        if source is None:
            document = snapshot.documents[component_id]
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", f"источник {binding.source!r} не найден", document.path, "data.source"))
        return (
            snapshot.root.resolve(), component_id, source.path,
            source.formula_mode, binding.sheet,
        )

    def refresh(
        self, snapshot: ProjectSnapshot, component_id: str, *, request_id: int | None = None,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        document, model = self._model(snapshot, component_id, component)
        binding = model.data
        if binding is None:
            return None
        source = snapshot.model.data_source
        if source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", f"источник {binding.source!r} не найден", document.path, "data.source"))
        cache_key = self._cache_key(snapshot, component_id, model)
        assert cache_key is not None
        request_key = (snapshot.root.resolve(), component_id)
        if request_id is None:
            request_id = self.begin_refresh(snapshot.root, component_id)
        elif request_id < self._requested.get(request_key, 0):
            raise ProjectError(Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", document.path))
        self._requested[request_key] = request_id
        version = self._versions.get(cache_key, 0) + 1
        check_exact_case(snapshot.root, source.path)
        path = resolve_project_path(snapshot.root, source.path)
        loaded = self.reader.read(
            path, binding.sheet, source="main",
            copies_columns=(snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test),
            version=version, request_id=request_id,
        )
        if request_id != self._requested.get(request_key):
            raise ProjectError(Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", document.path))
        loaded = replace(loaded, rows=tuple(
            replace(row, instance_id=instance_id(snapshot.model.id, component_id, binding.sheet, row.row_number))
            for row in loaded.rows
        ))
        resolver = BindingResolver(snapshot.root, snapshot.model)
        issues = []
        for row in loaded.rows:
            try:
                resolver.resolve_component(model, loaded, row, owner=document.path)
            except ProjectError as exc:
                issues.append(exc.diagnostic)
        if issues:
            # Retain the last successful cache and surface every row-level issue.
            raise ValidationIssues(issues)
        self._versions[cache_key] = version
        self._data[cache_key] = loaded
        return loaded

    def validate_all(self, snapshot: ProjectSnapshot) -> tuple[object, ...]:
        diagnostics = []
        for component in snapshot.model.components:
            model = snapshot.documents[component.id].model
            if model.data is None:
                continue
            try:
                self.refresh(snapshot, component.id, component=model)
            except ValidationIssues as exc:
                diagnostics.extend(exc.diagnostics)
            except ProjectError as exc:
                diagnostics.append(exc.diagnostic)
        return tuple(diagnostics)

    def data(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        _document, model = self._model(snapshot, component_id, component)
        if model.data is None:
            return None
        key = self._cache_key(snapshot, component_id, model)
        assert key is not None
        return self._data.get(key) or self.refresh(snapshot, component_id, component=model)

    def cached_data(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        """Return the latest successful snapshot without causing an XLSX read."""
        _document, model = self._model(snapshot, component_id, component)
        key = self._cache_key(snapshot, component_id, model)
        return self._data.get(key) if key is not None else None

    def select_row(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        row_number: int | None = None, instance_id: str | None = None,
        mode: str = "prod",
        component: ComponentDefinition | None = None,
    ) -> ResolvedComponent:
        document, model = self._model(snapshot, component_id, component)
        mode = validate_copy_mode(mode)
        data = self.data(snapshot, component_id, component=model)
        row: DataRow | None = None
        if data is not None:
            eligible = tuple(item for item in data.rows if copies_for_mode(item, mode) > 0)
            if not eligible:
                raise ProjectError(Diagnostic("DATA_EMPTY", "на листе нет строк данных", data.path, source=data.source, sheet=data.sheet))
            if instance_id is not None:
                row = next((item for item in eligible if item.instance_id == instance_id), None)
            elif row_number is not None:
                row = next((item for item in eligible if item.row_number == row_number), None)
            else:
                row = eligible[0]
            if row is None:
                raise ProjectError(Diagnostic("DATA_ROW_UNKNOWN", "строка предпросмотра не найдена", data.path, source=data.source, sheet=data.sheet))
        resolver = BindingResolver(snapshot.root, snapshot.model)
        return resolver.resolve_component(model, data, row, owner=document.path)

    def export_png(
        self, snapshot: ProjectSnapshot, component_id: str, output: Path, *,
        row_number: int | None = None, instance_id: str | None = None, dpi: int | None = None,
        mode: str = "prod",
        component: ComponentDefinition | None = None,
    ) -> RenderResult:
        resolved = self.select_row(
            snapshot, component_id, row_number=row_number, instance_id=instance_id, mode=mode,
            component=component,
        )
        return render_component(snapshot, component_id, output, component=resolved.component, dpi=dpi)


class ValidationIssues(ProjectError):
    def __init__(self, diagnostics):
        self.diagnostics = tuple(diagnostics)
        super().__init__(self.diagnostics[0])
