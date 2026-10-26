"""Viewport rulers drawn in the editor's millimetre coordinate system."""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget


def _tick_step(pixels_per_mm: float) -> float:
    raw = 58 / max(pixels_per_mm, 1e-9)
    scale = 10 ** math.floor(math.log10(max(raw, 1e-9)))
    return next((scale * value for value in (1, 2, 5, 10) if scale * value >= raw), scale * 10)


class CanvasRuler(QWidget):
    def __init__(self, canvas, horizontal: bool):
        super().__init__(canvas)
        self.canvas = canvas
        self.horizontal = horizontal
        self.cursor_mm: float | None = None
        self.setMouseTracking(True)

    def set_cursor(self, value: float | None) -> None:
        self.cursor_mm = value
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().button())
        painter.setPen(self.palette().buttonText().color())
        self._draw_ticks(painter)
        if self.cursor_mm is not None:
            self._draw_cursor(painter)
        painter.end()

    def _draw_ticks(self, painter: QPainter) -> None:
        horizontal = self.horizontal
        axis = 0 if horizontal else 1
        extent = self.width() if horizontal else self.height()
        scale = self.canvas.transform().m11() * self.canvas._world_per_mm_x
        if not horizontal:
            scale = self.canvas.transform().m22() * self.canvas._world_per_mm_y
        first = self._coordinate(0 if horizontal else 1, 0)
        last = self._coordinate(0 if horizontal else 1, extent)
        step = _tick_step(abs(scale))
        for tick in range(math.ceil(min(first, last) / step), math.floor(max(first, last) / step) + 1):
            self._draw_tick(painter, tick * step, self._pixel(tick * step), extent, step)

    def _coordinate(self, axis: int, pixel: int) -> float:
        point = self.canvas.mapToScene(QPoint(pixel if axis == 0 else 0, pixel if axis == 1 else 0))
        return point.x() / self.canvas._world_per_mm_x if axis == 0 else point.y() / self.canvas._world_per_mm_y

    def _pixel(self, millimetres: float) -> float:
        world = self.canvas._world_per_mm_x if self.horizontal else self.canvas._world_per_mm_y
        point = self.canvas.viewportTransform().map(QPointF(millimetres * world, millimetres * world))
        return point.x() if self.horizontal else point.y()

    def _draw_tick(self, painter, value, pixel, extent, step) -> None:
        major = round(value / step) % 5 == 0
        length = 8 if major else 4
        if self.horizontal:
            painter.drawLine(round(pixel), self.height(), round(pixel), self.height() - length)
            if major:
                painter.drawText(round(pixel) + 2, self.height() - 9, f"{value:g}")
        else:
            painter.drawLine(self.width(), round(pixel), self.width() - length, round(pixel))
            if major:
                painter.save()
                painter.translate(self.width() - 8, round(pixel) - 2)
                painter.rotate(-90)
                painter.drawText(0, 0, f"{value:g}")
                painter.restore()

    def _draw_cursor(self, painter) -> None:
        pixel = round(self._pixel(self.cursor_mm))
        painter.setPen(QPen(self.palette().highlight().color(), 1))
        if self.horizontal:
            painter.drawLine(pixel, 0, pixel, self.height())
        else:
            painter.drawLine(0, pixel, self.width(), pixel)
