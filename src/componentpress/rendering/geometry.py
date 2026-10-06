"""Physical-unit conversions; rounding happens only at export boundaries."""

from PySide6.QtCore import QSize


MM_PER_INCH = 25.4
HTML_DPI = 96.0


def mm_to_pixels(value_mm: float, dpi: float) -> float:
    return value_mm * dpi / MM_PER_INCH


def pixel_size(width_mm: float, height_mm: float, dpi: int) -> QSize:
    return QSize(round(mm_to_pixels(width_mm, dpi)), round(mm_to_pixels(height_mm, dpi)))


def mm_to_html_pixels(value_mm: float) -> float:
    return mm_to_pixels(value_mm, HTML_DPI)
