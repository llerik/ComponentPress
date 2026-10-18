"""Focused regression checks for preview cache ownership and request generations."""

from __future__ import annotations

from pathlib import Path

import pytest

from componentpress.application.contracts import DocumentSnapshot, FileFingerprint, ProjectSnapshot
from componentpress.application.preview_service import PreviewService, ValidationIssues
from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.project import ComponentRef, DataSource, ProjectDefinition
from componentpress.domain.values import DataRow, DataSheetSnapshot


def _component(component_id: str, *, name: str | None = None, bound: bool = False) -> ComponentDefinition:
    return ComponentDefinition(
        schema_version=7, id=component_id, name=name or component_id,
        size_mm=SizeMM(width=40, height=30),
        data=DataBinding(sheet="Sheet") if bound else None,
    )


def _snapshot(root: Path, components: tuple[ComponentDefinition, ...]) -> ProjectSnapshot:
    project = ProjectDefinition(
        schema_version=7, id=root.name, name=root.name, version="0.1.0",
        data_source=DataSource(path="source.xlsx") if any(item.data for item in components) else None,
        components=tuple(ComponentRef(id=item.id, path=f"components/{item.id}.yaml") for item in components),
    )
    documents = {
        item.id: DocumentSnapshot(root / "components" / f"{item.id}.yaml", "", "", item)
        for item in components
    }
    return ProjectSnapshot(root, "", "", project, documents)


class _Store:
    def __init__(self) -> None:
        self.stored: list[tuple[Path, str, FileFingerprint, list[str]]] = []
        self.on_fingerprint = None
        self.on_store = None

    def canonical_root(self, root: Path) -> Path:
        return root.resolve()

    def source_path(self, root: Path, relative_path: str) -> Path:
        return root / relative_path

    def fingerprint(self, path: Path) -> FileFingerprint:
        if self.on_fingerprint is not None:
            self.on_fingerprint()
        return FileFingerprint(1, 2)

    def store_validation(self, root, source_path, fingerprint, component_ids):
        if self.on_store is not None:
            self.on_store()
        self.stored.append((root, source_path, fingerprint, list(component_ids)))
        return None


class _Reader:
    def __init__(self) -> None:
        self.on_read = None
        self.reads = 0

    def read(self, path, sheet, **kwargs):
        self.reads += 1
        read = self.reads
        if self.on_read is not None:
            self.on_read()
        return DataSheetSnapshot(
            "main", path, sheet, "B" if read > 1 else "A", kwargs["version"], (),
            (DataRow(2, "row", 1, {}),), kwargs["request_id"],
        )


class _Resolver:
    def __init__(self) -> None:
        self.on_resolve = None

    def validate_columns(self, component, data, *, owner=None) -> None:
        return None

    def validate_static_resources(self, component, *, owner=None) -> None:
        return None

    def resolve_component(self, component, data, row, *, owner=None):
        if self.on_resolve is not None:
            self.on_resolve()
        if component.name == "Invalid override":
            raise ProjectError(Diagnostic("OVERRIDE_BAD", "", owner, "elements"))
        return None


def _service(store=None, reader=None, resolver=None, logger=None):
    store = store or _Store()
    reader = reader or _Reader()
    resolver = resolver or _Resolver()
    events = []
    service = PreviewService(
        reader, store, lambda *_args, **_kwargs: resolver,
        lambda *_args, **_kwargs: None,
        event_logger=logger or (lambda event, **context: events.append((event, context))),
    )
    return service, store, reader, resolver, events


def test_invalidate_root_clears_static_and_failure_only_preview_state(tmp_path: Path) -> None:
    service, _store, _reader, _resolver, _events = _service()
    first = _snapshot(tmp_path / "first", (
        _component("static"), _component("broken", name="Invalid override"),
        _component("bound", bound=True),
    ))
    second = _snapshot(tmp_path / "second", (
        _component("other-static"), _component("other-broken", name="Invalid override"),
        _component("other-bound", bound=True),
    ))
    assert len(service.validate_all(first)) == 1
    assert len(service.validate_all(second)) == 1

    first_root = first.root.resolve()
    assert any(key[0] == first_root for key in service._data)
    assert (first_root, "static") in service._validated_models
    assert (first_root, "broken") in service._failures
    assert any(key[0] == first_root for key in service._fingerprints)
    assert any(key[0] == first_root for key in service._persisted_validation)
    assert any(key[0] == first_root for key in service._requested)

    service.invalidate(first.root / "nested" / "..")
    for cache in (service._data, service._validated_models, service._failures,
                  service._fingerprints, service._persisted_validation, service._requested):
        assert all(key[0] != first_root for key in cache)
        assert any(key[0] == second.root.resolve() for key in cache)
    service.invalidate()
    assert all(not cache for cache in (service._data, service._validated_models, service._failures,
                                       service._fingerprints, service._persisted_validation,
                                       service._requested))


