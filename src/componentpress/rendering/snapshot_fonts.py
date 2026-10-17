"""Register and capture the project fonts used by a prepared build."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from types import MappingProxyType

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase, QRawFont

from componentpress.application.contracts import BuildInputSnapshot, SnapshotResource
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.files import file_hash
from componentpress.project_io.snapshot_capture import read_stable


def _font_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


@dataclass
class _FontCapture:
    snapshot: BuildInputSnapshot
    requested: set[str]
    requested_keys: dict[str, str]
    resources: dict[str, SnapshotResource]
    input_hashes: dict[str, str]
    memory_used: int
    font_ids: list[int]
    invalid_candidates: dict[str, Path]


def _capture_state(snapshot: BuildInputSnapshot) -> _FontCapture:
    requested = {name.casefold() for name in snapshot.font_families}
    requested_keys = {_font_key(name): name for name in snapshot.font_families}
    resources = dict(snapshot.resources)
    input_hashes = dict(snapshot.input_hashes)
    memory_used = sum(item.size for item in resources.values() if item.data is not None)
    return _FontCapture(snapshot, requested, requested_keys, resources, input_hashes,
                        memory_used, [], {})


def _remember_invalid(state: _FontCapture, path: Path) -> None:
    key = _font_key(path.stem)
    if key in state.requested_keys:
        state.invalid_candidates[key] = path


def _store_font(state: _FontCapture, path: Path, data: bytes, digest: str) -> None:
    snapshot = state.snapshot
    relative = path.relative_to(snapshot.root).as_posix()
    if state.memory_used + len(data) <= snapshot.memory_budget_bytes:
        resource = SnapshotResource(relative, digest, len(data), data=data)
        state.memory_used += len(data)
    else:
        directory = snapshot.job_directory / "resources"
        directory.mkdir(parents=True, exist_ok=True)
        copied = directory / f"{digest}{path.suffix.lower()}"
        if not copied.exists():
            copied.write_bytes(data)
        resource = SnapshotResource(relative, digest, len(data), copied_path=copied)
    state.resources[relative] = resource
    state.input_hashes[relative] = digest


def _capture_font(state: _FontCapture, path: Path) -> None:
    data, digest = read_stable(path)
    raw = QRawFont(QByteArray(data), 16.0)
    if not raw.isValid():
        _remember_invalid(state, path)
        return
    if raw.familyName().casefold() not in state.requested:
        return
    font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
    if font_id < 0:
        raise ProjectError(Diagnostic("FONT_INVALID", "не удалось зарегистрировать используемый шрифт", path))
    state.font_ids.append(font_id)
    if file_hash(path) != digest:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "шрифт изменился во время подготовки снимка", path))
    _store_font(state, path, data, digest)


def _capture_project_fonts(state: _FontCapture) -> None:
    font_root = state.snapshot.root / "assets" / "fonts"
    if not font_root.exists():
        return
    for path in sorted(font_root.rglob("*")):
        if path.is_symlink():
            raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки шрифтов не поддерживаются", path))
        if path.is_file() and path.suffix.lower() in (".ttf", ".otf"):
            _capture_font(state, path)


def _validate_used_fonts(state: _FontCapture) -> None:
    families = state.snapshot.font_families
    invalid = next((state.invalid_candidates.get(_font_key(name)) for name in families
                    if _font_key(name) in state.invalid_candidates), None)
    if invalid is not None:
        raise ProjectError(Diagnostic("FONT_INVALID", "используемый файл шрифта повреждён", invalid))
    available = {family.casefold() for family in QFontDatabase.families()}
    missing = sorted(name for name in families if name.casefold() not in available)
    if missing:
        raise ProjectError(Diagnostic("FONT_MISSING", f"шрифты не найдены: {', '.join(missing)}", state.snapshot.root))


def _release_fonts(font_ids: list[int]) -> None:
    for font_id in reversed(font_ids):
        QFontDatabase.removeApplicationFont(font_id)


def register_snapshot_fonts(snapshot: BuildInputSnapshot) -> BuildInputSnapshot:
    state = _capture_state(snapshot)
    try:
        _capture_project_fonts(state)
        _validate_used_fonts(state)
        return _font_snapshot(state)
    except Exception:
        _release_fonts(state.font_ids)
        raise


def _font_snapshot(state: _FontCapture) -> BuildInputSnapshot:
    return replace(
        state.snapshot,
        resources=MappingProxyType(state.resources),
        input_hashes=MappingProxyType(state.input_hashes),
        registered_font_ids=tuple(state.font_ids),
    )
