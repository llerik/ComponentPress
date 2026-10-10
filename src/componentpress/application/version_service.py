"""Create verified project-version archives and safely restore their contents."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import socket
import tempfile
import uuid
import zipfile

from componentpress.application.contracts import ProjectSnapshot
from componentpress.application.ports import ArchiveRepository
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.archive_snapshots import snapshot_sources, verify_source_inventory, _is_reparse
from componentpress.project_io.archive_snapshots import EXCLUDED_ROOTS
from componentpress.project_io.files import atomic_write, file_hash
from componentpress.project_io.paths import validate_relative_path
from componentpress.application.ports import ProjectRepository, WriteLockFactory
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.yaml_codec import dump_yaml, parse_project, update_tree
from componentpress.project_io.yaml_codec import parse_component
from componentpress.platforms.services import ProjectWriteLock


MANIFEST = "manifest.json"
PREFIX = "project/"
FORMAT_VERSION = 1
_HEX = re.compile(r"[0-9a-f]{64}")


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


def _manifest_for(archive: zipfile.ZipFile) -> dict:
    try:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or MANIFEST not in names:
            raise ValueError("повторные имена или отсутствует manifest.json")
        metadata = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", f"неверный манифест архива: {exc}")) from exc
    if not isinstance(metadata, dict) or type(metadata.get("format_version")) is not int or metadata.get("format_version") != FORMAT_VERSION:
        raise ProjectError(Diagnostic("ARCHIVE_FORMAT", "неподдерживаемый формат архива проекта"))
    files = metadata.get("files")
    directories = metadata.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, list):
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "манифест не содержит списка файлов и папок"))
    expected = {MANIFEST}
    spellings: dict[str, str] = {}
    explicit: dict[str, bool] = {}
    paths: list[tuple[str, bool]] = []
    for relative, entry in files.items():
        try:
            validate_relative_path(relative)
        except ProjectError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"недопустимый путь в манифесте: {relative!r}")) from exc
        if relative.split("/", 1)[0].casefold() in EXCLUDED_ROOTS:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"служебный каталог нельзя восстанавливать: {relative!r}"))
        if not isinstance(entry, dict) or type(entry.get("size")) is not int or entry["size"] < 0 or not isinstance(entry.get("sha256"), str) or not _HEX.fullmatch(entry["sha256"]):
            raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", f"неверная запись файла {relative!r}"))
        paths.append((relative, True))
        expected.add(PREFIX + relative)
    if "project.yaml" not in files:
        raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "в архиве нет project.yaml"))
    if len({item.casefold() for item in directories}) != len(directories):
        raise ProjectError(Diagnostic("ARCHIVE_PATH", "повторные папки в манифесте"))
    for relative in directories:
        if not isinstance(relative, str):
            raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "неверное имя папки в манифесте"))
        try:
            validate_relative_path(relative)
        except ProjectError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"недопустимый путь папки {relative!r}")) from exc
        if relative.split("/", 1)[0].casefold() in EXCLUDED_ROOTS:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"служебный каталог нельзя восстанавливать: {relative!r}"))
        paths.append((relative, False))
        expected.add(PREFIX + relative + "/")
    for relative, is_file in paths:
        pieces = relative.casefold().split("/")
        original_pieces = relative.split("/")
        for depth in range(1, len(pieces) + 1):
            key = "/".join(pieces[:depth])
            spelling = "/".join(original_pieces[:depth])
            previous = spellings.get(key)
            if previous is not None:
                if previous != spelling:
                    raise ProjectError(Diagnostic("ARCHIVE_PATH", f"конфликт имён путей в архиве: {relative}"))
            else:
                spellings[key] = spelling
        key = relative.casefold()
        if key in explicit:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"путь повторяется как файл или папка: {relative}"))
        explicit[key] = is_file
    for key, is_file in explicit.items():
        pieces = key.split("/")
        for index in range(1, len(pieces)):
            parent = "/".join(pieces[:index])
            if explicit.get(parent) is True:
                raise ProjectError(Diagnostic("ARCHIVE_PATH", f"файл используется как папка: {parent}"))
    actual = {item.filename for item in infos}
    if expected != actual:
        raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", "состав ZIP не совпадает с манифестом"))
    return metadata


def _verify_archive(archive: zipfile.ZipFile, *, extract_to: Path | None = None, cancellation=None, on_progress=None) -> dict:
    metadata = _manifest_for(archive)
    names = set()
    file_infos = [item for item in archive.infolist() if item.filename != MANIFEST and not item.is_dir()]
    for index, info in enumerate(archive.infolist(), 1):
        if cancellation is not None:
            cancellation.check()
        name = info.filename
        if name == MANIFEST:
            continue
        is_dir = name.endswith("/")
        relative = name[len(PREFIX):].rstrip("/")
        try:
            validate_relative_path(relative)
        except ProjectError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"недопустимый путь в ZIP: {name!r}")) from exc
        key = relative.casefold()
        if key in names:
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"пути в архиве не уникальны без учёта регистра: {relative}"))
        names.add(key)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
            raise ProjectError(Diagnostic("ARCHIVE_PATH", f"специальный файл в архиве: {relative}"))
        target = extract_to.joinpath(*relative.split("/")) if extract_to else None
        if is_dir:
            if extract_to is not None:
                target.mkdir(parents=True, exist_ok=True)
            continue
        expected = metadata["files"].get(relative)
        if expected is None:
            raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", f"файл отсутствует в манифесте: {relative}"))
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(info, "r") as stream:
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    output = target.open("xb")
                else:
                    output = None
                try:
                    while True:
                        if cancellation is not None:
                            cancellation.check()
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        digest.update(block)
                        if output is not None:
                            output.write(block)
                finally:
                    if output is not None:
                        output.close()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось прочитать {relative}: {exc}")) from exc
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise ProjectError(Diagnostic("ARCHIVE_HASH", f"контрольная сумма или размер не совпадают: {relative}"))
        if on_progress is not None:
            on_progress(index, len(file_infos), "Проверка архива" if extract_to is None else "Восстановление файлов")
    if extract_to is None:
        for relative, expected in metadata["files"].items():
            try:
                with archive.open(PREFIX + relative, "r") as stream:
                    digest, size = _digest(stream)
            except (KeyError, OSError, zipfile.BadZipFile) as exc:
                raise ProjectError(Diagnostic("ARCHIVE_READ", f"не удалось прочитать {relative}: {exc}")) from exc
            if size != expected["size"] or digest != expected["sha256"]:
                raise ProjectError(Diagnostic("ARCHIVE_HASH", f"контрольная сумма или размер не совпадают: {relative}"))
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


def recover_project_versions(root: Path) -> tuple[Diagnostic, ...]:
    """Finish an interrupted patch update only when archive and source hashes prove ownership."""
    operation_root = root / ".componentpress" / "versioning"
    if not operation_root.is_dir():
        return ()
    state_root = root / ".componentpress"
    if operation_root.is_symlink() or operation_root.resolve().parent != state_root.resolve():
        return (Diagnostic("VERSION_OWNER_UNKNOWN", "каталог архивирования находится вне разрешённого служебного пути и оставлен без изменений", operation_root, severity="warning"),)
    with ProjectWriteLock(root):
        return _recover_project_versions_locked(root, operation_root)


def _recover_project_versions_locked(root: Path, operation_root: Path) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    try:
        current_project, _ = parse_project((root / "project.yaml").read_text(encoding="utf-8"), root / "project.yaml")
    except (OSError, UnicodeError, ProjectError) as exc:
        return (Diagnostic("VERSION_RECOVERY", f"невозможно проверить журнал архивирования: {exc}", root / "project.yaml", severity="warning"),)
    for operation in sorted(operation_root.iterdir()):
        journal = operation / "publish.json"
        marker = operation / "operation.json"
        if not operation.is_dir() or operation.is_symlink():
            continue
        try:
            if not marker.is_file():
                continue
            if marker.is_symlink():
                raise ValueError("служебный маркер является символической ссылкой")
            owner = json.loads(marker.read_text(encoding="utf-8"))
            if (
                owner.get("format_version") != 1
                or owner.get("operation_id") != operation.name
                or owner.get("project_id") != current_project.id
            ):
                diagnostics.append(Diagnostic("VERSION_OWNER_UNKNOWN", "владелец временного архива не подтверждён; данные сохранены", operation, severity="warning"))
                continue
            if not journal.is_file():
                shutil.rmtree(operation)
                continue
            if journal.is_symlink():
                raise ValueError("журнал является символической ссылкой")
            record = json.loads(journal.read_text(encoding="utf-8"))
            if record.get("operation_id") != operation.name or record.get("project_id") != current_project.id:
                raise ValueError("идентификатор операции не совпадает с журналом")
            raw_target = Path(record["target"])
            if raw_target.is_symlink() or raw_target.suffix.casefold() != ".zip":
                raise ValueError("путь архивного файла изменён или недопустим")
            target = raw_target.resolve()
            project_path = root / "project.yaml"
            project_hash = file_hash(project_path)
            archive_matches = target.is_file() and file_hash(target) == record["archive_hash"]
            if not archive_matches:
                # Atomic publication never leaves a partial destination. Keep prior user ZIP untouched.
                journal.unlink()
                shutil.rmtree(operation, ignore_errors=True)
                continue
            with zipfile.ZipFile(target, "r") as archive:
                metadata = _verify_archive(archive)
            if metadata.get("project_id") != record["project_id"] or metadata.get("project_version") != record["project_version"]:
                raise ValueError("архив не совпадает с журналом операции")
            if project_hash == record["project_hash"]:
                _original, tree = parse_project(project_path.read_text(encoding="utf-8"), project_path)
                model, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
                if model.id != record["project_id"] or model.version != record["project_version"]:
                    raise ValueError("версия проекта отличается от журнала")
                updated = model.model_copy(update={"version": record["next_version"]})
                update_tree(tree, updated)
                atomic_write(project_path, dump_yaml(tree).encode("utf-8"), record["project_hash"])
            else:
                model, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
                if model.id != record["project_id"] or model.version != record["next_version"]:
                    raise ProjectError(Diagnostic("VERSION_RECOVERY_CONFLICT", "архив опубликован, но описание проекта изменено; версия не повышена", project_path))
            journal.unlink()
            shutil.rmtree(operation, ignore_errors=True)
        except ProjectError as exc:
            diagnostics.append(exc.diagnostic)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            diagnostics.append(Diagnostic("VERSION_RECOVERY", f"не удалось завершить архивирование; данные сохранены: {exc}", journal, severity="warning"))
    return tuple(diagnostics)


class VersionService(ArchiveRepository):
    """Application facade for archive creation and project recovery."""

    def __init__(self, repository: ProjectRepository | None = None, write_lock: WriteLockFactory = ProjectWriteLock):
        self.repository = repository or FileProjectRepository()
        self.write_lock = write_lock

    def export(self, snapshot: ProjectSnapshot, target: Path, *, replace: bool = False, cancellation=None, on_progress=None) -> tuple[Path, str, bool, str | None]:
        root = snapshot.root.resolve()
        with self.write_lock(root):
            result = export_project_version(snapshot, target, replace=replace, cancellation=cancellation, on_progress=on_progress)
        return result

    def restore_path(self, archive_path: Path, destination: Path, *, cancellation=None, on_progress=None) -> Path:
        return import_project_archive(archive_path, destination, cancellation=cancellation, on_progress=on_progress)

    def restore(self, archive_path: Path, destination: Path, *, cancellation=None, on_progress=None) -> ProjectSnapshot:
        root = self.restore_path(archive_path, destination, cancellation=cancellation, on_progress=on_progress)
        return self.repository.open(root)


def export_project_version(
    snapshot: ProjectSnapshot,
    target: Path,
    *,
    replace: bool = False,
    cancellation=None,
    on_progress=None,
) -> tuple[Path, str, bool, str | None]:
    """Publish one verified archive; return path, next version, and patch status."""
    if target.suffix.casefold() != ".zip":
        raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "для архива проекта выберите файл .zip", target))
    root = snapshot.root.resolve()
    target = Path(os.path.abspath(target.expanduser()))
    if target == root / "project.yaml" or any(target == item.path for item in snapshot.documents.values()):
        raise ProjectError(Diagnostic("ARCHIVE_SOURCE_TARGET", "нельзя заменить исходный файл проекта архивом", target))
    try:
        target.relative_to(root)
        inside_project = True
    except ValueError:
        inside_project = False
    if inside_project:
        candidate = target
        while candidate != root.parent:
            if candidate.exists() or candidate.is_symlink():
                if _is_reparse(candidate):
                    raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "путь архива не должен проходить через символическую ссылку или точку повторной обработки", candidate))
            if candidate == root:
                break
            candidate = candidate.parent
    if inside_project and not (target == root / "archive" or root / "archive" in target.parents):
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "внутри проекта архивы сохраняются только в archive/", target))
    if target.exists():
        if not replace or not _same_project_archive(target, snapshot.model.id):
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "файл уже существует или не является архивом этого проекта", target))
    if not snapshot.documents:
        # Empty projects are archivable; repository validation below still checks project.yaml.
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    operation_root = root / ".componentpress" / "versioning" / uuid.uuid4().hex
    copied = operation_root / "snapshot"
    temporary: Path | None = None
    published = False
    old_version = snapshot.model.version
    new_version = _version_step(old_version)
    project_path = root / "project.yaml"
    try:
        if cancellation is not None:
            cancellation.check()
        operation_root.mkdir(parents=True)
        atomic_write(operation_root / "operation.json", json.dumps({
            "format_version": 1,
            "operation_id": operation_root.name,
            "project_id": snapshot.model.id,
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "phase": "preparing",
        }, ensure_ascii=False).encode("utf-8"))
        copied.mkdir()
        files, directories = snapshot_sources(root, copied, cancellation=cancellation, on_progress=on_progress)
        # An archive located outside archive/ is itself project input: refuse to snapshot it.
        try:
            relative_target = target.resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            relative_target = ""
        if relative_target and relative_target in files:
            raise ProjectError(Diagnostic("ARCHIVE_SOURCE_TARGET", "целевой архив совпадает с исходным файлом проекта", target))
        project_text = (copied / "project.yaml").read_text(encoding="utf-8")
        archived_project, _ = parse_project(project_text, copied / "project.yaml")
        if archived_project.id != snapshot.model.id or archived_project.version != old_version:
            raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "проект изменился после подготовки снимка", project_path))
        if files["project.yaml"]["sha256"] != snapshot.disk_hash:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменилось после чтения", project_path))
        for document in snapshot.documents.values():
            relative = document.path.resolve().relative_to(root).as_posix()
            entry = files.get(relative)
            if entry is None or entry["sha256"] != document.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "документ компонента изменился после чтения", document.path))
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".componentpress-version-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        manifest = {
            "format_version": FORMAT_VERSION,
            "project_id": snapshot.model.id,
            "project_version": old_version,
            "schema_version": archived_project.schema_version,
            "files": files,
            "directories": directories,
        }
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            for relative in directories:
                info = zipfile.ZipInfo(PREFIX + relative.rstrip("/") + "/")
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b"")
            for index, relative in enumerate(sorted(files), 1):
                if cancellation is not None:
                    cancellation.check()
                info = zipfile.ZipInfo(PREFIX + relative)
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w") as outgoing, (copied / Path(relative)).open("rb") as incoming:
                    while True:
                        if cancellation is not None:
                            cancellation.check()
                        block = incoming.read(1024 * 1024)
                        if not block:
                            break
                        outgoing.write(block)
                if on_progress is not None:
                    on_progress(index, len(files), "Упаковка архива")
        with zipfile.ZipFile(temporary, "r") as archive:
            _verify_archive(archive, cancellation=cancellation, on_progress=on_progress)
        if cancellation is not None:
            cancellation.check()
        current = _metadata(temporary)
        if current["project_id"] != snapshot.model.id or current["project_version"] != old_version:
            raise ProjectError(Diagnostic("ARCHIVE_VERIFY", "манифест архива не совпадает с проектом", temporary))
        old_project_hash = file_hash(project_path)
        if old_project_hash != snapshot.disk_hash:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменилось во время архивации", project_path))
        conflict = _version_conflict(root, snapshot.model.id, old_version, target)
        if conflict is not None and not (replace and conflict.resolve() == target.resolve(strict=False)):
            raise ProjectError(Diagnostic("ARCHIVE_VERSION_EXISTS", f"архив версии {old_version} уже существует: {conflict}; явно подтвердите замену", conflict))
        verify_source_inventory(root, files, directories, cancellation=cancellation, on_progress=on_progress)
        if cancellation is not None:
            cancellation.check()
        # The journal lets the next project open finish a crash after archive publication.
        journal = operation_root / "publish.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(journal, json.dumps({
            "operation_id": operation_root.name,
            "target": str(target), "project_id": snapshot.model.id,
            "project_version": old_version, "next_version": new_version,
            "project_hash": old_project_hash, "archive_hash": file_hash(temporary),
        }, ensure_ascii=False).encode("utf-8"))
        if target.exists() and not replace:
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "архив уже существует; подтвердите замену", target))
        if replace and target.exists() and not _same_project_archive(target, snapshot.model.id):
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "архив изменился или принадлежит другому проекту", target))
        os.replace(temporary, target)
        temporary = None
        published = True
        next_snapshot, _ = parse_project(project_path.read_text(encoding="utf-8"), project_path)
        _original, tree = parse_project(project_path.read_text(encoding="utf-8"), project_path)
        next_snapshot = next_snapshot.model_copy(update={"version": new_version})
        update_tree(tree, next_snapshot)
        next_text = dump_yaml(tree).encode("utf-8")
        try:
            atomic_write(project_path, next_text, old_project_hash)
        except ProjectError as exc:
            return target, new_version, False, str(exc.diagnostic)
        journal.unlink(missing_ok=True)
        return target, new_version, True, None
    except ProjectError as exc:
        if not published:
            try:
                (operation_root / "publish.json").unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target, new_version, False, str(exc.diagnostic)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        if not published:
            try:
                (operation_root / "publish.json").unlink(missing_ok=True)
            except OSError:
                pass
            raise ProjectError(Diagnostic("ARCHIVE_EXPORT", f"ошибка создания архива: {exc}", target)) from exc
        return target, new_version, False, f"не удалось обновить версию проекта: {exc}"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if not published:
            shutil.rmtree(operation_root, ignore_errors=True)
        else:
            # Keep the operation journal only if patch publication is unresolved.
            if not (operation_root / "publish.json").exists():
                shutil.rmtree(operation_root, ignore_errors=True)


def import_project_archive(archive_path: Path, destination: Path, *, cancellation=None, on_progress=None) -> Path:
    """Verify a ZIP before extracting it into a new sibling staging directory."""
    archive_path = archive_path.expanduser().absolute()
    destination = destination.expanduser().absolute()
    if archive_path.suffix.casefold() != ".zip":
        raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "выберите ZIP архив проекта", archive_path))
    if destination.exists():
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION_EXISTS", "папка восстановления должна отсутствовать или быть пустой", destination))
    if not destination.parent.is_dir():
        raise ProjectError(Diagnostic("ARCHIVE_DESTINATION", "выберите существующую папку для восстановления", destination.parent))
    staging = destination.parent / f".componentpress-import-{uuid.uuid4().hex}"
    try:
        # Holding this handle prevents path substitution between validation and extraction.
        with archive_path.open("rb") as source:
            before, _ = _digest(source, cancellation)
            if cancellation is not None:
                cancellation.check()
            source.seek(0)
            with zipfile.ZipFile(source, "r") as archive:
                metadata = _verify_archive(archive, cancellation=cancellation, on_progress=on_progress)
                project_text = archive.read(PREFIX + "project.yaml").decode("utf-8")
                project_model, _ = parse_project(project_text, Path("project.yaml"))
                for reference in project_model.components:
                    relative = reference.path
                    if relative not in metadata["files"]:
                        raise ProjectError(Diagnostic("ARCHIVE_CONTENTS", f"в архиве отсутствует компонент {relative}", archive_path))
                    try:
                        component_text = archive.read(PREFIX + relative).decode("utf-8")
                    except (KeyError, UnicodeError) as exc:
                        raise ProjectError(Diagnostic("ARCHIVE_COMPONENT", f"не удаётся прочитать документ компонента {relative}", archive_path)) from exc
                    component, _ = parse_component(component_text, Path(relative), project_model.variables)
                    if component.id != reference.id:
                        raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", f"ID компонента не совпадает с реестром: {relative}", archive_path))
                if metadata.get("project_id") != project_model.id or metadata.get("project_version") != project_model.version or metadata.get("schema_version") != project_model.schema_version:
                    raise ProjectError(Diagnostic("ARCHIVE_MANIFEST", "идентификатор, версия или схема в манифесте не совпадает с проектом", archive_path))
                required_bytes = sum(entry["size"] for entry in metadata["files"].values())
                if required_bytes > shutil.disk_usage(destination.parent).free:
                    raise ProjectError(Diagnostic("ARCHIVE_SPACE", "недостаточно места для восстановления проекта", destination.parent))
                if cancellation is not None:
                    cancellation.check()
                staging.mkdir()
                _verify_archive(archive, extract_to=staging, cancellation=cancellation, on_progress=on_progress)
            source.seek(0)
            after, _ = _digest(source, cancellation)
            if before != after:
                raise ProjectError(Diagnostic("ARCHIVE_SOURCE_CHANGED", "архив изменился во время восстановления", archive_path))
        if cancellation is not None:
            cancellation.check()
        repository = FileProjectRepository()
        restored = repository.open(staging)
        if restored.model.id != metadata["project_id"] or restored.model.version != metadata["project_version"]:
            raise ProjectError(Diagnostic("ARCHIVE_VERIFY", "проект после восстановления не совпадает с манифестом", staging))
        try:
            os.rename(staging, destination)
        except FileExistsError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_DESTINATION_EXISTS", "папку назначения создали во время восстановления", destination)) from exc
        return destination
    except ProjectError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_IMPORT", f"ошибка восстановления архива: {exc}", archive_path)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

