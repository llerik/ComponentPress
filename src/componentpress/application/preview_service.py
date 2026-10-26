"""Application service for immutable per-row component previews."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Callable
from uuid import uuid4

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
    operation_id: str


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
    icons = tuple(sorted((name, item.path) for name, item in snapshot.model.icons.items()))
    return (component.data.sheet if component.data else None, _node_signature(component.elements),
            tuple(sorted(snapshot.model.variables.items())), snapshot.model.images, icons)


def _drain_diagnostics(resolver) -> tuple[Diagnostic, ...]:
    drain = getattr(resolver, "drain_diagnostics", None)
    return tuple(drain()) if drain is not None else ()


def _attach_warnings(error: ProjectError, warnings: tuple[Diagnostic, ...]) -> None:
    if warnings:
        error.diagnostics = (*warnings, error.diagnostic)


class PreviewService:
    """Keeps data versions separate from editable YAML and rejects stale reads."""

    def __init__(
        self, reader: DataSourceReader, state_store: ValidationStateStore,
        resolver_factory: BindingResolverFactory, renderer: ComponentRenderer,
        *, event_logger: Callable[..., None],
    ):
        self.reader = reader
        self._state_store = state_store
        self._resolver_factory = resolver_factory
        self._renderer = renderer
        self._event_logger = event_logger
        self._data: dict[CacheKey, _CachedData] = {}
        self._validated_models: dict[tuple[Path, str], ModelSignature] = {}
        self._failures: dict[CacheKey, _ValidationFailure] = {}
        self._fingerprints: dict[SourceKey, FileFingerprint] = {}
        self._persisted_validation: dict[SourceKey, _PersistedValidation] = {}
        self._requested: dict[RequestKey, int] = {}
        self._request_counter = 0

    def invalidate(self, root: Path | None = None) -> None:
        if root is None:
            self._clear_all()
            return
        resolved = self._state_store.canonical_root(root)
        for cache in (self._data, self._validated_models, self._failures,
                      self._fingerprints, self._persisted_validation, self._requested):
            self._clear_root(cache, resolved)

    def _clear_all(self) -> None:
        self._data.clear()
        self._validated_models.clear()
        self._failures.clear()
        self._fingerprints.clear()
        self._persisted_validation.clear()
        self._requested.clear()

    @staticmethod
    def _clear_root(cache: dict, root: Path) -> None:
        for key in tuple(cache):
            if key[0] == root:
                cache.pop(key)

    def begin_refresh(self, root: Path, component_id: str) -> int:
        key = self._request_key(root, component_id)
        self._request_counter += 1
        self._requested[key] = self._request_counter
        return self._request_counter

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
        operation_id: str, request_ids: dict[str, int] | None = None,
    ) -> tuple[_ValidationPlan | None, Diagnostic | None]:
        root = self._state_store.canonical_root(snapshot.root)
        all_ids = tuple(dict.fromkeys((*(item.id for item in snapshot.model.components), *snapshot.documents)))
        ids = component_ids or all_ids
        requests = {
            item: (request_ids or {}).get(item) or self.begin_refresh(root, item)
            for item in ids
        }
        plan = _ValidationPlan(snapshot.model.data_source, None, root, ids, None, None, requests, operation_id)
        self._log_validation(plan, "started")
        if self._stale_request(plan):
            return plan, self._stale_diagnostic(root)
        return self._prepare_validation_source(snapshot, plan, all_ids, component_overrides)

    def _prepare_validation_source(self, snapshot, plan, all_ids, component_overrides):
        try:
            return self._plan_source(snapshot, plan, all_ids, component_overrides)
        except ProjectError as exc:
            self._log_validation(plan, "failed", exc.diagnostic.code)
            raise
        except Exception:
            self._log_validation(plan, "failed", "PREVIEW_ERROR")
            raise

    def _plan_source(self, snapshot, plan, all_ids, component_overrides):
        static_only = self._static_only(snapshot, plan.component_ids, component_overrides)
        source_key = None if static_only else self._source_key(snapshot)
        self._log_validation(plan, "started", phase="preparation")
        if self._stale_request(plan):
            return plan, self._stale_diagnostic(plan.root)
        try:
            source_path, fingerprint = self._source_state(snapshot, plan.source, static_only)
        except ProjectError as exc:
            return self._source_error(plan, exc.diagnostic)
        if self._stale_request(plan):
            return plan, self._stale_diagnostic(plan.root)
        return self._complete_plan(plan, source_key, source_path, fingerprint, all_ids)

    def _source_error(self, plan, diagnostic):
        return (plan, self._stale_diagnostic(plan.root) if self._stale_request(plan) else diagnostic)

    def _complete_plan(self, plan, source_key, source_path, fingerprint, all_ids):
        if source_key is not None and self._fingerprints.get(source_key) != fingerprint:
            requests = dict(plan.requests)
            for item in all_ids:
                if item not in requests:
                    requests[item] = self.begin_refresh(plan.root, item)
            plan = replace(plan, component_ids=all_ids, requests=requests)
        return replace(plan, source_key=source_key, source_path=source_path,
                       fingerprint=fingerprint), None

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
        diagnostics = self.validate_all(
            snapshot, manual=True, component_ids=(component_id,),
            component_overrides={component_id: model},
            _request_ids={component_id: request_id} if request_id is not None else None,
        )
        errors = tuple(item for item in diagnostics if item.severity == "error")
        if errors:
            raise ValidationIssues(errors)
        return self.cached_data(snapshot, component_id, component=model)

    def _reject_stale(self, root: Path, component_id: str, request_id: int | None, path: Path) -> None:
        current = self._requested.get(self._request_key(root, component_id), 0)
        if request_id is not None and request_id != current:
            raise ProjectError(self._stale_diagnostic(root))

    @staticmethod
    def _stale_diagnostic(root: Path) -> Diagnostic:
        return Diagnostic("DATA_REFRESH_STALE", "устаревший результат обновления данных отброшен", root)

    def _stale_request(self, plan: _ValidationPlan) -> bool:
        return any(self._requested.get(self._request_key(plan.root, item)) != request
                   for item, request in plan.requests.items())

    def _log_validation(
        self, plan: _ValidationPlan, status: str, code: str | None = None,
        *, phase: str = "preparation",
    ) -> None:
        try:
            self._event_logger("preview.validation", operation="preview", operation_id=plan.operation_id,
                               phase=phase, status=status, code=code)
        except Exception:
            pass

    def validate_all(
        self, snapshot: ProjectSnapshot, *, force_read: bool = False, manual: bool = True,
        component_ids: tuple[str, ...] | None = None,
        component_overrides: dict[str, ComponentDefinition] | None = None,
        _request_ids: dict[str, int] | None = None,
    ) -> tuple[Diagnostic, ...]:
        invalid = self._document_diagnostics(snapshot, component_ids)
        if invalid:
            return invalid
        operation_id = uuid4().hex
        plan, diagnostic = self._validation_plan(
            snapshot, component_ids=component_ids, component_overrides=component_overrides,
            operation_id=operation_id, request_ids=_request_ids,
        )
        if diagnostic is not None or plan is None:
            if plan is not None:
                self._log_validation(plan, "cancelled" if diagnostic.code == "DATA_REFRESH_STALE" else "failed", diagnostic.code)
            assert diagnostic is not None
            return (diagnostic,)
        return self._validate_plan(snapshot, plan, force_read, manual, component_overrides)

    @staticmethod
    def _document_diagnostics(snapshot, component_ids):
        requested = component_ids if component_ids is not None else tuple(snapshot.documents)
        return tuple(diagnostic for key in requested
                     if (document := snapshot.documents.get(key)) is not None
                     for diagnostic in document.diagnostics)

    def _validate_plan(self, snapshot, plan, force_read, manual, component_overrides):
        try:
            batch = self._stage_validation(snapshot, plan, force_read, manual, component_overrides)
        except ProjectError as exc:
            self._log_validation(plan, "failed", exc.diagnostic.code)
            raise
        except Exception:
            self._log_validation(plan, "failed", "PREVIEW_ERROR")
            raise
        return self._finish_validation(snapshot, plan, batch)

    def _finish_validation(self, snapshot, plan, batch):
        stale = self._checked_stale_source(plan)
        if stale is not None:
            self._log_validation(plan, "cancelled", stale.code)
            return (stale,)
        stale = self._publish_logged(snapshot, plan, batch)
        if stale is not None:
            self._log_validation(plan, "cancelled", stale.code)
            return (stale,)
        self._log_validation(plan, "failed" if batch.diagnostics else "succeeded",
                             batch.diagnostics[0].code if batch.diagnostics else None)
        return tuple(batch.diagnostics)

    def _checked_stale_source(self, plan):
        try:
            return self._stale_source(plan)
        except ProjectError as exc:
            self._log_validation(plan, "failed", exc.diagnostic.code)
            raise
        except Exception:
            self._log_validation(plan, "failed", "PREVIEW_ERROR")
            raise

    def _publish_logged(self, snapshot, plan, batch):
        try:
            return self._publish(snapshot, plan, batch)
        except ProjectError as exc:
            self._log_validation(plan, "failed", exc.diagnostic.code)
            raise
        except Exception:
            self._log_validation(plan, "failed", "PREVIEW_ERROR")
            raise

    def _stage_validation(
        self, snapshot: ProjectSnapshot, plan: _ValidationPlan, force_read: bool,
        manual: bool, overrides: dict[str, ComponentDefinition] | None,
    ) -> _ValidationBatch:
        batch = _ValidationBatch()
        for component_id in plan.component_ids:
            document = snapshot.documents.get(component_id)
            if document is None:
                continue
            self._log_validation(plan, "started")
            if self._stale_request(plan):
                break
            model = (overrides or {}).get(component_id, document.model)
            if model is None:
                batch.diagnostics.extend(document.diagnostics)
                continue
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
        resolver = self._resolver_factory(snapshot.root, snapshot.model)
        try:
            resolved = resolver.resolve_component(model, None, None, owner=document.path)
        except ProjectError as exc:
            diagnostics = (*_drain_diagnostics(resolver), exc.diagnostic)
            self._record_failure(batch, key, None, signature, diagnostics)
        else:
            batch.static[key] = signature
            batch.checked_ids.append(component_id)
            if resolved is not None:
                batch.diagnostics.extend(resolved.diagnostics)

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
            warnings = self._validate_sheet(snapshot, model, document.path, loaded)
        except ValidationIssues as exc:
            self._record_failure(batch, cache_key, plan.fingerprint, signature, exc.diagnostics)
        except ProjectError as exc:
            self._record_failure(batch, cache_key, plan.fingerprint, signature,
                                 getattr(exc, "diagnostics", (exc.diagnostic,)))
        else:
            batch.cached[cache_key] = _CachedData(loaded, plan.fingerprint, signature)
            batch.checked_ids.append(component_id)
            batch.diagnostics.extend(warnings)

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
    ) -> tuple[Diagnostic, ...]:
        resolver = self._resolver_factory(snapshot.root, snapshot.model)
        try:
            resolver.validate_columns(component, data, owner=owner)
            resolver.validate_static_resources(component, owner=owner)
        except ProjectError as exc:
            _attach_warnings(exc, _drain_diagnostics(resolver))
            raise
        warnings = _drain_diagnostics(resolver)
        return (*warnings, *self._validate_sheet_rows(resolver, component, owner, data, warnings))

    @staticmethod
    def _validate_sheet_rows(resolver, component, owner, data, previous):
        warnings = []
        for row in data.rows:
            try:
                resolved = resolver.resolve_component(component, data, row, owner=owner)
            except ProjectError as exc:
                _attach_warnings(exc, (*previous, *warnings, *_drain_diagnostics(resolver)))
                raise
            if resolved is not None:
                warnings.extend(resolved.diagnostics)
        return tuple(warnings)

    @staticmethod
    def _record_failure(
        batch: _ValidationBatch, key: CacheKey, fingerprint: FileFingerprint | None,
        signature: ModelSignature, diagnostics: tuple[Diagnostic, ...],
    ) -> None:
        batch.diagnostics.extend(diagnostics)
        batch.failures[key] = _ValidationFailure(fingerprint, signature, diagnostics)

    def _stale_source(self, plan: _ValidationPlan) -> Diagnostic | None:
        if self._stale_request(plan):
            return self._stale_diagnostic(plan.root)
        if plan.source_path is None or plan.fingerprint is None:
            return None
        diagnostic = self._changed_source_diagnostic(plan.source_path, plan.fingerprint)
        return self._stale_diagnostic(plan.root) if self._stale_request(plan) else diagnostic

    def _changed_source_diagnostic(self, path: Path, fingerprint: FileFingerprint) -> Diagnostic | None:
        try:
            if self._state_store.fingerprint(path) == fingerprint:
                return None
        except ProjectError:
            pass
        return Diagnostic("FILE_CHANGED_EXTERNALLY", "книга Excel изменилась во время проверки", path)

    def _publish(self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch) -> Diagnostic | None:
        if plan.source_key is not None and plan.fingerprint is not None and batch.checked_ids:
            diagnostic = self._publish_source_state(snapshot, plan, batch)
            if diagnostic is not None and diagnostic.code == "DATA_REFRESH_STALE":
                return diagnostic
            if diagnostic is not None:
                batch.diagnostics.append(diagnostic)
        if self._stale_request(plan):
            return self._stale_diagnostic(plan.root)
        self._data.update(batch.cached)
        self._validated_models.update(batch.static)
        self._failures.update(batch.failures)
        return None

    def _publish_source_state(self, snapshot: ProjectSnapshot, plan: _ValidationPlan, batch: _ValidationBatch) -> Diagnostic | None:
        key, fingerprint = plan.source_key, plan.fingerprint
        assert key is not None and fingerprint is not None and plan.source is not None
        previous = self._persisted_validation.get(key)
        prior_ids = previous.component_ids if previous and previous.fingerprint == fingerprint else frozenset()
        ids = prior_ids.union(batch.checked_ids)
        if previous is not None and (previous.fingerprint, previous.component_ids) == (fingerprint, ids):
            self._fingerprints[key] = fingerprint
            return None
        return self._store_validation(snapshot, plan, key, fingerprint, ids)

    def _store_validation(self, snapshot, plan, key, fingerprint, ids):
        self._log_validation(plan, "started", phase="publishing")
        if self._stale_request(plan):
            return self._stale_diagnostic(plan.root)
        error = self._state_store.store_validation(snapshot.root, plan.source.path, fingerprint, list(ids))
        if self._stale_request(plan):
            return self._stale_diagnostic(plan.root)
        self._fingerprints[key] = fingerprint
        if error is None:
            self._persisted_validation[key] = _PersistedValidation(fingerprint, ids)
        else:
            return error
        return None

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