def test_static_refresh_reports_invalid_model_override(tmp_path: Path) -> None:
    service, _store, _reader, _resolver, _events = _service()
    original = _component("card")
    snapshot = _snapshot(tmp_path / "project", (original,))
    invalid = original.model_copy(update={"name": "Invalid override"})

    with pytest.raises(ValidationIssues) as caught:
        service.refresh(snapshot, "card", component=invalid)
    assert [(issue.code, issue.field, issue.path) for issue in caught.value.diagnostics] == [
        ("OVERRIDE_BAD", "elements", snapshot.documents["card"].path),
    ]
    assert (snapshot.root.resolve(), "card") not in service._validated_models
    assert service.refresh(snapshot, "card", component=original) is None
    assert snapshot.documents["card"].model is original
    assert (snapshot.root.resolve(), "card") in service._validated_models


def test_preview_validation_is_unaffected_by_logging_failure(tmp_path: Path) -> None:
    def broken_logger(*_args, **_kwargs):
        raise OSError("log unavailable")

    service, _store, _reader, _resolver, _events = _service(logger=broken_logger)
    snapshot = _snapshot(tmp_path / "project", (_component("card"),))
    assert service.validate_all(snapshot) == ()
    assert (snapshot.root.resolve(), "card") in service._validated_models


def test_validation_failure_is_logged_without_replacing_primary_error(tmp_path: Path) -> None:
    service, store, _reader, _resolver, events = _service()
    snapshot = _snapshot(tmp_path / "project", (_component("card", bound=True),))
    failure = OSError("state store unavailable")

    def fail_store(*_args):
        raise failure

    store.store_validation = fail_store
    with pytest.raises(OSError) as caught:
        service.validate_all(snapshot)
    assert caught.value is failure
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["code"] == "PREVIEW_ERROR"


@pytest.mark.parametrize("phase", ("fingerprint_first", "reader", "resolver", "fingerprint_last", "store"))
def test_invalidate_supersedes_inflight_request_generation(tmp_path: Path, phase: str) -> None:
    store, reader, resolver = _Store(), _Reader(), _Resolver()
    service, _store, _reader, _resolver, _events = _service(store, reader, resolver)
    snapshot = _snapshot(tmp_path / "project", (_component("card", bound=True),))
    fired = False

    def invalidate_and_publish_b() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        if phase == "fingerprint_first":
            reader.reads += 1
        service.invalidate(snapshot.root)
        assert service.validate_all(snapshot, component_ids=("card",)) == ()

    if phase == "fingerprint_first":
        store.on_fingerprint = invalidate_and_publish_b
    elif phase == "fingerprint_last":
        fingerprint_calls = 0

        def on_fingerprint() -> None:
            nonlocal fingerprint_calls
            fingerprint_calls += 1
            if fingerprint_calls == 2:
                invalidate_and_publish_b()

        store.on_fingerprint = on_fingerprint
    elif phase == "reader":
        reader.on_read = invalidate_and_publish_b
    elif phase == "resolver":
        resolver.on_resolve = invalidate_and_publish_b
    else:
        store.on_store = invalidate_and_publish_b

    request = service.begin_refresh(snapshot.root, "card")
    with pytest.raises(ProjectError) as caught:
        service.refresh(snapshot, "card", request_id=request)
    assert caught.value.diagnostic.code == "DATA_REFRESH_STALE"
    cached = service.cached_data(snapshot, "card")
    assert cached is not None and cached.content_hash == "B"
    assert service._requested[(snapshot.root.resolve(), "card")] > request
    assert (snapshot.root.resolve(), "source.xlsx", "cached") in service._persisted_validation
    stored_count = len(store.stored)
    assert service.validate_all(snapshot, component_ids=("card",)) == ()
    assert service.cached_data(snapshot, "card").content_hash == "B"
    assert len(store.stored) == stored_count
