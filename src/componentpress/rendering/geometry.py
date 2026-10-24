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
    _validate_image_fit(source_width, source_height, width_mm, height_mm)
    if fit == "stretch":
        return _stretch_geometry(source_width, source_height, x_mm, y_mm, width_mm, height_mm)
    if fit == "contain":
        return _contain_geometry(source_width, source_height, x_mm, y_mm, width_mm, height_mm)
    if fit == "cover":
        return _cover_geometry(source_width, source_height, x_mm, y_mm, width_mm, height_mm)
    raise ValueError(f"unsupported image fit mode: {fit}")


def _validate_image_fit(source_width: int, source_height: int, width_mm: float, height_mm: float) -> None:
    if min(source_width, source_height, width_mm, height_mm) <= 0:
        raise ValueError("image and target dimensions must be positive")


def _stretch_geometry(source_width: int, source_height: int, x_mm: float, y_mm: float,
                      width_mm: float, height_mm: float) -> tuple[float, ...]:
    return x_mm, y_mm, width_mm, height_mm, 0.0, 0.0, float(source_width), float(source_height)


def _contain_geometry(source_width: int, source_height: int, x_mm: float, y_mm: float,
                      width_mm: float, height_mm: float) -> tuple[float, ...]:
    source_ratio = source_width / source_height
    target_ratio = width_mm / height_mm
    fitted_width, fitted_height = _contained_size(source_ratio, target_ratio, width_mm, height_mm)
    return (x_mm + (width_mm - fitted_width) / 2, y_mm + (height_mm - fitted_height) / 2,
            fitted_width, fitted_height, 0.0, 0.0, float(source_width), float(source_height))


def _contained_size(source_ratio: float, target_ratio: float, width_mm: float,
                    height_mm: float) -> tuple[float, float]:
    return (width_mm, width_mm / source_ratio) if source_ratio > target_ratio else (height_mm * source_ratio, height_mm)


def _cover_geometry(source_width: int, source_height: int, x_mm: float, y_mm: float,
                    width_mm: float, height_mm: float) -> tuple[float, ...]:
    source_rect = _center_crop(source_width, source_height, width_mm / height_mm)
    return x_mm, y_mm, width_mm, height_mm, *source_rect


def _center_crop(source_width: int, source_height: int, target_ratio: float) -> tuple[float, ...]:
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_width = source_height * target_ratio
        return (source_width - crop_width) / 2, 0.0, crop_width, float(source_height)
    crop_height = source_width / target_ratio
    return 0.0, (source_height - crop_height) / 2, float(source_width), crop_height


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
