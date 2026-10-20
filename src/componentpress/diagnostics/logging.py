"""Private, bounded technical logs that never contain project content."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import traceback
from uuid import uuid4
import zipfile
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QLockFile, QStandardPaths

from componentpress.version import APPLICATION_VERSION

_LOGGER = logging.getLogger("componentpress")
_SESSION_ID = uuid4().hex
_LOG_DIRECTORY: Path | None = None
_LOG_PATH: Path | None = None
_BUFFER: list[str] = []
_SESSION_LOCK: QLockFile | None = None
_MAX_BYTES = 5 * 1024 * 1024
_MAX_BACKUPS = 3
_MAX_SESSIONS = 20
_MAX_AGE_DAYS = 14


def _directory() -> Path:
    if os.environ.get("COMPONENTPRESS_LOG_DIR"):
        return Path(os.environ["COMPONENTPRESS_LOG_DIR"])
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not location:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if not location:
        raise OSError("application data directory is unavailable")
    return Path(location) / "logs"


_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.]{0,79}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_OPERATIONS = {"create", "open", "migrate", "rename", "resize", "register", "file_association", "program_settings", "save", "project_settings", "import_image", "import_image_folder", "import_data", "build", "render", "preview", "archive", "export", "import", "dialog", "remove", "restore", "png_export", "add_component", "remove_component", "restore_component", "discard_component_history_backup"}
_PHASES = {"preparing", "preparation", "rendering", "render", "fonts", "png", "assembling_pdf", "publishing", "finished", "failed", "cancelled", "coordinator", "font_cleanup", "lock_release", "completion", "cancelling", "succeeded", "cleanup", "export", "import"}


def _owned_log(path: Path) -> bool:
    return path.name.startswith("componentpress-") and path.suffix == ".log" and not path.is_symlink() and path.is_file()


def _prune(directory: Path, current: Path) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - _MAX_AGE_DAYS * 86400
    entries: list[Path] = []
    for path in directory.iterdir():
        if path == current or not _owned_log(path):
            continue
        lock = QLockFile(str(path.with_suffix(".active")))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                for index in range(1, _MAX_BACKUPS + 1):
                    path.with_suffix(path.suffix + f".{index}").unlink(missing_ok=True)
            else:
                entries.append(path)
        except OSError:
            continue
        finally:
            lock.unlock()
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for old in entries[max(0, _MAX_SESSIONS - 1):]:
        lock = QLockFile(str(old.with_suffix(".active")))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            continue
        try:
            old.unlink()
            for index in range(1, _MAX_BACKUPS + 1):
                old.with_suffix(old.suffix + f".{index}").unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            lock.unlock()


def initialize() -> Path | None:
    """Start a per-process rotating log; logging failure never blocks startup."""
    global _LOG_DIRECTORY, _LOG_PATH, _SESSION_LOCK
    if _LOG_PATH is not None:
        return _LOG_PATH
    try:
        _LOG_DIRECTORY = _directory()
        _LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        _LOG_PATH = _LOG_DIRECTORY / f"componentpress-{_SESSION_ID}.log"
        _SESSION_LOCK = QLockFile(str(_LOG_PATH.with_suffix(".active")))
        _SESSION_LOCK.setStaleLockTime(0)
        if not _SESSION_LOCK.tryLock(0):
            _LOG_PATH = None
            return None
        _prune(_LOG_DIRECTORY, _LOG_PATH)
        handler = RotatingFileHandler(
            _LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_MAX_BACKUPS,
            encoding="utf-8", delay=False,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"))
        _LOGGER.setLevel(logging.INFO)
        _LOGGER.addHandler(handler)
        for record in _BUFFER:
            _LOGGER.info("%s", record)
        _BUFFER.clear()
        log_event("application.start", version=APPLICATION_VERSION)
        return _LOG_PATH
    except (OSError, ValueError):
        if _SESSION_LOCK is not None:
            _SESSION_LOCK.unlock()
            _SESSION_LOCK = None
        _LOG_DIRECTORY = None
        _LOG_PATH = None
        return None


def log_event(event: str, *, level: int = logging.INFO, **context: object) -> None:
    """Write only explicit safe fields; never format exceptions or project text."""
    if not _EVENT_RE.fullmatch(event):
        event = "event.invalid"
    safe = {}
    for key, value in context.items():
        if key not in {"operation", "operation_id", "status", "job_id", "phase", "mode", "code", "incident_id", "version", "language"}:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, str):
            valid = (
                key == "operation" and value in _OPERATIONS
                or key == "phase" and value in _PHASES
                or key == "language" and value in {"ru", "en"}
                or key == "status" and value in {"started", "succeeded", "failed", "cancelled", "cleanup_pending"}
                or key == "mode" and value in {"prod", "test"}
                or key == "code" and bool(_CODE_RE.fullmatch(value))
                or key == "incident_id" and bool(re.fullmatch(r"[0-9a-f]{32}", value))
                or key == "job_id" and bool(re.fullmatch(r"[0-9a-f]{32}", value))
                or key == "operation_id" and bool(re.fullmatch(r"[0-9a-f]{32}", value))
                or key == "version" and bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", value))
            )
            if valid:
                safe[key] = value
    line = json.dumps({"event": event, **safe}, ensure_ascii=True, separators=(",", ":"))
    try:
        if _LOG_PATH is None:
            if len(_BUFFER) == 256:
                _BUFFER.pop(0)
            _BUFFER.append(line)
            return
        _LOGGER.log(level, "%s", line)
    except (OSError, ValueError):
        # A diagnostic sink must never change the outcome of the operation.
        pass


def log_exception(event: str, error: BaseException, **context: object) -> str:
    """Write a sanitized traceback and return its opaque incident identifier."""
    incident_id = uuid4().hex
    log_event(event, level=logging.ERROR, incident_id=incident_id, **context)
    frames = traceback.extract_tb(error.__traceback__)
    safe_frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in frames]
    detail = {"incident_id": incident_id, "exception_type": type(error).__name__, "frames": safe_frames}
    try:
        if _LOG_PATH is not None:
            _LOGGER.error("%s", json.dumps(detail, ensure_ascii=True, separators=(",", ":")))
        elif len(_BUFFER) < 256:
            _BUFFER.append(json.dumps(detail, ensure_ascii=True, separators=(",", ":")))
    except (OSError, ValueError):
        pass
    return incident_id


def log_folder() -> Path | None:
    if _LOG_DIRECTORY is not None:
        return _LOG_DIRECTORY
    try:
        return _directory()
    except OSError:
        return None


def session_log() -> Path | None:
    return _LOG_PATH


def _handler_snapshot(path: Path) -> tuple[tuple[str, bytes], ...]:
    handler = next((item for item in _LOGGER.handlers if getattr(item, "baseFilename", None) == str(path.resolve())), None)
    if handler is None:
        return _read_log_parts(path)
    handler.acquire()
    try:
        return _read_log_parts(path)
    finally:
        handler.release()


def _read_log_parts(path: Path) -> tuple[tuple[str, bytes], ...]:
    parts = []
    for index in range(_MAX_BACKUPS + 1):
        source = path if index == 0 else path.with_suffix(path.suffix + f".{index}")
        if source.is_file() and not source.is_symlink():
            name = "session.log" if index == 0 else f"session.log.{index}"
            parts.append((name, source.read_bytes()))
    return tuple(parts)


def create_report(target: Path, *, incident_id: str | None = None, code: str | None = None,
                  job_id: str | None = None, phase: str | None = None,
                  status: str | None = None, mode: str | None = None) -> Path:
    """Export bounded runtime data and this session's sanitized log, atomically."""
    entries = {}
    if incident_id is not None and re.fullmatch(r"[0-9a-f]{32}", incident_id):
        entries["incident_id"] = incident_id
    if code is not None and _CODE_RE.fullmatch(code):
        entries["code"] = code
    if job_id is not None and re.fullmatch(r"[0-9a-f]{32}", job_id):
        entries["job_id"] = job_id
    if phase is not None and phase in _PHASES:
        entries["phase"] = phase
    if status is not None and status in {"started", "succeeded", "failed", "cancelled", "cleanup_pending"}:
        entries["status"] = status
    if mode in {"prod", "test"}:
        entries["mode"] = mode
    runtime = {"version": APPLICATION_VERSION, "os": platform.platform(), "python": platform.python_version()}
    try:
        from PySide6 import __version__ as qt_version
        runtime["qt"] = qt_version
    except ImportError:
        runtime["qt"] = "unavailable"
    payload = json.dumps({"report_version": 1, "runtime": runtime, "incident": entries}, indent=2).encode("utf-8")
    if len(payload) > 64 * 1024:
        raise ValueError("support report metadata exceeds size limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("report.json", payload)
                path = _LOG_PATH
                if path is not None:
                    for name, data in _handler_snapshot(path):
                        archive.writestr(name, data)
                elif _BUFFER:
                    archive.writestr("session.log", ("\n".join(_BUFFER) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def install_exception_hooks() -> None:
    def uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log_exception("exception.uncaught", exc)
        sys.__excepthook__(exc_type, exc, tb)

    def thread_uncaught(args):
        log_exception("exception.thread", args.exc_value)

    sys.excepthook = uncaught
    import threading
    threading.excepthook = thread_uncaught


def shutdown() -> None:
    global _LOG_DIRECTORY, _LOG_PATH, _SESSION_LOCK
    if _LOG_DIRECTORY is None and _LOG_PATH is None and not _LOGGER.handlers and _SESSION_LOCK is None:
        return
    log_event("application.stop")
    for handler in tuple(_LOGGER.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            _LOGGER.removeHandler(handler)
    if _SESSION_LOCK is not None:
        _SESSION_LOCK.unlock()
    _LOG_DIRECTORY = None
    _LOG_PATH = None
    _SESSION_LOCK = None
