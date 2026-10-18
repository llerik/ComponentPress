"""Create verified project-version archives and safely restore their contents."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import socket
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

from componentpress.application.contracts import ArchiveExportResult, ProjectSnapshot
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.archive_snapshots import snapshot_sources, verify_source_inventory, _is_reparse
from componentpress.project_io.archive_snapshots import EXCLUDED_ROOTS
from componentpress.project_io.files import atomic_write, file_hash
from componentpress.project_io.paths import validate_relative_path
from componentpress.application.ports import ProjectRepository, WriteLockFactory
from componentpress.project_io.yaml_codec import dump_yaml, parse_project, update_tree
from componentpress.project_io.yaml_codec import parse_component
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.i18n import tr


MANIFEST = "manifest.json"
PREFIX = "project/"
FORMAT_VERSION = 1
_HEX = re.compile(r"[0-9a-f]{64}")


def _safe_log(event: str, **context) -> None:
    try:
        log_event(event, **context)
    except Exception:
        pass


def _safe_exception(event: str, error: BaseException, **context) -> None:
    try:
        log_exception(event, error, **context)
    except Exception:
        pass


def _log_phase(event: str, phase: str, status: str, operation_id: str, code=None) -> None:
    context = {"operation": "archive", "phase": phase, "status": status, "operation_id": operation_id}
    if code is not None:
        context["code"] = code
    _safe_log(event, **context)


def _recover_pending_registration(root: Path) -> None:
    from componentpress.project_io.transactions import recover_add_component

    recover_add_component(root)


def _digest(stream, cancellation=None) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        if cancellation is not None:
            cancellation.check()
        hasher.update(block)
        size += len(block)
    return hasher.hexdigest(), size


def _version_step(version: str) -> str:
    parts = [int(part) for part in version.split(".")]
    parts[2] += 1
    return ".".join(str(part) for part in parts)


@dataclass
class _Export:
    snapshot: ProjectSnapshot
    target: Path
    root: Path
    operation_id: str
    replace: bool
    cancellation: object
    on_progress: object
    diagnostic_formatter: Callable[[Diagnostic], str]
    old_version: str
    new_version: str
    operation_root: Path
    copied: Path
    project_path: Path
    temporary: Path | None = None
    published: bool = False
    files: dict | None = None
    directories: list | None = None
    old_project_hash: str = ""


def _read_manifest(archive: zipfile.ZipFile) -> tuple[dict, list[zipfile.ZipInfo]]:
    try:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or MANIFEST not in names:
            raise ValueError("повторные имена или отсутствует manifest.json")
        metadata = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", f"неверный манифест архива: {exc}")) from exc
    return metadata, infos


def _register_manifest_path(relative, is_file, entry, explicit, spellings):
    if not isinstance(relative, str):
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "неверное имя пути в манифесте"))
    try:
        validate_relative_path(relative)
    except ProjectError as exc:
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"недопустимый путь в манифесте: {relative!r}")) from exc
    if relative.split("/", 1)[0].casefold() in EXCLUDED_ROOTS:
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"служебный каталог нельзя восстанавливать: {relative!r}"))
    _register_spelling(relative, spellings)
    _register_explicit(relative, is_file, entry, explicit)


def _register_spelling(relative: str, spellings: dict[str, str]) -> None:
    pieces = relative.split("/")
    for depth in range(1, len(pieces) + 1):
        key, spelling = "/".join(pieces[:depth]).casefold(), "/".join(pieces[:depth])
        if key in spellings and spellings[key] != spelling:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"конфликт имён путей в архиве: {relative}"))
        spellings[key] = spelling


def _register_explicit(relative, is_file, entry, explicit) -> None:
    if is_file and (
        not isinstance(entry, dict) or type(entry.get("size")) is not int or entry["size"] < 0
        or not isinstance(entry.get("sha256"), str) or not _HEX.fullmatch(entry["sha256"])
    ):
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", f"неверная запись файла {relative!r}"))
    key = relative.casefold()
    if key in explicit:
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"путь повторяется как файл или папка: {relative}"))
    explicit[key] = is_file


def _check_manifest_parents(explicit: dict[str, bool]) -> None:
    for key in explicit:
        pieces = key.split("/")
        for index in range(1, len(pieces)):
            parent = "/".join(pieces[:index])
            if explicit.get(parent) is True:
                raise ProjectError(Diagnostic("ARCHIVE_PATH", f"файл используется как папка: {parent}"))


def _manifest_for(archive: zipfile.ZipFile) -> dict:
    metadata, infos = _read_manifest(archive)
    if not isinstance(metadata, dict) or type(metadata.get("format_version")) is not int or metadata["format_version"] != FORMAT_VERSION:
        raise ProjectError(Diagnostic("ARCHIVE_FORMAT", "неподдерживаемый формат архива проекта"))
    files, directories = metadata.get("files"), metadata.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, list):
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "манифест не содержит списка файлов и папок"))
    expected, explicit, spellings = {MANIFEST}, {}, {}
    for relative, entry in files.items():
        _register_manifest_path(relative, True, entry, explicit, spellings)
        expected.add(PREFIX + relative)
    if "project.yaml" not in files:
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "в архиве нет project.yaml"))
    for relative in directories:
        _register_manifest_path(relative, False, None, explicit, spellings)
        expected.add(PREFIX + relative + "/")
    _check_manifest_parents(explicit)
    if expected != {item.filename for item in infos}:
        raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", "состав ZIP не совпадает с манифестом"))
    return metadata


def _validate_archive_member(info, relative: str, names: set[str]) -> None:
    try:
        validate_relative_path(relative)
    except ProjectError as exc:
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"недопустимый путь в ZIP: {info.filename!r}")) from exc
    key = relative.casefold()
    if key in names:
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"пути в архиве не уникальны без учёта регистра: {relative}"))
    names.add(key)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
        raise ProjectError(Diagnostic("ARCHIVE_PATH", f"специальный файл в архиве: {relative}"))


def _read_archive_member(archive, info, cancellation, output):
    digest, size = hashlib.sha256(), 0
    with archive.open(info, "r") as stream:
        while True:
            if cancellation is not None:
                cancellation.check()
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest(), size
            size += len(block)
            digest.update(block)
            if output is not None:
                output.write(block)


def _extract_and_hash(archive, info, target, cancellation):
    try:
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                return _read_archive_member(archive, info, cancellation, output)
        return _read_archive_member(archive, info, cancellation, None)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        relative = info.filename[len(PREFIX):]
        raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось прочитать {relative}: {exc}")) from exc


def _verify_member(archive, info, metadata, names, extract_to, cancellation, progress, index, total):
    if info.filename == MANIFEST:
        return
    relative = info.filename[len(PREFIX):].rstrip("/")
    _validate_archive_member(info, relative, names)
    target = extract_to.joinpath(*relative.split("/")) if extract_to else None
    if info.is_dir():
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
        return
    expected = metadata["files"].get(relative)
    if expected is None:
        raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", f"файл отсутствует в манифесте: {relative}"))
    size_hash = _extract_and_hash(archive, info, target, cancellation)
    if size_hash != (expected["sha256"], expected["size"]):
        raise ProjectError(Diagnostic("ARCHIVE_HASH", f"контрольная сумма или размер не совпадают: {relative}"))
    if progress is not None:
        progress(index, total, tr("ui.archive_progress_verify") if extract_to is None else tr("ui.archive_progress_restore"))


def _verify_manifest_hashes(archive, metadata, cancellation) -> None:
    for relative, expected in metadata["files"].items():
        try:
            with archive.open(PREFIX + relative, "r") as stream:
                digest, size = _digest(stream, cancellation)
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось прочитать {relative}: {exc}")) from exc
        if size != expected["size"] or digest != expected["sha256"]:
            raise ProjectError(Diagnostic("ARCHIVE_HASH", f"контрольная сумма или размер не совпадают: {relative}"))


def _verify_archive(archive: zipfile.ZipFile, *, extract_to: Path | None = None, cancellation=None, on_progress=None) -> dict:
    metadata = _manifest_for(archive)
    infos = archive.infolist()
    files = [item for item in infos if item.filename != MANIFEST and not item.is_dir()]
    names = set()
    for index, info in enumerate(infos, 1):
        if cancellation is not None:
            cancellation.check()
        _verify_member(archive, info, metadata, names, extract_to, cancellation, on_progress, index, len(files))
    if extract_to is None:
        _verify_manifest_hashes(archive, metadata, cancellation)
    return metadata


def _metadata(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            return _verify_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_INVALID", f"некорректный ZIP: {exc}", path)) from exc


def _same_project_archive(path: Path, project_id: str) -> bool:
    try:
        return _metadata(path).get("project_id") == project_id
    except ProjectError:
        return False


def _version_conflict(root: Path, project_id: str, version: str, target: Path) -> Path | None:
    archive_root = root / "archive"
    if not archive_root.is_dir():
        return None
    for candidate in archive_root.rglob("*.zip"):
        if candidate.resolve() == target.resolve(strict=False):
            continue
        try:
            metadata = _metadata(candidate)
        except ProjectError:
            continue
        if metadata.get("project_id") == project_id and metadata.get("project_version") == version:
            return candidate
    return None


def _check_export_target(snapshot, target, replace):
    if target.suffix.casefold() != ".zip":
        raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "для архива проекта выберите файл .zip", target))
    root = snapshot.root.resolve()
    target = Path(os.path.abspath(target.expanduser()))
    _validate_export_location(snapshot, root, target)
    if target.exists() and (not replace or not _same_project_archive(target, snapshot.model.id)):
        raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "файл уже существует или не является архивом этого проекта", target))
    return root, target


def _validate_export_location(snapshot, root, target):
    if target == root / "project.yaml" or any(target == item.path for item in snapshot.documents.values()):
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_TARGET", "нельзя заменить исходный файл проекта архивом", target))
    try:
        target.relative_to(root)
    except ValueError:
        return
    _check_destination_reparse(root, target)
    if target != root / "archive" and root / "archive" not in target.parents:
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "внутри проекта архивы сохраняются только в archive/", target))


def _check_destination_reparse(root, target):
    candidate = target
    while candidate != root.parent:
        if (candidate.exists() or candidate.is_symlink()) and _is_reparse(candidate):
            raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "путь архива не должен проходить через символическую ссылку или точку повторной обработки", candidate))
        if candidate == root:
            return
        candidate = candidate.parent


def _new_export(snapshot, target, replace, cancellation, on_progress, formatter, operation_id):
    root, target = _check_export_target(snapshot, target, replace)
    target.parent.mkdir(parents=True, exist_ok=True)
    operation_root = root / ".componentpress" / "versioning" / operation_id
    return _Export(
        snapshot, target, root, operation_id, replace, cancellation, on_progress, formatter,
        snapshot.model.version, _version_step(snapshot.model.version), operation_root,
        operation_root / "snapshot", root / "project.yaml",
    )


def _check_cancel(cancellation):
    if cancellation is not None:
        cancellation.check()


def _mark_export(state):
    state.operation_root.mkdir(parents=True)
    marker = {
        "format_version": 1, "operation_id": state.operation_id,
        "project_id": state.snapshot.model.id, "owner_pid": os.getpid(),
        "owner_host": socket.gethostname(), "phase": "preparing",
    }
    _log_phase("project.archive.marker", "preparation", "started", state.operation_id)
    atomic_write(state.operation_root / "operation.json", json.dumps(marker).encode("utf-8"))
    _log_phase("project.archive.marker", "preparation", "succeeded", state.operation_id)
    state.copied.mkdir()


def _check_snapshot_target(state):
    try:
        relative = state.target.resolve(strict=False).relative_to(state.root).as_posix()
    except ValueError:
        return
    if relative in state.files:
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_TARGET", "целевой архив совпадает с исходным файлом проекта", state.target))


def _check_snapshot_project(state):
    project_path = state.copied / "project.yaml"
    archived, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
    if archived.id != state.snapshot.model.id or archived.version != state.old_version:
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "проект изменился после подготовки снимка", state.project_path))
    if state.files["project.yaml"]["sha256"] != state.snapshot.disk_hash:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменилось после чтения", state.project_path))
    for document in state.snapshot.documents.values():
        _check_snapshot_document(state, document)
    return archived


def _check_snapshot_document(state, document):
    relative = document.path.resolve().relative_to(state.root).as_posix()
    entry = state.files.get(relative)
    if entry is None or entry["sha256"] != document.disk_hash:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "документ компонента изменился после чтения", document.path))


def _write_archive_file(archive, state, relative):
    info = zipfile.ZipInfo(PREFIX + relative)
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with archive.open(info, "w") as outgoing, (state.copied / Path(relative)).open("rb") as incoming:
        while True:
            _check_cancel(state.cancellation)
            block = incoming.read(1024 * 1024)
            if not block:
                return
            outgoing.write(block)


def _write_archive_entries(archive, state):
    for relative in state.directories:
        info = zipfile.ZipInfo(PREFIX + relative.rstrip("/") + "/")
        info.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(info, b"")
    for index, relative in enumerate(sorted(state.files), 1):
        _check_cancel(state.cancellation)
        _write_archive_file(archive, state, relative)
        if state.on_progress is not None:
            state.on_progress(index, len(state.files), tr("ui.archive_progress_pack"))


def _write_archive(state, archived_project):
    manifest = {
        "format_version": FORMAT_VERSION, "project_id": state.snapshot.model.id,
        "project_version": state.old_version, "schema_version": archived_project.schema_version,
        "files": state.files, "directories": state.directories,
    }
    with zipfile.ZipFile(state.temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        _write_archive_entries(archive, state)


def _build_archive(state):
    _mark_export(state)
    _log_phase("project.archive.snapshot", "preparation", "started", state.operation_id)
    state.files, state.directories = snapshot_sources(
        state.root, state.copied, cancellation=state.cancellation, on_progress=state.on_progress,
    )
    _check_snapshot_target(state)
    _log_phase("project.archive.snapshot", "preparation", "succeeded", state.operation_id)
    with tempfile.NamedTemporaryFile(dir=state.target.parent, prefix=".componentpress-version-", suffix=".tmp", delete=False) as stream:
        state.temporary = Path(stream.name)
    archived_project = _check_snapshot_project(state)
    _log_phase("project.archive.package", "preparation", "started", state.operation_id)
    _write_archive(state, archived_project)
    with zipfile.ZipFile(state.temporary, "r") as archive:
        _verify_archive(archive, cancellation=state.cancellation, on_progress=state.on_progress)
    _log_phase("project.archive.package", "preparation", "succeeded", state.operation_id)
    return archived_project


def _validate_publication(state):
    _check_cancel(state.cancellation)
    current = _metadata(state.temporary)
    if current["project_id"] != state.snapshot.model.id or current["project_version"] != state.old_version:
        raise ProjectError(Diagnostic("ARCHIVE_VERIFY", "манифест архива не совпадает с проектом", state.temporary))
    state.old_project_hash = file_hash(state.project_path)
    if state.old_project_hash != state.snapshot.disk_hash:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменилось во время архивации", state.project_path))
    conflict = _version_conflict(state.root, state.snapshot.model.id, state.old_version, state.target)
    if conflict is not None and not (state.replace and conflict.resolve() == state.target.resolve(strict=False)):
        raise ProjectError(Diagnostic("ARCHIVE_VERSION_EXISTS", f"архив версии {state.old_version} уже существует: {conflict}; явно подтвердите замену", conflict))
    verify_source_inventory(state.root, state.files, state.directories, cancellation=state.cancellation, on_progress=state.on_progress)
    _check_cancel(state.cancellation)


def _write_publication_journal(state):
    journal = state.operation_root / "publish.json"
    record = {
        "operation_id": state.operation_id, "target": str(state.target),
        "project_id": state.snapshot.model.id, "project_version": state.old_version,
        "next_version": state.new_version, "project_hash": state.old_project_hash,
        "archive_hash": file_hash(state.temporary),
    }
    _log_phase("project.archive.journal", "publishing", "started", state.operation_id)
    atomic_write(journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    _log_phase("project.archive.journal", "publishing", "succeeded", state.operation_id)
    return journal


def _replace_archive(state):
    if state.target.exists() and not state.replace:
        raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "архив уже существует; подтвердите замену", state.target))
    if state.replace and state.target.exists() and not _same_project_archive(state.target, state.snapshot.model.id):
        raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "архив изменился или принадлежит другому проекту", state.target))
    _log_phase("project.archive.publish", "publishing", "started", state.operation_id)
    os.replace(state.temporary, state.target)
    state.temporary, state.published = None, True
    _log_phase("project.archive.publish", "publishing", "succeeded", state.operation_id)


def _patch_published_version(state):
    _recover_pending_registration(state.root)
    model, _ = parse_project(state.project_path.read_text(encoding="utf-8"), state.project_path)
    _original, tree = parse_project(state.project_path.read_text(encoding="utf-8"), state.project_path)
    updated = model.model_copy(update={"version": state.new_version})
    update_tree(tree, updated)
    _log_phase("project.archive.version_patch", "publishing", "started", state.operation_id)
    result = atomic_write(state.project_path, dump_yaml(tree).encode("utf-8"), state.old_project_hash)
    _log_phase("project.archive.version_patch", "publishing", "succeeded", state.operation_id)
    return result


def _publish_archive(state) -> ArchiveExportResult:
    _validate_publication(state)
    journal = _write_publication_journal(state)
    _replace_archive(state)
    try:
        _patch_published_version(state)
        journal.unlink(missing_ok=True)
        return ArchiveExportResult(state.target, state.new_version, True)
    except ProjectError as exc:
        return ArchiveExportResult(state.target, state.new_version, False, state.diagnostic_formatter(exc.diagnostic))


def _cleanup_export(state):
    _log_phase("project.archive.cleanup", "cleanup", "started", state.operation_id)
    if state.temporary is not None:
        state.temporary.unlink(missing_ok=True)
    journal = state.operation_root / "publish.json"
    if not state.published or not journal.exists():
        shutil.rmtree(state.operation_root, ignore_errors=True)
    _log_phase("project.archive.cleanup", "cleanup", "succeeded", state.operation_id)


def _handle_project_export_error(state, error: ProjectError) -> ArchiveExportResult:
    if not state.published:
        _discard_export_journal(state)
        raise error
    return ArchiveExportResult(state.target, state.new_version, False, state.diagnostic_formatter(error.diagnostic))


def _handle_io_export_error(state, error: OSError | zipfile.BadZipFile | ValueError) -> ArchiveExportResult:
    if not state.published:
        _discard_export_journal(state)
        raise ProjectError(Diagnostic("ARCHIVE_EXPORT", f"ошибка создания архива: {error}", state.target)) from error
    return ArchiveExportResult(state.target, state.new_version, False, f"не удалось обновить версию проекта: {error}")


def _discard_export_journal(state):
    try:
        (state.operation_root / "publish.json").unlink(missing_ok=True)
    except OSError:
        pass


def export_project_version(snapshot, target, *, replace=False, cancellation=None, on_progress=None, diagnostic_formatter, operation_id) -> ArchiveExportResult:
    state = _new_export(snapshot, target, replace, cancellation, on_progress, diagnostic_formatter, operation_id)
    try:
        _check_cancel(cancellation)
        _build_archive(state)
        return _publish_archive(state)
    except ProjectError as exc:
        return _handle_project_export_error(state, exc)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return _handle_io_export_error(state, exc)
    finally:
        _cleanup_export(state)


def _validate_archive_components(archive, metadata, project_model, archive_path):
    for reference in project_model.components:
        relative = reference.path
        if relative not in metadata["files"]:
            raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", f"в архиве отсутствует компонент {relative}", archive_path))
        try:
            text = archive.read(PREFIX + relative).decode("utf-8")
        except (KeyError, UnicodeError) as exc:
            raise ProjectError(Diagnostic("ARCHIVE_COMPONENT", f"не удаётся прочитать документ компонента {relative}", archive_path)) from exc
        component, _ = parse_component(text, Path(relative), project_model.variables)
        if component.id != reference.id:
            raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", f"ID компонента не совпадает с реестром: {relative}", archive_path))


def _validate_archive_project(archive, metadata, archive_path, destination_parent):
    text = archive.read(PREFIX + "project.yaml").decode("utf-8")
    project, _ = parse_project(text, Path("project.yaml"))
    _validate_archive_components(archive, metadata, project, archive_path)
    expected = (project.id, project.version, project.schema_version)
    actual = (metadata.get("project_id"), metadata.get("project_version"), metadata.get("schema_version"))
    if actual != expected:
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "идентификатор, версия или схема в манифесте не совпадает с проектом", archive_path))
    required = sum(entry["size"] for entry in metadata["files"].values())
    if required > shutil.disk_usage(destination_parent).free:
        raise ProjectError(Diagnostic("ARCHIVE_SPACE", "недостаточно места для восстановления проекта", destination_parent))
    return project


def _extract_archive(archive, staging, cancellation, on_progress, operation_id):
    _check_cancel(cancellation)
    _log_phase("project.archive.extract", "import", "started", operation_id)
    staging.mkdir()
    _verify_archive(archive, extract_to=staging, cancellation=cancellation, on_progress=on_progress)
    _log_phase("project.archive.extract", "import", "succeeded", operation_id)


def _extract_verified_source(source, archive_path, destination_parent, staging, cancellation, on_progress, operation_id):
    before, _ = _digest(source, cancellation)
    _check_cancel(cancellation)
    source.seek(0)
    with zipfile.ZipFile(source, "r") as archive:
        _log_phase("project.archive.verify", "import", "started", operation_id)
        metadata = _verify_archive(archive, cancellation=cancellation, on_progress=on_progress)
        _validate_archive_project(archive, metadata, archive_path, destination_parent)
        _log_phase("project.archive.verify", "import", "succeeded", operation_id)
        _extract_archive(archive, staging, cancellation, on_progress, operation_id)
    source.seek(0)
    if before != _digest(source, cancellation)[0]:
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "архив изменился во время восстановления", archive_path))
    return metadata


def _publish_restored_project(metadata, staging, destination, repository, operation_id):
    restored = repository.open(staging)
    if (restored.model.id, restored.model.version) != (metadata["project_id"], metadata["project_version"]):
        raise ProjectError(Diagnostic("ARCHIVE_VERIFY", "проект после восстановления не совпадает с манифестом", staging))
    try:
        _log_phase("project.archive.restore_publish", "publishing", "started", operation_id)
        os.rename(staging, destination)
    except FileExistsError as exc:
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION_EXISTS", "папку назначения создали во время восстановления", destination)) from exc
    _log_phase("project.archive.restore_publish", "publishing", "succeeded", operation_id)
    return destination


def _validate_import_paths(archive_path, destination):
    archive_path, destination = archive_path.expanduser().absolute(), destination.expanduser().absolute()
    if archive_path.suffix.casefold() != ".zip":
        raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "выберите ZIP архив проекта", archive_path))
    if destination.exists():
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION_EXISTS", "папка восстановления должна отсутствовать или быть пустой", destination))
    if not destination.parent.is_dir():
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "выберите существующую папку для восстановления", destination.parent))
    return archive_path, destination


def import_project_archive(archive_path, destination, repository, *, cancellation=None, on_progress=None, operation_id):
    """Verify a ZIP before extracting it into a new sibling staging directory."""
    archive_path, destination = _validate_import_paths(archive_path, destination)
    staging = destination.parent / f".componentpress-import-{uuid.uuid4().hex}"
    try:
        with archive_path.open("rb") as source:
            metadata = _extract_verified_source(
                source, archive_path, destination.parent, staging, cancellation, on_progress, operation_id,
            )
        return _publish_restored_project(metadata, staging, destination, repository, operation_id)
    except ProjectError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_IMPORT", f"ошибка восстановления архива: {exc}", archive_path)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


class ZipArchiveRepository:
    """Filesystem implementation of the application archive port."""

    def __init__(self, projects: ProjectRepository, write_lock: WriteLockFactory, *, diagnostic_formatter: Callable[[Diagnostic], str]):
        self.projects = projects
        self.write_lock = write_lock
        self.diagnostic_formatter = diagnostic_formatter

    def export(self, snapshot, target, *, replace=False, cancellation=None, on_progress=None):
        operation_id = uuid.uuid4().hex
        _safe_log("project.archive", operation="archive", phase="export", status="started", operation_id=operation_id)
        try:
            with self.write_lock(snapshot.root.resolve()):
                _recover_pending_registration(snapshot.root)
                result = export_project_version(
                    snapshot, target, replace=replace, cancellation=cancellation,
                    on_progress=on_progress, diagnostic_formatter=self.diagnostic_formatter,
                    operation_id=operation_id,
                )
        except ProjectError as exc:
            self._log_failure(exc, operation_id)
            raise
        except Exception as exc:
            _safe_exception("project.archive", exc, operation="archive", phase="export", status="failed", operation_id=operation_id)
            raise
        self._log_export_result(result, operation_id)
        return result

    def restore_path(self, archive_path, destination, *, cancellation=None, on_progress=None):
        operation_id = uuid.uuid4().hex
        _safe_log("project.archive", operation="import", phase="import", status="started", operation_id=operation_id)
        try:
            result = import_project_archive(
                archive_path, destination, self.projects, cancellation=cancellation,
                on_progress=on_progress, operation_id=operation_id,
            )
        except ProjectError as exc:
            self._log_failure(exc, operation_id, operation="import")
            raise
        except Exception as exc:
            _safe_exception("project.archive", exc, operation="import", phase="import", status="failed", operation_id=operation_id)
            raise
        _safe_log("project.archive", operation="import", phase="finished", status="succeeded", operation_id=operation_id)
        return result

    @staticmethod
    def _log_failure(error, operation_id, operation="archive"):
        status = "cancelled" if error.diagnostic.code == "BUILD_CANCELLED" else "failed"
        _safe_log("project.archive", operation=operation, phase="finished", status=status, code=error.diagnostic.code, operation_id=operation_id)

    @staticmethod
    def _log_export_result(result, operation_id):
        if not result.postprocessing_complete:
            _safe_log("project.archive.version_patch", operation="archive", phase="publishing", status="failed", code="VERSION_PATCH", operation_id=operation_id)
        _safe_log("project.archive", operation="archive", phase="finished", status="succeeded", operation_id=operation_id)

