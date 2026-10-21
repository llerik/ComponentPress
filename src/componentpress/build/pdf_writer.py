"""Sequential printable PDF assembly with vector cut rectangles."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLineF, QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from componentpress.application.contracts import BuildInputSnapshot
from componentpress.application.ports import Cancellation
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.i18n import tr
from .imposition import ImpositionPlan, cut_grid_lines


def write_print_pdf(
    snapshot: BuildInputSnapshot,
    plan: ImpositionPlan,
    cancellation: Cancellation,
    progress=None,
) -> Path:
    target = snapshot.staging_directory / "print.pdf"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        writer = _create_writer(temporary, snapshot, plan)
        painter = QPainter(writer)
        if not painter.isActive():
            raise ProjectError(Diagnostic("PDF_WRITE", "Qt не смог начать запись PDF", target))
        scale = snapshot.print_settings.dpi / 25.4
        pen = QPen(QColor("#000000"))
        pen.setWidthF(snapshot.print_settings.cut_line_width_mm * scale)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
        try:
            for page_number, page in enumerate(plan.pages):
                cancellation.check()
                component_names = _page_component_names(snapshot, page)
                _report_page_progress(progress, page_number, len(plan.pages), component_names)
                if page_number and not writer.newPage():
                    raise ProjectError(Diagnostic("PDF_WRITE", "Qt не смог создать страницу PDF", target))
                for placement in page.placements:
                    cancellation.check()
                    instance = snapshot.instances[placement.instance_index]
                    image_path = snapshot.staging_directory / "images" / instance.png_name
                    image = QImage(str(image_path))
                    if image.isNull():
                        raise ProjectError(Diagnostic("PNG_READ", "не удалось прочитать PNG экземпляра", image_path))
                    rect = QRectF(
                        placement.x_mm * scale,
                        placement.y_mm * scale,
                        placement.width_mm * scale,
                        placement.height_mm * scale,
                    )
                    painter.drawImage(rect, image)
                if snapshot.print_settings.cut_lines and page.placements:
                    vertical, horizontal = cut_grid_lines(page, snapshot.print_settings.gap_mm)
                    painter.save()
                    painter.setPen(pen)
                    for x in vertical:
                        painter.drawLine(QLineF(x * scale, 0, x * scale, plan.paper_height_mm * scale))
                    for y in horizontal:
                        painter.drawLine(QLineF(0, y * scale, plan.paper_width_mm * scale, y * scale))
                    painter.restore()
                _report_page_progress(progress, page_number + 1, len(plan.pages), component_names)
        finally:
            painter.end()
        cancellation.check()
        os.replace(temporary, target)
        return target
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_WRITE", str(exc), target)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _page_component_names(snapshot, page) -> tuple[str, ...]:
    return tuple(sorted({snapshot.instances[item.instance_index].component_name for item in page.placements}))


def _create_writer(temporary: Path, snapshot: BuildInputSnapshot, plan: ImpositionPlan) -> QPdfWriter:
    writer = QPdfWriter(str(temporary))
    writer.setResolution(snapshot.print_settings.dpi)
    writer.setPageSize(QPageSize(QSizeF(plan.paper_width_mm, plan.paper_height_mm), QPageSize.Unit.Millimeter,
                                 snapshot.print_settings.paper, QPageSize.SizeMatchPolicy.ExactMatch))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    return writer


def _report_page_progress(progress, completed: int, total: int, component_names: tuple[str, ...]) -> None:
    if progress is not None:
        progress(completed, total, tr("ui.pdf_page_progress", page=max(1, completed), total=total), component_names)
