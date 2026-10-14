"""Application service for immutable per-row component previews."""

from __future__ import annotations

from dataclasses import replace
import json
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
        self._data_fingerprints: dict[tuple[object, ...], tuple[object, ...]] = {}
        self._validated_models: dict[tuple[object, ...], tuple[object, ...]] = {}
        self._failures: dict[tuple[object, ...], tuple[tuple[object, ...], tuple[object, ...], tuple[Diagnostic, ...]]] = {}
        self._fingerprints: dict[tuple[Path, str, str], tuple[object, ...]] = {}
        self._persisted_validation: dict[tuple[Path, str, str], tuple[tuple[object, ...], frozenset[str]]] = {}
        self._versions: dict[tuple[object, ...], int] = {}
        self._requested: dict[tuple[Path, str], int] = {}

    def invalidate(self, root: Path | None = None) -> None:
        """Drop validated data when the project settings or source change."""
        if root is None:
            self._data.clear()
            self._data_fingerprints.clear()
            self._validated_models.clear()
            self._failures.clear()
            self._fingerprints.clear()
            self._persisted_validation.clear()
            self._versions.clear()
            self._requested.clear()
            return
        resolved = root.resolve()
        for key in tuple(self._data):
            if key[0] == resolved:
                self._data.pop(key, None)
                self._data_fingerprints.pop(key, None)
                self._validated_models.pop(key, None)
                self._failures.pop(key, None)
                self._versions.pop(key, None)
        for key in tuple(self._fingerprints):
            if key[0] == resolved:
                self._fingerprints.pop(key, None)
                self._persisted_validation.pop(key, None)
        for key in tuple(self._requested):
            if key[0] == resolved:
                self._requested.pop(key, None)

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
            snapshot.root.resolve(), snapshot.model.id, component_id, source.path,
            source.formula_mode, binding.sheet,
            snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test,
        )

    @staticmethod
    def _source_key(snapshot: ProjectSnapshot) -> tuple[Path, str, str] | None:
        source = snapshot.model.data_source
        return (snapshot.root.resolve(), source.path, source.formula_mode) if source is not None else None

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int]:
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("DATA_SOURCE_MISSING", "книга XLSX не найдена", path)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
        return metadata.st_size, metadata.st_mtime_ns

    @staticmethod
    def _model_signature(snapshot: ProjectSnapshot, component: ComponentDefinition) -> tuple[object, ...]:
        from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode

        def nodes_signature(nodes):
            result = []
            for node in nodes:
                if isinstance(node, ImageNode):
                    result.append(("image", node.source_mode, node.source if node.source_mode == "manual" else node.source_column))
                elif isinstance(node, HtmlNode):
                    result.append(("html", node.content_mode, node.html if node.content_mode == "manual" else node.content_column))
                elif isinstance(node, ConditionalGroupNode):
                    result.append(("conditional", node.condition_column, node.condition_operator, node.condition_value, nodes_signature(node.children)))
                elif isinstance(node, GroupNode):
                    result.append(("group", nodes_signature(node.children)))
                else:
                    result.append((node.type,))
            return tuple(result)

        project = snapshot.model
        icons = tuple(sorted((name, item.path, item.width_mm, item.height_mm) for name, item in project.icons.items()))
        return (component.data.sheet if component.data else None, nodes_signature(component.elements),
                tuple(sorted(project.variables.items())), icons)

    def _store_validation_state(self, root: Path, source_path: str, fingerprint: tuple[object, ...], component_ids: list[str]) -> Diagnostic | None:
        path = root / ".componentpress" / "validation.json"
        folder = path.parent
        payload = {
            "format": 1,
            "source": {"path": source_path, "size": fingerprint[0], "mtime_ns": fingerprint[1]},
            "validated_components": sorted(component_ids),
        }
        try:
            if folder.is_symlink() or path.is_symlink():
                raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки для состояния проверки не поддерживаются", folder))
            from componentpress.project_io.files import atomic_write

            atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except ProjectError as exc:
            return exc.diagnostic
        return None

    def refresh(
        self, snapshot: ProjectSnapshot, component_id: str, *, request_id: int | None = None,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        document, model = self._model(snapshot, component_id, component)
        if request_id is not None and request_id < self._requested.get((snapshot.root.resolve(), component_id), 0):
            raise ProjectError(Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", document.path))
        if model.data is None:
            self.validate_all(snapshot, manual=True, component_ids=(component_id,))
            return None
        diagnostics = self.validate_all(
            snapshot, manual=True, component_ids=(component_id,),
            component_overrides={component_id: model},
        )
        if diagnostics:
            raise ValidationIssues(diagnostics)
        return self.cached_data(snapshot, component_id, component=model)

    def validate_all(
        self, snapshot: ProjectSnapshot, *, force_read: bool = False, manual: bool = True,
        component_ids: tuple[str, ...] | None = None,
        component_overrides: dict[str, ComponentDefinition] | None = None,
    ) -> tuple[Diagnostic, ...]:
        source = snapshot.model.data_source
        all_ids = tuple(dict.fromkeys((*(item.id for item in snapshot.model.components), *snapshot.documents)))
        requested_ids = component_ids or all_ids
        static_only = bool(component_ids) and all(
            component_id in snapshot.documents
            and (component_overrides or {}).get(component_id, snapshot.documents[component_id].model).data is None
            for component_id in requested_ids
        )
        source_key = None if static_only else self._source_key(snapshot)
        try:
            source_path = resolve_project_path(snapshot.root, source.path) if source is not None and not static_only else None
            if source is not None and not static_only:
                check_exact_case(snapshot.root, source.path)
                baseline = self._fingerprint(source_path)
            else:
                baseline = None
        except ProjectError as exc:
            return (exc.diagnostic,)

        # If the source changed, all dependent components must move to the same generation.
        if source_key is not None and self._fingerprints.get(source_key) != baseline:
            requested_ids = all_ids
        requests = {item: self.begin_refresh(snapshot.root, item) for item in requested_ids}
        staged: dict[tuple[object, ...], tuple[DataSheetSnapshot, tuple[object, ...], tuple[object, ...]]] = {}
        static_staged: dict[tuple[object, ...], tuple[object, ...]] = {}
        failures: dict[tuple[object, ...], tuple[tuple[object, ...], tuple[object, ...], tuple[Diagnostic, ...]]] = {}
        diagnostics: list[Diagnostic] = []
        checked_ids: list[str] = []

        for component_id in requested_ids:
            document = snapshot.documents.get(component_id)
            if document is None:
                continue
            model = (component_overrides or {}).get(component_id, document.model)
            signature = self._model_signature(snapshot, model)
            cache_key = self._cache_key(snapshot, component_id, model)
            if cache_key is None:
                static_key = (snapshot.root.resolve(), component_id)
                if not manual and self._validated_models.get(static_key) == signature:
                    checked_ids.append(component_id)
                    continue
                try:
                    BindingResolver(snapshot.root, snapshot.model).resolve_component(model, None, None, owner=document.path)
                except ProjectError as exc:
                    diagnostics.append(exc.diagnostic)
                    failures[static_key] = ((), signature, (exc.diagnostic,))
                else:
                    static_staged[static_key] = signature
                    checked_ids.append(component_id)
                continue

            assert source_path is not None and baseline is not None
            cached = self._data.get(cache_key)
            cached_for_current = cached is not None and self._data_fingerprints.get(cache_key) == baseline
            if (not force_read and cached_for_current and not manual
                    and self._validated_models.get(cache_key) == signature):
                checked_ids.append(component_id)
                continue
            previous_failure = self._failures.get(cache_key)
            if (not force_read and not manual and previous_failure is not None
                    and previous_failure[:2] == (baseline, signature)):
                diagnostics.extend(previous_failure[2])
                continue

            version = self._versions.get(cache_key, 0) + 1
            try:
                if cached_for_current and not force_read:
                    loaded = cached
                else:
                    loaded = self.reader.read(
                        source_path, model.data.sheet, source="main",
                        copies_columns=(snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test),
                        version=version, request_id=requests.get(component_id, 0),
                    )
                    loaded = replace(loaded, rows=tuple(
                        replace(row, instance_id=instance_id(snapshot.model.id, component_id, model.data.sheet, row.row_number))
                        for row in loaded.rows
                    ))
                resolver = BindingResolver(snapshot.root, snapshot.model)
                resolver.validate_columns(model, loaded, owner=document.path)
                resolver.validate_static_resources(model, owner=document.path)
                for row in loaded.rows:
                    resolver.resolve_component(model, loaded, row, owner=document.path)
            except ValidationIssues as exc:
                diagnostics.extend(exc.diagnostics)
                failures[cache_key] = (baseline, signature, exc.diagnostics)
            except ProjectError as exc:
                diagnostics.append(exc.diagnostic)
                failures[cache_key] = (baseline, signature, (exc.diagnostic,))
            else:
                staged[cache_key] = (loaded, signature, baseline)
                checked_ids.append(component_id)

        if any(self._requested.get((snapshot.root.resolve(), component_id)) != request for component_id, request in requests.items()):
            return (Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", snapshot.root),)
        if source is not None and source_path is not None and baseline is not None:
            try:
                if self._fingerprint(source_path) != baseline:
                    raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "книга Excel изменилась во время проверки", source_path))
            except ProjectError as exc:
                return (exc.diagnostic if exc.diagnostic.code == "FILE_CHANGED_EXTERNALLY" else
                        Diagnostic("FILE_CHANGED_EXTERNALLY", "книга Excel изменилась во время проверки", source_path),)
        # Publish only after every component has observed the same Excel fingerprint.
        self._data.update({key: value[0] for key, value in staged.items()})
        self._data_fingerprints.update({key: value[2] for key, value in staged.items()})
        self._validated_models.update({key: value[1] for key, value in staged.items()})
        self._validated_models.update(static_staged)
        self._failures.update(failures)
        self._versions.update({key: value[0].version for key, value in staged.items()})
        if source_key is not None and baseline is not None and checked_ids:
            self._fingerprints[source_key] = baseline
            previous = self._persisted_validation.get(source_key)
            prior_ids = previous[1] if previous is not None and previous[0] == baseline else frozenset()
            validated_ids = prior_ids.union(checked_ids)
            if previous != (baseline, validated_ids):
                state_error = self._store_validation_state(snapshot.root, source.path, baseline, list(validated_ids))
                if state_error is not None:
                    diagnostics.append(state_error)
                else:
                    self._persisted_validation[source_key] = (baseline, validated_ids)
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
        validate_resources: bool = True,
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
        resolver = BindingResolver(snapshot.root, snapshot.model, validate_resources=validate_resources)
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
