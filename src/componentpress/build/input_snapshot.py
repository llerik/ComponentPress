"""Create the immutable, minimal set of values consumed by a build."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from componentpress.application.contracts import BuildInputSnapshot, BuildInstance, ProjectSnapshot, SnapshotResource
from componentpress.application.identifiers import instance_id as make_instance_id
from componentpress.application.ports import Cancellation, ComponentResolver
from componentpress.bindings.resolver import BindingResolver
from componentpress.data_sources import XlsxReader
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.copy_mode import copies_for_mode, validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.html_resources import html_image_sources
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import PrintSettings
from componentpress.domain.render_plan import ResolvedComponent
from componentpress.domain.schema import PROJECT_MANIFEST
from componentpress.domain.values import DataRow, DataSheetSnapshot
from componentpress.project_io.files import file_hash
from componentpress.project_io.paths import check_exact_case, resolve_project_path
from componentpress.project_io.snapshot_capture import copy_stable, read_stable
from componentpress.rendering.image_quality import measure_component_images
from componentpress.rendering.resources import ProjectResourceLoader
from componentpress.rendering.snapshot_fonts import register_snapshot_fonts as _register_snapshot_fonts
from componentpress.rendering.snapshot_resources import SnapshotResourceLoader
from componentpress.execution.cancellation import CancellationToken


@dataclass
class _BuildPlan:
    instances: list[BuildInstance] = field(default_factory=list)
    resource_paths: set[str] = field(default_factory=set)
    font_families: set[str] = field(default_factory=set)
    input_hashes: dict[str, str] = field(default_factory=dict)
    source_hashes: dict[Path, str] = field(default_factory=dict)
    xlsx_bytes: dict[Path, bytes] = field(default_factory=dict)
    warnings: list[Diagnostic] = field(default_factory=list)


@dataclass
class _ResourceCapture:
    resources: dict[str, SnapshotResource] = field(default_factory=dict)
    input_hashes: dict[str, str] = field(default_factory=dict)
    source_hashes: dict[Path, str] = field(default_factory=dict)
    memory_used: int = 0


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:80] or "instance"


def _resource_paths(component: ComponentDefinition) -> set[str]:
    paths: set[str] = set()

    def visit(nodes: tuple[Node, ...]) -> None:
        for node in nodes:
            if isinstance(node, ImageNode):
                if node.source:
                    paths.add(node.source)
            elif isinstance(node, HtmlNode):
                paths.update(html_image_sources(node.html))
            elif isinstance(node, GroupNode):
                visit(node.children)

    visit(component.elements)
    return paths


def _font_families(component: ComponentDefinition) -> set[str]:
    result: set[str] = set()

    def visit(nodes: tuple[Node, ...]) -> None:
        for node in nodes:
            if isinstance(node, HtmlNode):
                result.add(node.font_family)
            elif isinstance(node, GroupNode):
                visit(node.children)

    visit(component.elements)
    return result


class InputSnapshotBuilder:
    def __init__(self, reader: XlsxReader | None = None):
        self.reader = reader or XlsxReader()

    def _validate_options(self, memory_budget_bytes: int, max_render_workers: int) -> None:
        if memory_budget_bytes < 0:
            raise ProjectError(Diagnostic("BUILD_MEMORY_BUDGET", "бюджет памяти не может быть отрицательным"))
        if max_render_workers not in range(1, 65):
            raise ProjectError(Diagnostic("BUILD_WORKERS", "число потоков должно быть от 1 до 64"))

    @staticmethod
    def _selected_components(source: ProjectSnapshot, component_ids: tuple[str, ...] | None) -> tuple[str, ...]:
        selected = set(component_ids) if component_ids is not None else {item.id for item in source.model.components}
        unknown = selected.difference(source.documents)
        if unknown:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", f"компоненты не найдены: {', '.join(sorted(unknown))}", source.root))
        ordered = tuple(item.id for item in source.model.components if item.id in selected)
        if not ordered:
            raise ProjectError(Diagnostic("BUILD_EMPTY", "не выбраны компоненты для сборки", source.root))
        for component_id in ordered:
            document = source.documents[component_id]
            if document.model is None or any(item.severity == "error" for item in document.diagnostics):
                diagnostic = next(iter(document.diagnostics), None)
                raise ProjectError(diagnostic or Diagnostic("DOCUMENT_INVALID", "component text is invalid", document.path))
        return ordered

    @staticmethod
    def _create_directories(job_directory: Path) -> Path:
        job_directory.mkdir(parents=True, exist_ok=True)
        staging = job_directory / "staging"
        (staging / "images").mkdir(parents=True, exist_ok=True)
        return staging

    def create(
        self, source: ProjectSnapshot, *, job_id: str, job_directory: Path,
        component_ids: tuple[str, ...] | None = None, mode: str = "prod",
        print_settings: PrintSettings | None = None, memory_budget_bytes: int = 128 * 1024 * 1024,
        max_render_workers: int = 2, cancellation: Cancellation | None = None,
        defer_font_registration: bool = False,
    ) -> BuildInputSnapshot:
        token = cancellation or CancellationToken()
        mode = validate_copy_mode(mode)
        self._validate_options(memory_budget_bytes, max_render_workers)
        ordered = self._selected_components(source, component_ids)
        staging = self._create_directories(job_directory)
        plan = self._collect_instances(source, ordered, mode, token)
        captured = self._capture_resources(source, plan, job_directory, memory_budget_bytes, token)
        self._verify_sources(plan.source_hashes, captured.source_hashes, token)
        warnings = (*plan.warnings, *self._quality_warnings(source, plan.instances, captured.resources))
        snapshot = self._assemble_snapshot(source, plan, captured, job_id, job_directory, staging,
                                           print_settings, mode, max_render_workers, memory_budget_bytes, warnings)
        return snapshot if defer_font_registration else self.register_snapshot_fonts(snapshot)

    @staticmethod
    def _record_source(plan: _BuildPlan, path: Path, source: ProjectSnapshot, digest: str) -> None:
        relative = path.relative_to(source.root).as_posix()
        plan.input_hashes[relative] = digest
        plan.source_hashes[path] = digest

    def _read_sheet(self, source: ProjectSnapshot, component: ComponentDefinition, owner: Path,
                    plan: _BuildPlan) -> DataSheetSnapshot:
        binding = component.data
        data_source = source.model.data_source
        if data_source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", f"источник {binding.source!r} не найден", owner, "data.source"))
        check_exact_case(source.root, data_source.path)
        path = resolve_project_path(source.root, data_source.path)
        if path not in plan.xlsx_bytes:
            data, digest = read_stable(path)
            plan.xlsx_bytes[path] = data
            self._record_source(plan, path, source, digest)
        return self.reader.read(
            path, binding.sheet, source="main",
            copies_columns=(source.model.copies_columns.prod, source.model.copies_columns.test),
            data=plan.xlsx_bytes[path],
        )

    @staticmethod
    def _resolve_excel_rows(component: ComponentDefinition, sheet: DataSheetSnapshot, owner: Path,
                            project_id: str, resolver: ComponentResolver,
                            mode: str) -> tuple[tuple[DataRow, ResolvedComponent], ...]:
        resolved_rows = []
        for row in sheet.rows:
            stable = replace(row, instance_id=make_instance_id(
                project_id, component.id, sheet.sheet, row.row_number))
            resolved = resolver.resolve_component(component, sheet, stable, owner=owner)
            if copies_for_mode(stable, mode) > 0:
                resolved_rows.append((stable, resolved))
        return tuple(resolved_rows)

    def _resolve_rows(self, source: ProjectSnapshot, document, plan: _BuildPlan,
                      resolver: ComponentResolver, mode: str) -> tuple[tuple[DataRow | None, ResolvedComponent], ...]:
        component = document.model
        if component.data is None:
            return ((None, resolver.resolve_component(component, None, None, owner=document.path)),)
        sheet = self._read_sheet(source, component, document.path, plan)
        return self._resolve_excel_rows(component, sheet, document.path,
                                        source.model.id, resolver, mode)

    @staticmethod
    def _make_instance(index: int, component_id: str, document, row: DataRow | None, mode: str,
                       resolved: ResolvedComponent) -> BuildInstance:
        instance_id = row.instance_id if row is not None else "static"
        png_name = f"{index + 1:04d}-{_safe_name(component_id)}-{_safe_name(instance_id)}.png"
        return BuildInstance(
            index, component_id, document.model.name, instance_id,
            row.row_number if row is not None else None,
            copies_for_mode(row, mode) if row is not None else 1,
            resolved.component, document.path, png_name,
        )

    def _process_component(self, source: ProjectSnapshot, component_id: str, mode: str,
                           token: Cancellation, resolver: ComponentResolver, plan: _BuildPlan) -> None:
        document = source.documents[component_id]
        self._record_source(plan, document.path, source, document.disk_hash)
        rows = self._resolve_rows(source, document, plan, resolver, mode)
        for row, resolved in rows:
            token.check()
            plan.warnings.extend(resolved.diagnostics)
            instance = self._make_instance(len(plan.instances), component_id, document, row, mode, resolved)
            plan.instances.append(instance)
            plan.resource_paths.update(_resource_paths(resolved.component))
            plan.font_families.update(_font_families(resolved.component))

    def _collect_instances(self, source: ProjectSnapshot, ordered: tuple[str, ...], mode: str,
                           token: Cancellation) -> _BuildPlan:
        plan = _BuildPlan(input_hashes={PROJECT_MANIFEST: source.disk_hash},
                          source_hashes={source.root / PROJECT_MANIFEST: source.disk_hash})
        resolver: ComponentResolver = BindingResolver(
            source.root, source.model, ProjectResourceLoader(source.root))
        for component_id in ordered:
            token.check()
            self._process_component(source, component_id, mode, token, resolver, plan)
        if not plan.instances:
            raise ProjectError(Diagnostic("BUILD_EMPTY", "у выбранных компонентов нет строк с положительным тиражом", source.root))
        return plan

    @staticmethod
    def _capture_resource(source: ProjectSnapshot, relative: str, job_directory: Path,
                          budget: int, token: Cancellation, captured: _ResourceCapture) -> None:
        token.check()
        check_exact_case(source.root, relative)
        path = resolve_project_path(source.root, relative)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
        if captured.memory_used + size <= budget:
            data, digest = read_stable(path)
            resource = SnapshotResource(relative, digest, len(data), data=data)
            captured.memory_used += len(data)
        else:
            copied, digest, size = copy_stable(path, job_directory / "resources")
            resource = SnapshotResource(relative, digest, size, copied_path=copied)
        captured.resources[relative] = resource
        captured.input_hashes[relative] = digest
        captured.source_hashes[path] = digest

    def _capture_resources(self, source: ProjectSnapshot, plan: _BuildPlan, job_directory: Path,
                           budget: int, token: Cancellation) -> _ResourceCapture:
        captured = _ResourceCapture(input_hashes=dict(plan.input_hashes))
        for relative in sorted(plan.resource_paths):
            self._capture_resource(source, relative, job_directory, budget, token, captured)
        return captured

    @staticmethod
    def _verify_sources(source_hashes: Mapping[Path, str], captured_hashes: Mapping[Path, str],
                        token: Cancellation) -> None:
        for path, expected in {**source_hashes, **captured_hashes}.items():
            token.check()
            try:
                actual = file_hash(path)
            except OSError as exc:
                raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
            if actual != expected:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))

    @staticmethod
    def _quality_warnings_for_instance(source: ProjectSnapshot, instance: BuildInstance,
                                       loader: SnapshotResourceLoader) -> list[Diagnostic]:
        binding = instance.component.data
        measurements = measure_component_images(
            instance.component, source.model, loader, instance.document_path, bindings_resolved=True)
        warnings = []
        for measurement in measurements:
            warning = measurement.diagnostic(
                instance.document_path,
                source=binding.source if binding else None,
                sheet=binding.sheet if binding else None,
            )
            if warning is not None:
                if instance.row_number is not None:
                    warning = replace(warning, message=f"строка Excel {instance.row_number}: {warning.message}")
                warnings.append(warning)
        return warnings

    def _quality_warnings(self, source: ProjectSnapshot, instances: list[BuildInstance],
                          resources: Mapping[str, SnapshotResource]) -> tuple[Diagnostic, ...]:
        loader = SnapshotResourceLoader(resources)
        warnings = []
        for instance in instances:
            warnings.extend(self._quality_warnings_for_instance(source, instance, loader))
        return tuple(warnings)

    @staticmethod
    def _assemble_snapshot(source: ProjectSnapshot, plan: _BuildPlan, captured: _ResourceCapture,
                           job_id: str, job_directory: Path, staging: Path,
                           print_settings: PrintSettings | None, mode: str, workers: int,
                           budget: int, warnings: tuple[Diagnostic, ...]) -> BuildInputSnapshot:
        return BuildInputSnapshot(
            job_id=job_id, root=source.root, job_directory=job_directory,
            staging_directory=staging, project=source.model.model_copy(deep=True),
            print_settings=print_settings or source.model.print, mode=mode,
            instances=tuple(plan.instances), resources=MappingProxyType(captured.resources),
            input_hashes=MappingProxyType(captured.input_hashes),
            font_families=tuple(sorted(plan.font_families)), registered_font_ids=(),
            max_render_workers=workers, memory_budget_bytes=budget, warnings=warnings,
        )

    @staticmethod
    def register_snapshot_fonts(snapshot: BuildInputSnapshot) -> BuildInputSnapshot:
        """Register captured fonts in the main thread before render tasks start."""
        return _register_snapshot_fonts(snapshot)
