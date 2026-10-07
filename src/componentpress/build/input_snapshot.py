"""Create the immutable, minimal set of values consumed by a build."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase, QImage, QRawFont

from componentpress.application.contracts import ProjectSnapshot
from componentpress.application.identifiers import instance_id as make_instance_id
from componentpress.bindings.resolver import BindingResolver
from componentpress.data_sources import XlsxReader
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.copy_mode import copies_for_mode, validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import PrintSettings, ProjectDefinition
from componentpress.project_io.files import file_hash
from componentpress.project_io.paths import check_exact_case, resolve_project_path
from componentpress.project_io.yaml_codec import html_image_sources
from componentpress.rendering.resources import LoadedImage, ProjectResourceLoader
from componentpress.execution.cancellation import CancellationToken


@dataclass(frozen=True)
class SnapshotResource:
    relative_path: str
    content_hash: str
    size: int
    data: bytes | None = None
    copied_path: Path | None = None

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.copied_path is None:
            raise RuntimeError("resource has no storage")
        return self.copied_path.read_bytes()


@dataclass(frozen=True)
class BuildInstance:
    index: int
    component_id: str
    component_name: str
    instance_id: str
    row_number: int | None
    copies: int
    component: ComponentDefinition
    document_path: Path
    png_name: str


@dataclass(frozen=True)
class BuildInputSnapshot:
    job_id: str
    root: Path
    job_directory: Path
    staging_directory: Path
    project: ProjectDefinition
    print_settings: PrintSettings
    mode: str
    instances: tuple[BuildInstance, ...]
    resources: Mapping[str, SnapshotResource]
    input_hashes: Mapping[str, str]
    font_families: tuple[str, ...]
    registered_font_ids: tuple[int, ...]
    max_render_workers: int
    memory_budget_bytes: int

    @property
    def total_copies(self) -> int:
        return sum(instance.copies for instance in self.instances)


class SnapshotResourceLoader:
    """Thread-owned QImage cache backed only by captured bytes/copies."""

    def __init__(self, resources: Mapping[str, SnapshotResource], max_cache_bytes: int = 64 * 1024 * 1024):
        self.resources = resources
        self.max_cache_bytes = max_cache_bytes
        self._images: dict[str, LoadedImage] = {}
        self._cache_bytes = 0

    def image(self, relative_path: str, *, owner: Path | None = None, field: str | None = None) -> LoadedImage:
        resource = self.resources.get(relative_path)
        if resource is None:
            raise ProjectError(Diagnostic("RESOURCE_NOT_SNAPSHOTTED", f"ресурс не включён в снимок: {relative_path}", owner, field))
        cached = self._images.get(resource.content_hash)
        if cached is not None:
            return cached
        try:
            data = resource.read()
        except OSError as exc:
            raise ProjectError(Diagnostic("SNAPSHOT_READ", str(exc), resource.copied_path, field)) from exc
        if hashlib.sha256(data).hexdigest() != resource.content_hash:
            raise ProjectError(Diagnostic("SNAPSHOT_CORRUPT", f"повреждена копия ресурса {relative_path}", resource.copied_path, field))
        image = QImage.fromData(QByteArray(data))
        if image.isNull():
            raise ProjectError(Diagnostic("RESOURCE_INVALID", f"не удалось прочитать изображение: {relative_path}", owner, field))
        loaded = LoadedImage(image, resource.content_hash)
        size = image.sizeInBytes()
        if self._cache_bytes + size <= self.max_cache_bytes:
            self._images[resource.content_hash] = loaded
            self._cache_bytes += size
        return loaded


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:80] or "instance"


def _resource_paths(component: ComponentDefinition) -> set[str]:
    paths: set[str] = set()

    def visit(nodes: tuple[Node, ...]) -> None:
        for node in nodes:
            if isinstance(node, ImageNode):
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


def _font_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


class InputSnapshotBuilder:
    def __init__(self, reader: XlsxReader | None = None):
        self.reader = reader or XlsxReader()

    @staticmethod
    def _read_stable(path: Path) -> tuple[bytes, str]:
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("RESOURCE_MISSING", "используемый файл не найден", path)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))
        return data, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _copy_stable(path: Path, directory: Path) -> tuple[Path, str, int]:
        """Stream a large source to managed storage without a full byte buffer."""
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".capture-{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            before = path.stat()
            with path.open("rb") as source, temporary.open("xb") as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(block)
                    digest.update(block)
                    size += len(block)
                target.flush()
                os.fsync(target.fileno())
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))
            content_hash = digest.hexdigest()
            copied = directory / f"{content_hash}{path.suffix.lower()}"
            if copied.exists():
                temporary.unlink()
            else:
                os.replace(temporary, copied)
            return copied, content_hash, size
        except ProjectError:
            raise
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("RESOURCE_MISSING", "используемый файл не найден", path)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def create(
        self,
        source: ProjectSnapshot,
        *,
        job_id: str,
        job_directory: Path,
        component_ids: tuple[str, ...] | None = None,
        mode: str = "prod",
        print_settings: PrintSettings | None = None,
        memory_budget_bytes: int = 128 * 1024 * 1024,
        max_render_workers: int = 2,
        cancellation: CancellationToken | None = None,
        defer_font_registration: bool = False,
    ) -> BuildInputSnapshot:
        token = cancellation or CancellationToken()
        mode = validate_copy_mode(mode)
        if memory_budget_bytes < 0:
            raise ProjectError(Diagnostic("BUILD_MEMORY_BUDGET", "бюджет памяти не может быть отрицательным"))
        if max_render_workers not in range(1, 65):
            raise ProjectError(Diagnostic("BUILD_WORKERS", "число потоков должно быть от 1 до 64"))
        selected = set(component_ids) if component_ids is not None else {item.id for item in source.model.components}
        unknown = selected.difference(source.documents)
        if unknown:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", f"компоненты не найдены: {', '.join(sorted(unknown))}", source.root))
        ordered = [item.id for item in source.model.components if item.id in selected]
        if not ordered:
            raise ProjectError(Diagnostic("BUILD_EMPTY", "не выбраны компоненты для сборки", source.root))

        job_directory.mkdir(parents=True, exist_ok=True)
        staging = job_directory / "staging"
        (staging / "images").mkdir(parents=True, exist_ok=True)
        resource_directory = job_directory / "resources"
        instances: list[BuildInstance] = []
        paths: set[str] = set()
        requested_fonts: set[str] = set()
        input_hashes: dict[str, str] = {"project.yaml": source.disk_hash}
        source_file_hashes: dict[Path, str] = {source.root / "project.yaml": source.disk_hash}
        xlsx_bytes: dict[Path, bytes] = {}
        xlsx_hashes: dict[Path, str] = {}
        live_loader = ProjectResourceLoader(source.root)
        resolver = BindingResolver(source.root, source.model, live_loader)

        for component_id in ordered:
            token.check()
            document = source.documents[component_id]
            input_hashes[document.path.relative_to(source.root).as_posix()] = document.disk_hash
            source_file_hashes[document.path] = document.disk_hash
            component = document.model
            rows = ((None, None),)
            sheet = None
            if component.data is not None:
                data_source = source.model.data_source
                if data_source is None:
                    raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", f"источник {component.data.source!r} не найден", document.path, "data.source"))
                check_exact_case(source.root, data_source.path)
                xlsx_path = resolve_project_path(source.root, data_source.path)
                if xlsx_path not in xlsx_bytes:
                    data, digest = self._read_stable(xlsx_path)
                    xlsx_bytes[xlsx_path] = data
                    xlsx_hashes[xlsx_path] = digest
                    input_hashes[xlsx_path.relative_to(source.root).as_posix()] = digest
                sheet = self.reader.read(
                    xlsx_path,
                    component.data.sheet,
                    source="main",
                    copies_columns=(source.model.copies_columns.prod, source.model.copies_columns.test),
                    data=xlsx_bytes[xlsx_path],
                )
                # Validate every row, including zero-copy rows. Only after
                # resolution succeeds do we retain the stage-8 Prod rows.
                validated_rows = []
                for row in sheet.rows:
                    stable = replace(row, instance_id=make_instance_id(source.model.id, component_id, sheet.sheet, row.row_number))
                    resolved = resolver.resolve_component(component, sheet, stable, owner=document.path)
                    if copies_for_mode(stable, mode) > 0:
                        validated_rows.append((stable, resolved))
                rows = tuple(validated_rows)
            else:
                rows = ((None, resolver.resolve_component(component, None, None, owner=document.path)),)
            for row, resolved in rows:
                token.check()
                copies = copies_for_mode(row, mode) if row is not None else 1
                index = len(instances)
                instance_id = row.instance_id if row is not None else "static"
                png_name = f"{index + 1:04d}-{_safe_name(component_id)}-{_safe_name(instance_id)}.png"
                instance = BuildInstance(
                    index, component_id, component.name, instance_id,
                    row.row_number if row is not None else None, copies,
                    resolved.component, document.path, png_name,
                )
                instances.append(instance)
                paths.update(_resource_paths(resolved.component))
                requested_fonts.update(_font_families(resolved.component))
        if not instances:
            raise ProjectError(Diagnostic("BUILD_EMPTY", "у выбранных компонентов нет строк с положительным тиражом", source.root))

        resources: dict[str, SnapshotResource] = {}
        memory_used = 0
        captured_sources: dict[Path, str] = {}
        for relative in sorted(paths):
            token.check()
            check_exact_case(source.root, relative)
            path = resolve_project_path(source.root, relative)
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
            if memory_used + size <= memory_budget_bytes:
                data, digest = self._read_stable(path)
                entry = SnapshotResource(relative, digest, len(data), data=data)
                memory_used += len(data)
            else:
                copied, digest, size = self._copy_stable(path, resource_directory)
                entry = SnapshotResource(relative, digest, size, copied_path=copied)
            captured_sources[path] = digest
            input_hashes[relative] = digest
            resources[relative] = entry

        # Accept the snapshot only if every source still matches the captured version.
        for path, expected in {**source_file_hashes, **xlsx_hashes, **captured_sources}.items():
            token.check()
            try:
                actual = file_hash(path)
            except OSError as exc:
                raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
            if actual != expected:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "файл изменился во время подготовки снимка", path))
        snapshot = BuildInputSnapshot(
            job_id=job_id,
            root=source.root,
            job_directory=job_directory,
            staging_directory=staging,
            project=source.model.model_copy(deep=True),
            print_settings=print_settings or source.model.print,
            mode=mode,
            instances=tuple(instances),
            resources=MappingProxyType(resources),
            input_hashes=MappingProxyType(input_hashes),
            font_families=tuple(sorted(requested_fonts)),
            registered_font_ids=(),
            max_render_workers=max_render_workers,
            memory_budget_bytes=memory_budget_bytes,
        )
        return snapshot if defer_font_registration else self.register_snapshot_fonts(snapshot)

    def register_snapshot_fonts(self, snapshot: BuildInputSnapshot) -> BuildInputSnapshot:
        """Register and capture only fonts used by printable resolved instances.

        This method must be called in the Qt main thread before render tasks are
        issued. Invalid unrelated font files are deliberately ignored.
        """
        requested = {name.casefold(): name for name in snapshot.font_families}
        requested_keys = {_font_key(name): name for name in snapshot.font_families}
        resources = dict(snapshot.resources)
        input_hashes = dict(snapshot.input_hashes)
        memory_used = sum(item.size for item in resources.values() if item.data is not None)
        resource_directory = snapshot.job_directory / "resources"
        font_ids: list[int] = []
        invalid_candidates: dict[str, Path] = {}
        font_root = snapshot.root / "assets" / "fonts"
        try:
            if font_root.exists():
                for path in sorted(font_root.rglob("*")):
                    if path.is_symlink():
                        raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки шрифтов не поддерживаются", path))
                    if not path.is_file() or path.suffix.lower() not in (".ttf", ".otf"):
                        continue
                    data, digest = self._read_stable(path)
                    raw = QRawFont(QByteArray(data), 16.0)
                    if not raw.isValid():
                        key = _font_key(path.stem)
                        if key in requested_keys:
                            invalid_candidates[key] = path
                        continue
                    family = raw.familyName()
                    if family.casefold() not in requested:
                        continue
                    font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
                    if font_id < 0:
                        raise ProjectError(Diagnostic("FONT_INVALID", "не удалось зарегистрировать используемый шрифт", path))
                    font_ids.append(font_id)
                    if file_hash(path) != digest:
                        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "шрифт изменился во время подготовки снимка", path))
                    relative = path.relative_to(snapshot.root).as_posix()
                    input_hashes[relative] = digest
                    if memory_used + len(data) <= snapshot.memory_budget_bytes:
                        resources[relative] = SnapshotResource(relative, digest, len(data), data=data)
                        memory_used += len(data)
                    else:
                        resource_directory.mkdir(parents=True, exist_ok=True)
                        copied = resource_directory / f"{digest}{path.suffix.lower()}"
                        if not copied.exists():
                            copied.write_bytes(data)
                        resources[relative] = SnapshotResource(relative, digest, len(data), copied_path=copied)

            available = {family.casefold() for family in QFontDatabase.families()}
            invalid = next((invalid_candidates.get(_font_key(name)) for name in snapshot.font_families if _font_key(name) in invalid_candidates), None)
            if invalid is not None:
                raise ProjectError(Diagnostic("FONT_INVALID", "используемый файл шрифта повреждён", invalid))
            missing = sorted(name for name in snapshot.font_families if name.casefold() not in available)
            if missing:
                raise ProjectError(Diagnostic("FONT_MISSING", f"шрифты не найдены: {', '.join(missing)}", snapshot.root))

            return replace(
                snapshot,
                resources=MappingProxyType(resources),
                input_hashes=MappingProxyType(input_hashes),
                registered_font_ids=tuple(font_ids),
            )
        except Exception:
            for font_id in reversed(font_ids):
                QFontDatabase.removeApplicationFont(font_id)
            raise
