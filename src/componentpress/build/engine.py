"""Build artifact preparation, publication, and result bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Callable

from componentpress.application.contracts import BuildInputSnapshot, BuildProgress, BuildResult
from componentpress.application.ports import Cancellation
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.i18n import tr
from .imposition import calculate_imposition
from .pdf_writer import write_print_pdf
from .report import sha256, write_report


class BuildEngine:
    def __init__(self, store: JobStore, render_instances: Callable):
        self.store = store
        self.render_instances = render_instances

    def execute(self, snapshot: BuildInputSnapshot, cancellation: Cancellation, progress: Callable[[BuildProgress], None]) -> BuildResult:
        started = datetime.now(timezone.utc).isoformat()
        log_event("build.started", operation="build", job_id=snapshot.job_id, phase="preparing", mode=snapshot.mode)
        try:
            plan = self._prepare_artifacts(snapshot, cancellation, progress, started)
            output = self._publish(snapshot, plan, cancellation)
        except Exception as error:
            return self._finish_failure(snapshot, error)
        return self._finish_success(snapshot, output, plan)

    def _prepare_artifacts(self, snapshot, cancellation, progress, started):
        cancellation.check()
        plan = calculate_imposition(snapshot.instances, snapshot.print_settings)
        self.store.update(snapshot.job_id, phase="rendering")
        self.render_instances(snapshot, cancellation, progress)
        cancellation.check()
        self.store.update(snapshot.job_id, phase="assembling_pdf")
        self._write_pdf(snapshot, plan, cancellation, progress)
        cancellation.check()
        hashes = self._output_hashes(snapshot)
        write_report(snapshot, plan, hashes, started_at=started)
        cancellation.check()
        return plan

    @staticmethod
    def _write_pdf(snapshot, plan, cancellation, progress):
        page_progress = lambda done, total, _message: progress(BuildProgress(
            snapshot.job_id, "assembling_pdf", done, total,
            tr("ui.pdf_page_progress", page=done, total=total),
        ))
        write_print_pdf(snapshot, plan, cancellation, page_progress)

    @staticmethod
    def _output_hashes(snapshot):
        hashes = {f"images/{item.png_name}": sha256(snapshot.staging_directory / "images" / item.png_name) for item in snapshot.instances}
        hashes["print.pdf"] = sha256(snapshot.staging_directory / "print.pdf")
        return hashes

    def _publish(self, snapshot, plan, cancellation):
        self.store.update(snapshot.job_id, phase="publishing")
        output = self._output_path(snapshot)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ProjectError(Diagnostic("BUILD_OUTPUT_EXISTS", "папка результата уже существует", output))
        relative = output.relative_to(snapshot.root).as_posix()
        self.store.update(snapshot.job_id, output_directory=relative)
        cancellation.check()
        os.replace(snapshot.staging_directory, output)
        return output

    @staticmethod
    def _output_path(snapshot):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return snapshot.root / "output" / f"build-{timestamp}-{snapshot.job_id[:8]}"

    def _finish_success(self, snapshot, output, plan):
        statistics = {"instances": len(snapshot.instances), "copies": snapshot.total_copies, "pages": len(plan.pages)}
        warnings, history_written = self._save_success(snapshot, output, statistics)
        cleanup = self._cleanup_published(snapshot, history_written)
        log_event("build.finished", operation="build", status="succeeded", job_id=snapshot.job_id, phase="finished", mode=snapshot.mode)
        return BuildResult(snapshot.job_id, "succeeded", output, output / "print.pdf", output / "build-report.json",
                           (*warnings, *cleanup), "cleanup_pending" if cleanup else "done", statistics, snapshot.mode)

    def _save_success(self, snapshot, output, statistics):
        warnings = []
        try:
            self.store.update(snapshot.job_id, phase="succeeded", cleanup_state="running")
        except Exception as error:
            warnings.append(self._bookkeeping_warning("не удалось записать итоговый статус", error, snapshot.job_directory))
        try:
            self.store.save_history(snapshot.job_id, self._history_payload(snapshot, output, statistics))
            return warnings, True
        except Exception as error:
            warnings.append(self._bookkeeping_warning("не удалось записать историю", error, snapshot.job_directory))
            return warnings, False

    @staticmethod
    def _history_payload(snapshot, output, statistics):
        return {"job_id": snapshot.job_id, "status": "succeeded", "output_directory": output.relative_to(snapshot.root).as_posix(),
                "statistics": statistics, "mode": snapshot.mode}

    def _cleanup_published(self, snapshot, history_written):
        if history_written:
            return self._cleanup(snapshot)
        pending = Diagnostic("CLEANUP_PENDING", "каталог задания сохранён для повторной записи истории и очистки",
                             snapshot.job_directory, severity="warning")
        try:
            self.store.update(snapshot.job_id, cleanup_state="cleanup_pending")
        except Exception as error:
            return (pending, self._bookkeeping_warning("не удалось записать состояние очистки", error, snapshot.job_directory))
        return (pending,)

    def _finish_failure(self, snapshot, error):
        diagnostic = self._failure_diagnostic(snapshot, error)
        status = "cancelled" if diagnostic.code == "BUILD_CANCELLED" else "failed"
        bookkeeping = self._record_failure(snapshot, status, diagnostic)
        cleanup = self._cleanup(snapshot)
        return BuildResult(snapshot.job_id, status, diagnostics=(diagnostic, *bookkeeping, *cleanup),
                           cleanup_state="cleanup_pending" if cleanup else "done", mode=snapshot.mode)

    def _record_failure(self, snapshot, status, diagnostic):
        warnings = []
        try:
            self.store.update(snapshot.job_id, phase=status, cleanup_state="running")
        except Exception as error:
            log_exception("build.failure_status", error, operation="build", job_id=snapshot.job_id, phase=status)
            warnings.append(self._bookkeeping_warning("не удалось записать итоговый статус", error, snapshot.job_directory))
        try:
            self.store.save_history(snapshot.job_id, {"job_id": snapshot.job_id, "status": status,
                                                      "diagnostics": [self._diagnostic_text(diagnostic)]})
        except Exception as error:
            log_exception("build.failure_history", error, operation="build", job_id=snapshot.job_id, phase=status)
            warnings.append(self._bookkeeping_warning("не удалось записать историю", error, snapshot.job_directory))
        return tuple(warnings)

    def _cleanup(self, snapshot):
        try:
            return cleanup_job_directory(self.store, snapshot.job_id)
        except Exception as error:
            return (self._bookkeeping_warning("не удалось выполнить очистку", error, snapshot.job_directory, code="CLEANUP_PENDING"),)

    @staticmethod
    def _failure_diagnostic(snapshot, error):
        if isinstance(error, ProjectError):
            status = "cancelled" if error.diagnostic.code == "BUILD_CANCELLED" else "failed"
            log_event(f"build.{status}", operation="build", job_id=snapshot.job_id, phase=error.diagnostic.code,
                      mode=snapshot.mode, code=error.diagnostic.code)
            return error.diagnostic
        incident = log_exception("build.failed", error, operation="build", job_id=snapshot.job_id,
                                 phase="failed", mode=snapshot.mode)
        return Diagnostic("BUILD_FAILED", "Unexpected build error", incident_id=incident, message_key="error.build_failed")

    @staticmethod
    def _diagnostic_text(diagnostic):
        from componentpress.i18n.diagnostics import format_diagnostic

        return format_diagnostic(diagnostic)

    @staticmethod
    def _bookkeeping_warning(message, error, path, *, code="BUILD_BOOKKEEPING_FAILED"):
        if not isinstance(error, ProjectError):
            log_exception("build.bookkeeping", error, operation="build", phase=code, code=code)
        detail = error.diagnostic.message if isinstance(error, ProjectError) else "технические подробности в журнале"
        return Diagnostic(code, f"{message}: {detail}", path, severity="warning")
