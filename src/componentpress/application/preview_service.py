"""Application service for immutable per-row component previews."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from componentpress.application.contracts import (
    DocumentSnapshot, FileFingerprint, ProjectSnapshot, RenderResult,
)
from componentpress.application.identifiers import instance_id
from componentpress.application.ports import (
    BindingResolverFactory, ComponentRenderer, DataSourceReader, ValidationStateStore,
)
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.copy_mode import copies_for_mode, validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode
from componentpress.domain.project import DataSource, ProjectDefinition
from componentpress.domain.render_plan import ResolvedComponent
from componentpress.domain.values import DataRow, DataSheetSnapshot


CacheKey = tuple[object, ...]
SourceKey = tuple[Path, str, str]
RequestKey = tuple[Path, str]
ModelSignature = tuple[object, ...]


@dataclass(frozen=True)
class _CachedData:
    snapshot: DataSheetSnapshot
    fingerprint: FileFingerprint
    signature: ModelSignature


@dataclass(frozen=True)
class _ValidationFailure:
    fingerprint: FileFingerprint | None
    signature: ModelSignature
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class _PersistedValidation:
    fingerprint: FileFingerprint
    component_ids: frozenset[str]


@dataclass(frozen=True)
class _ValidationPlan:
    source: DataSource | None
    source_key: SourceKey | None
    root: Path
    component_ids: tuple[str, ...]
    source_path: Path | None
    fingerprint: FileFingerprint | None
    requests: dict[str, int]


@dataclass
class _ValidationBatch:
    cached: dict[CacheKey, _CachedData] = field(default_factory=dict)
    static: dict[tuple[Path, str], ModelSignature] = field(default_factory=dict)
    failures: dict[CacheKey, _ValidationFailure] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    checked_ids: list[str] = field(default_factory=list)


def _node_signature(nodes: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(_single_node_signature(node) for node in nodes)


def _single_node_signature(node: object) -> tuple[object, ...]:
    if isinstance(node, ImageNode):
        return "image", node.source_mode, node.source if node.source_mode == "manual" else node.source_column
    if isinstance(node, HtmlNode):
        return "html", node.content_mode, node.html if node.content_mode == "manual" else node.content_column
    if isinstance(node, ConditionalGroupNode):
        return "conditional", node.condition_column, node.condition_operator, node.condition_value, _node_signature(node.children)
    if isinstance(node, GroupNode):
        return "group", _node_signature(node.children)
    return (getattr(node, "type"),)


def _model_signature(snapshot: ProjectSnapshot, component: ComponentDefinition) -> ModelSignature:
    icons = tuple(sorted((name, item.path, item.width_mm, item.height_mm) for name, item in snapshot.model.icons.items()))
    return (component.data.sheet if component.data else None, _node_signature(component.elements),
            tuple(sorted(snapshot.model.variables.items())), icons)


class PreviewService:
    """Keeps data versions separate from editable YAML and rejects stale reads."""

    def __init__(
        self, reader: DataSourceReader, state_store: ValidationStateStore,
        resolver_factory: BindingResolverFactory, renderer: ComponentRenderer,
    ):
        self.reader = reader
        self._state_store = state_store
        self._resolver_factory = resolver_factory
        self._renderer = renderer
        self._data: dict[CacheKey, _CachedData] = {}
        self._validated_models: dict[tuple[Path, str], ModelSignature] = {}
        self._failures: dict[CacheKey, _ValidationFailure] = {}
        self._fingerprints: dict[SourceKey, FileFingerprint] = {}
        self._persisted_validation: dict[SourceKey, _PersistedValidation] = {}
        self._requested: dict[RequestKey, int] = {}

    def invalidate(self, root: Path | None = None) -> None:
        if root is None:
            self._clear_all()
            return
        resolved = self._state_store.canonical_root(root)
        self._clear_data_for_root(resolved)
        self._clear_sources_for_root(resolved)
        self._clear_requests_for_root(resolved)

    def _clear_all(self) -> None:
        self._data.clear()
        self._validated_models.clear()
        self._failures.clear()
        self._fingerprints.clear()
        self._persisted_validation.clear()
        self._requested.clear()

    def _clear_data_for_root(self, root: Path) -> None:
        for key in tuple(self._data):
            if key[0] == root:
                self._data.pop(key, None)
                self._validated_models.pop(key, None)
                self._failures.pop(key, None)

    def _clear_sources_for_root(self, root: Path) -> None:
        for key in tuple(self._fingerprints):
            if key[0] == root:
                self._fingerprints.pop(key, None)
                self._persisted_validation.pop(key, None)

    def _clear_requests_for_root(self, root: Path) -> None:
        for key in tuple(self._requested):
            if key[0] == root:
                self._requested.pop(key, None)

    def begin_refresh(self, root: Path, component_id: str) -> int:
        key = self._request_key(root, component_id)
        self._requested[key] = self._requested.get(key, 0) + 1
        return self._requested[key]

    def _request_key(self, root: Path, component_id: str) -> RequestKey:
        return self._state_store.canonical_root(root), component_id

    @staticmethod
    def _model(
        snapshot: ProjectSnapshot, component_id: str, component: ComponentDefinition | None,
    ) -> tuple[DocumentSnapshot, ComponentDefinition]:
        document = snapshot.documents.get(component_id)
        if document is None:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
        return document, component or document.model

    def _cache_key(self, snapshot: ProjectSnapshot, component_id: str, component: ComponentDefinition) -> CacheKey | None:
        binding = component.data
        if binding is None:
            return None
        source = snapshot.model.data_source
        if source is None:
            document = snapshot.documents[component_id]
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", f"источник {binding.source!r} не найден", document.path, "data.source"))
        return (self._state_store.canonical_root(snapshot.root), snapshot.model.id, component_id,
                source.path, source.formula_mode, binding.sheet,
                snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test)

    def _source_key(self, snapshot: ProjectSnapshot) -> SourceKey | None:
        source = snapshot.model.data_source
        if source is None:
            return None
        return self._state_store.canonical_root(snapshot.root), source.path, source.formula_mode

    def _validation_plan(
        self, snapshot: ProjectSnapshot, *, component_ids: tuple[str, ...] | None,
        component_overrides: dict[str, ComponentDefinition] | None,
    ) -> tuple[_ValidationPlan | None, Diagnostic | None]:
        source = snapshot.model.data_source
        root = self._state_store.canonical_root(snapshot.root)
        all_ids = tuple(dict.fromkeys((*(item.id for item in snapshot.model.components), *snapshot.documents)))
        ids = component_ids or all_ids
        static_only = self._static_only(snapshot, ids, component_overrides)
        source_key = None if static_only else self._source_key(snapshot)
        try:
            source_path, fingerprint = self._source_state(snapshot, source, static_only)
        except ProjectError as exc:
            return None, exc.diagnostic
        if source_key is not None and self._fingerprints.get(source_key) != fingerprint:
            ids = all_ids
        requests = {item: self.begin_refresh(root, item) for item in ids}
        return _ValidationPlan(source, source_key, root, ids, source_path, fingerprint, requests), None

    @staticmethod
    def _static_only(
        snapshot: ProjectSnapshot, component_ids: tuple[str, ...],
        overrides: dict[str, ComponentDefinition] | None,
    ) -> bool:
        return bool(component_ids) and all(
            component_id in snapshot.documents
            and (overrides or {}).get(component_id, snapshot.documents[component_id].model).data is None
            for component_id in component_ids
        )

    def _source_state(self, snapshot: ProjectSnapshot, source: DataSource | None, static_only: bool) -> tuple[Path | None, FileFingerprint | None]:
        if source is None or static_only:
            return None, None
        path = self._state_store.source_path(snapshot.root, source.path)
        return path, self._state_store.fingerprint(path)

    def refresh(
        self, snapshot: ProjectSnapshot, component_id: str, *, request_id: int | None = None,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        document, model = self._model(snapshot, component_id, component)
        self._reject_stale(snapshot.root, component_id, request_id, document.path)
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

    def _reject_stale(self, root: Path, component_id: str, request_id: int | None, path: Path) -> None:
        current = self._requested.get(self._request_key(root, component_id), 0)
        if request_id is not None and request_id < current:
            raise ProjectError(Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", path))

    def validate_all(
        self, snapshot: ProjectSnapshot, *, force_read: bool = False, manual: bool = True,
        component_ids: tuple[str, ...] | None = None,
        component_overrides: dict[str, ComponentDefinition] | None = None,
    ) -> tuple[Diagnostic, ...]:
        plan, diagnostic = self._validation_plan(
            snapshot, component_ids=component_ids, component_overrides=component_overrides,
        )
        if diagnostic is not None:
            return (diagnostic,)
        batch = self._stage_validation(snapshot, plan, force_read, manual, component_overrides)
        stale = self._stale_source(plan)
        if stale is not None:
            return (stale,)
        self._publish(snapshot, plan, batch)
        return tuple(batch.diagnostics)

    def _stage_validation(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, force_read: bool,
        manual: bool, overrides: dict[str, ComponentDefinition] | None,
    ) -> _ValidationBatch:
        batch = _ValidationBatch()
        for component_id in plan.component_ids:
            document = snapshot.documents.get(component_id)
            if document is None:
                continue
            model = (overrides or {}).get(component_id, document.model)
            self._stage_component(snapshot, plan, batch, component_id, document, model, force_read, manual)
        return batch

    def _stage_component(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch,
        component_id: str, document: DocumentSnapshot, model: ComponentDefinition,
        force_read: bool, manual: bool,
    ) -> None:
        signature = _model_signature(snapshot, model)
        cache_key = self._cache_key(snapshot, component_id, model)
        if cache_key is None:
            self._stage_static(snapshot, batch, component_id, document, model, signature, manual)
            return
        self._stage_sheet(snapshot, plan, batch, component_id, document, model, signature, cache_key, force_read, manual)

    def _stage_static(
        self, snapshot: ProjectSnapshot, batch: _ValidationBatch, component_id: str,
        document: DocumentSnapshot, model: ComponentDefinition, signature: ModelSignature,
        manual: bool,
    ) -> None:
        key = self._state_store.canonical_root(snapshot.root), component_id
        if not manual and self._validated_models.get(key) == signature:
            batch.checked_ids.append(component_id)
            return
        try:
            self._resolver_factory(snapshot.root, snapshot.model).resolve_component(
                model, None, None, owner=document.path,
            )
        except ProjectError as exc:
            self._record_failure(batch, key, None, signature, (exc.diagnostic,))
        else:
            batch.static[key] = signature
            batch.checked_ids.append(component_id)

    def _stage_sheet(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch,
        component_id: str, document: DocumentSnapshot, model: ComponentDefinition,
        signature: ModelSignature, cache_key: CacheKey, force_read: bool, manual: bool,
    ) -> None:
        fingerprint = plan.fingerprint
        assert fingerprint is not None and plan.source_path is not None
        cached = self._data.get(cache_key)
        reused = cached is not None and cached.fingerprint == fingerprint
        if cached is not None and self._reuse_validated(cached, signature, manual, force_read, reused, batch, component_id):
            return
        failure = self._failures.get(cache_key)
        if self._reuse_failure(failure, fingerprint, signature, manual, force_read, batch):
            return
        self._read_and_validate(snapshot, plan, batch, component_id, document, model,
                                signature, cache_key, cached, reused, force_read)

    @staticmethod
    def _reuse_validated(
        cached: _CachedData, signature: ModelSignature, manual: bool,
        force_read: bool, reused: bool,
        batch: _ValidationBatch, component_id: str,
    ) -> bool:
        if not (not force_read and reused and not manual and cached.signature == signature):
            return False
        batch.checked_ids.append(component_id)
        return True

    @staticmethod
    def _reuse_failure(
        failure: _ValidationFailure | None, fingerprint: FileFingerprint | None,
        signature: ModelSignature, manual: bool, force_read: bool, batch: _ValidationBatch,
    ) -> bool:
        if force_read or manual or failure is None or (failure.fingerprint, failure.signature) != (fingerprint, signature):
            return False
        batch.diagnostics.extend(failure.diagnostics)
        return True

    def _read_and_validate(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch,
        component_id: str, document: DocumentSnapshot, model: ComponentDefinition,
        signature: ModelSignature, cache_key: CacheKey, cached: _CachedData | None,
        reused: bool, force_read: bool,
    ) -> None:
        previous = cached.snapshot if cached is not None else None
        version = previous.version + 1 if previous is not None else 1
        try:
            loaded = self._load_data(snapshot, plan, component_id, model, previous, reused, force_read, version)
            self._validate_sheet(snapshot, model, document.path, loaded)
        except ValidationIssues as exc:
            self._record_failure(batch, cache_key, plan.fingerprint, signature, exc.diagnostics)
        except ProjectError as exc:
            self._record_failure(batch, cache_key, plan.fingerprint, signature, (exc.diagnostic,))
        else:
            batch.cached[cache_key] = _CachedData(loaded, plan.fingerprint, signature)
            batch.checked_ids.append(component_id)

    def _load_data(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, component_id: str,
        model: ComponentDefinition, cached: DataSheetSnapshot | None,
        reused: bool, force_read: bool, version: int,
    ) -> DataSheetSnapshot:
        if reused and not force_read:
            assert cached is not None
            return cached
        loaded = self.reader.read(
            plan.source_path, model.data.sheet, source="main",
            copies_columns=(snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test),
            version=version, request_id=plan.requests.get(component_id, 0),
        )
        return replace(loaded, rows=tuple(
            replace(row, instance_id=instance_id(snapshot.model.id, component_id, model.data.sheet, row.row_number))
            for row in loaded.rows
        ))

    def _validate_sheet(
        self, snapshot: ProjectSnapshot, component: ComponentDefinition,
        owner: Path, data: DataSheetSnapshot,
    ) -> None:
        resolver = self._resolver_factory(snapshot.root, snapshot.model)
        resolver.validate_columns(component, data, owner=owner)
        resolver.validate_static_resources(component, owner=owner)
        for row in data.rows:
            resolver.resolve_component(component, data, row, owner=owner)

    @staticmethod
    def _record_failure(
        batch: _ValidationBatch, key: CacheKey, fingerprint: FileFingerprint | None,
        signature: ModelSignature, diagnostics: tuple[Diagnostic, ...],
    ) -> None:
        batch.diagnostics.extend(diagnostics)
        batch.failures[key] = _ValidationFailure(fingerprint, signature, diagnostics)

    def _stale_source(self, plan: _ValidationPlan) -> Diagnostic | None:
        if any(self._requested.get(self._request_key(plan.root, item)) != request
               for item, request in plan.requests.items()):
            return Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", plan.root)
        if plan.source_path is None or plan.fingerprint is None:
            return None
        return self._changed_source_diagnostic(plan.source_path, plan.fingerprint)

    def _changed_source_diagnostic(self, path: Path, fingerprint: FileFingerprint) -> Diagnostic | None:
        try:
            if self._state_store.fingerprint(path) == fingerprint:
                return None
        except ProjectError:
            pass
        return Diagnostic("FILE_CHANGED_EXTERNALLY", "книга Excel изменилась во время проверки", path)

    def _publish(self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch) -> None:
        self._data.update(batch.cached)
        self._validated_models.update(batch.static)
        self._failures.update(batch.failures)
        if plan.source_key is not None and plan.fingerprint is not None and batch.checked_ids:
            self._publish_source_state(snapshot, plan, batch)

    def _publish_source_state(self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch) -> None:
        key, fingerprint = plan.source_key, plan.fingerprint
        assert key is not None and fingerprint is not None and plan.source is not None
        self._fingerprints[key] = fingerprint
        previous = self._persisted_validation.get(key)
        prior_ids = previous.component_ids if previous and previous.fingerprint == fingerprint else frozenset()
        ids = prior_ids.union(batch.checked_ids)
        if previous is not None and (previous.fingerprint, previous.component_ids) == (fingerprint, ids):
            return
        error = self._state_store.store_validation(snapshot.root, plan.source.path, fingerprint, list(ids))
        if error is None:
            self._persisted_validation[key] = _PersistedValidation(fingerprint, ids)
        else:
            batch.diagnostics.append(error)

    def data(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        _document, model = self._model(snapshot, component_id, component)
        if model.data is None:
            return None
        cached = self._data.get(self._cache_key(snapshot, component_id, model))
        return cached.snapshot if cached is not None else self.refresh(snapshot, component_id, component=model)

    def cached_data(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        component: ComponentDefinition | None = None,
    ) -> DataSheetSnapshot | None:
        """Return the latest successful snapshot without causing an XLSX read."""
        _document, model = self._model(snapshot, component_id, component)
        key = self._cache_key(snapshot, component_id, model)
        cached = self._data.get(key) if key is not None else None
        return cached.snapshot if cached is not None else None

    def select_row(
        self, snapshot: ProjectSnapshot, component_id: str, *,
        row_number: int | None = None, instance_id: str | None = None,
        mode: str = "prod", component: ComponentDefinition | None = None,
        validate_resources: bool = True,
    ) -> ResolvedComponent:
        document, model = self._model(snapshot, component_id, component)
        data = self.data(snapshot, component_id, component=model)
        row = _select_row(data, row_number, instance_id, validate_copy_mode(mode))
        resolver = self._resolver_factory(snapshot.root, snapshot.model, validate_resources=validate_resources)
        return resolver.resolve_component(model, data, row, owner=document.path)

    def export_png(
        self, snapshot: ProjectSnapshot, component_id: str, output: Path, *,
        row_number: int | None = None, instance_id: str | None = None,
        dpi: int | None = None, mode: str = "prod",
        component: ComponentDefinition | None = None,
    ) -> RenderResult:
        resolved = self.select_row(snapshot, component_id, row_number=row_number,
                                   instance_id=instance_id, mode=mode, component=component)
        return self._renderer(snapshot, component_id, output, component=resolved.component, dpi=dpi)


def _select_row(
    data: DataSheetSnapshot | None, row_number: int | None,
    selected_id: str | None, mode: str,
) -> DataRow | None:
    if data is None:
        return None
    eligible = tuple(row for row in data.rows if copies_for_mode(row, mode) > 0)
    if not eligible:
        raise ProjectError(Diagnostic("DATA_EMPTY", "на листе нет строк данных", data.path, source=data.source, sheet=data.sheet))
    row = next((item for item in eligible if item.instance_id == selected_id), None) if selected_id is not None else None
    if row_number is not None and selected_id is None:
        row = next((item for item in eligible if item.row_number == row_number), None)
    if row_number is None and selected_id is None:
        row = eligible[0]
    if row is None:
        raise ProjectError(Diagnostic("DATA_ROW_UNKNOWN", "строка предпросмотра не найдена", data.path, source=data.source, sheet=data.sheet))
    return row


class ValidationIssues(ProjectError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]):
        self.diagnostics = diagnostics
        super().__init__(diagnostics[0])
