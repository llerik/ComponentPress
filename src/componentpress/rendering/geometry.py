"""Physical-unit conversions; rounding happens only at export boundaries."""

from PySide6.QtCore import QSize


MM_PER_INCH = 25.4
HTML_DPI = 96.0


def mm_to_pixels(value_mm: float, dpi: float) -> float:
    return value_mm * dpi / MM_PER_INCH


def pixel_size(width_mm: float, height_mm: float, dpi: int) -> QSize:
    return QSize(round(mm_to_pixels(width_mm, dpi)), round(mm_to_pixels(height_mm, dpi)))


def image_fit_geometry(
    source_width: int,
    source_height: int,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    fit: str,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return target and source rectangles for contain, cover, or stretch."""
    if source_width <= 0 or source_height <= 0 or width_mm <= 0 or height_mm <= 0:
        raise ValueError("image and target dimensions must be positive")
    source_ratio = source_width / source_height
    target_ratio = width_mm / height_mm
    if fit == "stretch":
        return x_mm, y_mm, width_mm, height_mm, 0.0, 0.0, float(source_width), float(source_height)
    if fit == "contain":
        if source_ratio > target_ratio:
            fitted_width, fitted_height = width_mm, width_mm / source_ratio
        else:
            fitted_width, fitted_height = height_mm * source_ratio, height_mm
        return (
            x_mm + (width_mm - fitted_width) / 2,
            y_mm + (height_mm - fitted_height) / 2,
            fitted_width,
            fitted_height,
            0.0,
            0.0,
            float(source_width),
            float(source_height),
        )
    if fit == "cover":
        if source_ratio > target_ratio:
            crop_width = source_height * target_ratio
            source_rect = ((source_width - crop_width) / 2, 0.0, crop_width, float(source_height))
        else:
            crop_height = source_width / target_ratio
            source_rect = (0.0, (source_height - crop_height) / 2, float(source_width), crop_height)
        return x_mm, y_mm, width_mm, height_mm, *source_rect
    raise ValueError(f"unsupported image fit mode: {fit}")


def effective_image_dpi(
    source_width: int,
    source_height: int,
    width_mm: float,
    height_mm: float,
    fit: str,
) -> tuple[float, float]:
    """Return effective horizontal and vertical image DPI at print size."""
    _x, _y, target_width, target_height, _sx, _sy, source_drawn_width, source_drawn_height = image_fit_geometry(
        source_width, source_height, 0.0, 0.0, width_mm, height_mm, fit
    )
    return (
        source_drawn_width * MM_PER_INCH / target_width,
        source_drawn_height * MM_PER_INCH / target_height,
    )


def mm_to_html_pixels(value_mm: float) -> float:
    return mm_to_pixels(value_mm, HTML_DPI)
