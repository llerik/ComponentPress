"""Build phases executed outside the GUI thread."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event, Lock
from typing import Callable

from PySide6.QtCore import QThreadPool

from componentpress.application.contracts import BuildInputSnapshot, BuildProgress, BuildResult
from componentpress.application.ports import Cancellation
from componentpress.i18n import tr
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.diagnostics.logging import log_event, log_exception
from componentpress.execution.cleanup import cleanup_job_directory
from componentpress.execution.job_store import JobStore
from componentpress.execution.tasks import RenderInstanceTask
from .imposition import calculate_imposition
from .pdf_writer import write_print_pdf
from .report import sha256, write_report


def _diagnostic_text(diagnostic: Diagnostic) -> str:
    from componentpress.i18n.diagnostics import format_diagnostic

    return format_diagnostic(diagnostic)


class BuildEngine:
    def __init__(self, store: JobStore):
        self.store = store

    def execute(
        self,
        snapshot: BuildInputSnapshot,
        cancellation: Cancellation,
        progress: Callable[[BuildProgress], None],
    ) -> BuildResult:
        started = datetime.now(timezone.utc).isoformat()
        output: Path | None = None
        log_event("build.started", operation="build", job_id=snapshot.job_id, phase="preparing", mode=snapshot.mode)
        try:
            cancellation.check()
            plan = calculate_imposition(snapshot.instances, snapshot.print_settings)
            self.store.update(snapshot.job_id, phase="rendering")
            self._render(snapshot, cancellation, progress)
            cancellation.check()
            self.store.update(snapshot.job_id, phase="assembling_pdf")
            write_print_pdf(
                snapshot,
                plan,
                cancellation,
                lambda done, total, message: progress(BuildProgress(snapshot.job_id, "assembling_pdf", done, total, tr("ui.pdf_page_progress", page=done, total=total))),
            )
            cancellation.check()
            output_hashes = {
                f"images/{item.png_name}": sha256(snapshot.staging_directory / "images" / item.png_name)
                for item in snapshot.instances
            }
            output_hashes["print.pdf"] = sha256(snapshot.staging_directory / "print.pdf")
            write_report(snapshot, plan, output_hashes, started_at=started)
            cancellation.check()
            self.store.update(snapshot.job_id, phase="publishing")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output = snapshot.root / "output" / f"build-{timestamp}-{snapshot.job_id[:8]}"
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise ProjectError(Diagnostic("BUILD_OUTPUT_EXISTS", "папка результата уже существует", output))
            relative_output = output.relative_to(snapshot.root).as_posix()
            # Persist the intended target before the atomic rename so recovery can
            # recognize a crash immediately after publication.
            self.store.update(snapshot.job_id, output_directory=relative_output)
            os.replace(snapshot.staging_directory, output)
            statistics = {
                "instances": len(snapshot.instances),
                "copies": snapshot.total_copies,
                "pages": len(plan.pages),
            }
            # os.replace is the publication boundary.  A complete, verified
            # output must never be reported as failed merely because status,
            # history, or temporary-file bookkeeping failed afterwards.
            post_publish: list[Diagnostic] = []
            try:
                self.store.update(snapshot.job_id, phase="succeeded", cleanup_state="running")
            except Exception as exc:
                post_publish.append(self._bookkeeping_warning("не удалось записать итоговый статус", exc, snapshot.job_directory))
            try:
                self.store.save_history(snapshot.job_id, {
                    "job_id": snapshot.job_id,
                    "status": "succeeded",
                    "output_directory": relative_output,
                    "statistics": statistics,
                    "mode": snapshot.mode,
                })
                history_written = True
            except Exception as exc:
                history_written = False
                post_publish.append(self._bookkeeping_warning("не удалось записать историю", exc, snapshot.job_directory))
            if history_written:
                try:
                    cleanup = cleanup_job_directory(self.store, snapshot.job_id)
                except Exception as exc:
                    cleanup = (self._bookkeeping_warning("не удалось выполнить очистку", exc, snapshot.job_directory, code="CLEANUP_PENDING"),)
            else:
                cleanup = (Diagnostic(
                    "CLEANUP_PENDING",
                    "каталог задания сохранён для повторной записи истории и очистки",
                    snapshot.job_directory,
                    severity="warning",
                ),)
                try:
                    self.store.update(snapshot.job_id, cleanup_state="cleanup_pending")
                except Exception as exc:
                    post_publish.append(self._bookkeeping_warning("не удалось записать состояние очистки", exc, snapshot.job_directory))
            log_event("build.finished", operation="build", status="succeeded", job_id=snapshot.job_id, phase="finished", mode=snapshot.mode)
            return BuildResult(
                snapshot.job_id,
                "succeeded",
                output,
                output / "print.pdf",
                output / "build-report.json",
                (*post_publish, *cleanup),
                "cleanup_pending" if cleanup else "done",
                statistics,
                snapshot.mode,
            )
        except Exception as exc:
            if isinstance(exc, ProjectError):
                diagnostic = exc.diagnostic
                event = "build.cancelled" if diagnostic.code == "BUILD_CANCELLED" else "build.failed"
                log_event(event, operation="build", job_id=snapshot.job_id, phase=diagnostic.code, mode=snapshot.mode, code=diagnostic.code)
            else:
                incident_id = log_exception("build.failed", exc, operation="build", job_id=snapshot.job_id, phase="failed", mode=snapshot.mode)
                diagnostic = Diagnostic("BUILD_FAILED", "Unexpected build error", incident_id=incident_id, message_key="error.build_failed")
            status = "cancelled" if diagnostic.code == "BUILD_CANCELLED" else "failed"
            try:
                self.store.update(snapshot.job_id, phase=status, cleanup_state="running")
                self.store.save_history(snapshot.job_id, {
                    "job_id": snapshot.job_id,
                    "status": status,
                    "diagnostics": [_diagnostic_text(diagnostic)],
                })
            except ProjectError:
                pass
            cleanup = cleanup_job_directory(self.store, snapshot.job_id)
            return BuildResult(
                snapshot.job_id,
                status,
                diagnostics=(diagnostic, *cleanup),
                cleanup_state="cleanup_pending" if cleanup else "done",
                mode=snapshot.mode,
            )

    @staticmethod
    def _bookkeeping_warning(message: str, exc: Exception, path: Path, *, code: str = "BUILD_BOOKKEEPING_FAILED") -> Diagnostic:
        if not isinstance(exc, ProjectError):
            log_exception("build.bookkeeping", exc, operation="build", phase=code, code=code)
        detail = exc.diagnostic.message if isinstance(exc, ProjectError) else "технические подробности в журнале"
        return Diagnostic(code, f"{message}: {detail}", path, severity="warning")

    @staticmethod
    def _render(snapshot: BuildInputSnapshot, cancellation: Cancellation, progress) -> None:
        pool = QThreadPool()
        pool.setMaxThreadCount(snapshot.max_render_workers)
        completed = 0
        errors: list[Exception] = []
        lock = Lock()
        all_done = Event()
        total = len(snapshot.instances)

        def done(_index: int, error: Exception | None) -> None:
            nonlocal completed
            with lock:
                completed += 1
                if error is not None:
                    errors.append(error)
                    cancellation.cancel()
                try:
                    progress(BuildProgress(snapshot.job_id, "rendering", completed, total, tr("ui.png_build_progress", completed=completed, total=total)))
                except Exception as exc:
                    log_exception("build.progress", exc, operation="build", job_id=snapshot.job_id, phase="rendering", mode=snapshot.mode)
                finally:
                    if completed == total:
                        all_done.set()

        for instance in snapshot.instances:
            pool.start(RenderInstanceTask(snapshot, instance, cancellation, done))
        all_done.wait()
        pool.waitForDone()
        if errors:
            first = errors[0]
            if isinstance(first, ProjectError):
                raise first
            raise ProjectError(Diagnostic("RENDER_FAILED", str(first), snapshot.job_directory))
        cancellation.check()
