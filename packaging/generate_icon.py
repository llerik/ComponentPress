"""Generate the deterministic multi-size Windows icon from the app PNG."""

from __future__ import annotations

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QPainter


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "componentpress" / "app_assets" / "componentpress.png"
TARGET = Path(__file__).resolve().with_name("componentpress.ico")
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _render_square(source: QImage, size: int) -> QImage:
    scaled = source.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    storage = QBuffer()
    if not storage.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("Не удалось открыть буфер PNG")
    if not image.save(storage, "PNG"):
        raise RuntimeError("Qt не удалось закодировать размер иконки в PNG")
    return bytes(storage.data())


def build_icon(source_path: Path = SOURCE, target_path: Path = TARGET) -> None:
    source = QImage(str(source_path))
    if source.isNull():
        raise RuntimeError(f"Не удалось прочитать исходную иконку: {source_path}")
    images = [_png_bytes(_render_square(source, size)) for size in SIZES]
    header_size = 6 + 16 * len(images)
    offset = header_size
    entries = []
    for size, payload in zip(SIZES, images, strict=True):
        encoded_size = 0 if size == 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                encoded_size,
                encoded_size,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        offset += len(payload)
    target_path.write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(images)
    )


if __name__ == "__main__":
    build_icon()
