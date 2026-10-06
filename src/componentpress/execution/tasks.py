from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QRunnable
from PySide6.QtGui import QImage, QPainter

from componentpress.build.input_snapshot import BuildInputSnapshot, BuildInstance, SnapshotResourceLoader
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.rendering.geometry import MM_PER_INCH, pixel_size
from componentpress.rendering.painter import paint_component
from .cancellation import CancellationToken


class RenderInstanceTask(QRunnable):
    def __init__(self, snapshot: BuildInputSnapshot, instance: BuildInstance, cancellation: CancellationToken, done):
        super().__init__()
        self.snapshot = snapshot
        self.instance = instance
        self.cancellation = cancellation
        self.done = done

    def run(self) -> None:
        error = None
        try:
            self.cancellation.check()
            dpi = self.snapshot.print_settings.dpi
            size = pixel_size(self.instance.component.size_mm.width, self.instance.component.size_mm.height, dpi)
            image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            dpm = round(dpi / 0.0254)
            image.setDotsPerMeterX(dpm)
            image.setDotsPerMeterY(dpm)
            image.fill(0)
            loader = SnapshotResourceLoader(self.snapshot.resources)
            painter = QPainter(image)
            if not painter.isActive():
                raise ProjectError(Diagnostic("RENDER_INIT", "не удалось создать растровый холст", self.instance.document_path))
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.scale(dpi / MM_PER_INCH, dpi / MM_PER_INCH)
                paint_component(
                    painter,
                    self.instance.component,
                    loader,
                    self.instance.document_path,
                    dict(self.snapshot.project.variables),
                    bindings_resolved=True,
                )
            finally:
                painter.end()
            self.cancellation.check()
            target = self.snapshot.staging_directory / "images" / self.instance.png_name
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                if not image.save(str(temporary), "PNG"):
                    raise ProjectError(Diagnostic("PNG_WRITE", "Qt не смог сохранить PNG", target))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:  # carried to the coordinator; QRunnable must not leak it
            error = exc
        self.done(self.instance.index, error)
