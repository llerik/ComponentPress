"""Sequential printable PDF assembly with vector cut rectangles."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QColor, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cancellation import CancellationToken
from .imposition import ImpositionPlan
from .input_snapshot import BuildInputSnapshot


def write_print_pdf(
    snapshot: BuildInputSnapshot,
    plan: ImpositionPlan,
    cancellation: CancellationToken,
    progress=None,
) -> Path:
    target = snapshot.staging_directory / "print.pdf"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        writer = QPdfWriter(str(temporary))
        writer.setResolution(snapshot.print_settings.dpi)
        writer.setPageSize(QPageSize(
            QSizeF(plan.paper_width_mm, plan.paper_height_mm),
            QPageSize.Unit.Millimeter,
            snapshot.print_settings.paper,
            QPageSize.SizeMatchPolicy.ExactMatch,
        ))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
        painter = QPainter(writer)
        if not painter.isActive():
            raise ProjectError(Diagnostic("PDF_WRITE", "Qt не смог начать запись PDF", target))
        scale = snapshot.print_settings.dpi / 25.4
        pen = QPen(QColor("#000000"))
        pen.setWidthF(snapshot.print_settings.cut_line_width_mm * scale)
        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
        try:
            for page_number, page in enumerate(plan.pages):
                cancellation.check()
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
                    if snapshot.print_settings.cut_lines:
                        painter.save()
                        painter.setPen(pen)
                        painter.setBrush(QColor(0, 0, 0, 0))
                        painter.drawRect(rect)
                        painter.restore()
                if progress is not None:
                    progress(page_number + 1, len(plan.pages), f"Страница {page_number + 1} из {len(plan.pages)}")
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
