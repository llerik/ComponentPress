"""Validated, atomic PNG ZIP export for the selected copy mode."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import zipfile
from uuid import uuid4

from PySide6.QtGui import QFontDatabase

from componentpress.application.contracts import ProjectSnapshot
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.domain.copy_mode import validate_copy_mode
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cancellation import CancellationToken
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.tasks import RenderInstanceTask


@dataclass(frozen=True)
class PngArchiveResult:
    path: Path
    mode: str
    png_count: int
    copies: int


def export_png_archive(
    source: ProjectSnapshot,
    target: Path,
    *,
    mode: str = "prod",
    component_ids: tuple[str, ...] | None = None,
    cancellation: CancellationToken | None = None,
    snapshot_builder: InputSnapshotBuilder | None = None,
    on_progress=None,
) -> PngArchiveResult:
    selected_mode = validate_copy_mode(mode)
    if target.exists():
        raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target))
    if target.suffix.casefold() != ".zip":
        raise ProjectError(Diagnostic("ARCHIVE_EXTENSION", "для PNG-архива выберите файл .zip", target))
    token = cancellation or CancellationToken()
    store = JobStore(source.root)
    job_id = uuid4().hex
    job_directory = store.initialize(job_id, source.model.id)
    registered_font_ids: tuple[int, ...] = ()
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        store.update(job_id, operation="png_export", mode=selected_mode)
        target.parent.mkdir(parents=True, exist_ok=True)
        builder = snapshot_builder or InputSnapshotBuilder()
        snapshot = builder.create(
            source,
            job_id=job_id,
            job_directory=job_directory,
            component_ids=component_ids,
            mode=selected_mode,
            cancellation=token,
            defer_font_registration=True,
        )
        snapshot = builder.register_snapshot_fonts(snapshot)
        registered_font_ids = snapshot.registered_font_ids
        if on_progress is not None:
            on_progress(0, len(snapshot.instances), "Рендер PNG")
        for index, instance in enumerate(snapshot.instances, 1):
            token.check()
            errors: list[Exception] = []
            RenderInstanceTask(snapshot, instance, token, lambda _index, error: errors.append(error) if error else None).run()
            if errors:
                error = errors[0]
                if isinstance(error, ProjectError):
                    raise error
                raise ProjectError(Diagnostic("PNG_EXPORT_FAILED", str(error), instance.document_path)) from error
            if on_progress is not None:
                on_progress(index, len(snapshot.instances), "Рендер PNG")

        token.check()
        png_records = []
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, instance in enumerate(snapshot.instances, 1):
                token.check()
                source_png = snapshot.staging_directory / "images" / instance.png_name
                data = source_png.read_bytes()
                relative = f"images/{instance.png_name}"
                digest = hashlib.sha256(data).hexdigest()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
                png_records.append({
                    "component_id": instance.component_id,
                    "instance_id": instance.instance_id,
                    "row_number": instance.row_number,
                    "copies": instance.copies,
                    "path": relative,
                    "sha256": digest,
                })
                if on_progress is not None:
                    on_progress(index, len(snapshot.instances), "Упаковка ZIP")
            manifest = {
                "manifest_version": 1,
                "project_id": snapshot.project.id,
                "mode": selected_mode,
                "input_hashes": dict(sorted(snapshot.input_hashes.items())),
                "counts": {"png": len(snapshot.instances), "copies": snapshot.total_copies},
                "instances": png_records,
            }
            info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        token.check()
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_EXISTS", "ZIP уже существует; выберите другое имя", target)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("ARCHIVE_PUBLISH", f"не удалось атомарно опубликовать ZIP: {exc}", target)) from exc
        return PngArchiveResult(target, selected_mode, len(snapshot.instances), snapshot.total_copies)
    except ProjectError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProjectError(Diagnostic("ARCHIVE_WRITE", str(exc), target)) from exc
    finally:
        temporary.unlink(missing_ok=True)
        for font_id in reversed(registered_font_ids):
            QFontDatabase.removeApplicationFont(font_id)
        cleanup_job_directory(store, job_id)
        store.release(job_id)
