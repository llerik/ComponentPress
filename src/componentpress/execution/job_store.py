"""Owned build directories, atomic metadata and conservative recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
from typing import Any

from PySide6.QtCore import QLockFile

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.files import atomic_write


class JobStore:
    METADATA_VERSION = 1

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state_root = self.root / ".componentpress"
        self.jobs_root = self.state_root / "jobs"
        self.locks_root = self.state_root / "job-locks"
        self.history_root = self.state_root / "build-history"
        self._locks: dict[str, QLockFile] = {}

    def job_directory(self, job_id: str) -> Path:
        if not job_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in job_id):
            raise ProjectError(Diagnostic("JOB_ID_INVALID", "недопустимый ID задания", self.jobs_root))
        return self.jobs_root / job_id

    def initialize(self, job_id: str, project_id: str) -> Path:
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.locks_root.mkdir(parents=True, exist_ok=True)
        directory = self.job_directory(job_id)
        if directory.exists():
            raise ProjectError(Diagnostic("JOB_EXISTS", "каталог задания уже существует", directory))
        lock = QLockFile(str(self.locks_root / f"{job_id}.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ProjectError(Diagnostic("JOB_LOCKED", "задание занято другим владельцем", directory))
        self._locks[job_id] = lock
        directory.mkdir(parents=True)
        self.write(job_id, {
            "metadata_version": self.METADATA_VERSION,
            "job_id": job_id,
            "project_id": project_id,
            "session_id": f"{socket.gethostname()}-{os.getpid()}",
            "owner": {"pid": os.getpid(), "host": socket.gethostname()},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "phase": "preparing",
            "cleanup_state": "pending",
            "output_directory": None,
        })
        return directory

    def read(self, job_id: str) -> dict[str, Any]:
        path = self.job_directory(job_id) / "job.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(Diagnostic("JOB_METADATA_INVALID", "повреждены метаданные задания", path)) from exc
        if value.get("metadata_version") != self.METADATA_VERSION or value.get("job_id") != job_id:
            raise ProjectError(Diagnostic("JOB_METADATA_INVALID", "неподдерживаемые метаданные задания", path))
        return value

    def write(self, job_id: str, metadata: dict[str, Any]) -> None:
        target = self.job_directory(job_id) / "job.json"
        atomic_write(target, (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def update(self, job_id: str, **changes: Any) -> None:
        metadata = self.read(job_id)
        metadata.update(changes)
        self.write(job_id, metadata)

    def save_history(self, job_id: str, payload: dict[str, Any]) -> None:
        self.history_root.mkdir(parents=True, exist_ok=True)
        target = self.history_root / f"{job_id}.json"
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(data) > 1024 * 1024:
            payload = {
                "job_id": job_id,
                "status": payload.get("status"),
                "diagnostic_count": len(payload.get("diagnostics", [])),
                "truncated": True,
            }
            data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write(target, data)
        entries = sorted(self.history_root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for old in entries[20:]:
            try:
                old.unlink()
            except OSError:
                pass

    def release(self, job_id: str) -> None:
        lock = self._locks.pop(job_id, None)
        if lock is not None:
            lock.unlock()

    @staticmethod
    def verify_published(path: Path, job_id: str) -> bool:
        report_path = path / "build-report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("job_id") != job_id or report.get("complete") is not True:
                return False
            for relative, expected in report.get("output_hashes", {}).items():
                file_path = path / Path(relative)
                digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                if digest != expected:
                    return False
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            return False

    def recover(self) -> tuple[Diagnostic, ...]:
        """Clean only valid, exclusively owned old jobs; unknown data is preserved."""
        from .cleanup import cleanup_job_directory

        diagnostics: list[Diagnostic] = []
        if not self.jobs_root.exists():
            return ()
        for directory in sorted(self.jobs_root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            job_id = directory.name
            try:
                metadata = self.read(job_id)
            except ProjectError as exc:
                diagnostics.append(exc.diagnostic)
                continue
            lock = QLockFile(str(self.locks_root / f"{job_id}.lock"))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                diagnostics.append(Diagnostic("JOB_OWNER_UNKNOWN", "каталог задания занят или владелец неоднозначен", directory, severity="warning"))
                continue
            try:
                output_value = metadata.get("output_directory")
                output = (self.root / Path(output_value)).resolve() if isinstance(output_value, str) else None
                published = bool(output and output.is_relative_to((self.root / "output").resolve()) and self.verify_published(output, job_id))
                status = "succeeded" if published else "interrupted"
                try:
                    self.save_history(job_id, {
                        "job_id": job_id,
                        "status": status,
                        "output_directory": output_value if published else None,
                        "diagnostics": [] if published else ["BUILD_INTERRUPTED"],
                    })
                except Exception as exc:
                    message = exc.diagnostic.message if isinstance(exc, ProjectError) else str(exc)
                    diagnostics.append(Diagnostic(
                        "JOB_HISTORY_FAILED",
                        f"не удалось сохранить историю восстановления; каталог задания оставлен для повтора: {message}",
                        directory,
                        severity="warning",
                    ))
                    try:
                        self.update(job_id, cleanup_state="cleanup_pending")
                    except ProjectError:
                        pass
                    continue
                cleanup = cleanup_job_directory(self, job_id)
                diagnostics.extend(cleanup)
                cleanup_pending = bool(cleanup)
                diagnostics.append(Diagnostic(
                    "BUILD_RECOVERED" if published else "BUILD_INTERRUPTED",
                    (
                        "опубликованный результат сохранён; очистка временных остатков ожидает повтора"
                        if published and cleanup_pending else
                        "опубликованный результат сохранён, временные остатки очищены"
                        if published else
                        "прерванное задание сохранено для повторной очистки без автоматического запуска"
                        if cleanup_pending else
                        "прерванное задание очищено без автоматического повтора"
                    ),
                    directory,
                    severity="warning",
                ))
            finally:
                lock.unlock()
        return tuple(diagnostics)
