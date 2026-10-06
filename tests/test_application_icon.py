from __future__ import annotations

import struct
import subprocess
import sys
import tomllib
from pathlib import Path

from PySide6.QtGui import QColor, QIcon, QPixmap

from componentpress import gui
from componentpress.app_assets import load_application_icon


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def test_embedded_application_icon_renders_at_desktop_sizes(qapp) -> None:
    icon = load_application_icon()

    assert not icon.isNull()
    for size in EXPECTED_ICON_SIZES:
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull(), f"icon could not render at {size}x{size}"
        logical_size = pixmap.deviceIndependentSize()
        assert (logical_size.width(), logical_size.height()) == (size, size)


def test_gui_main_assigns_application_icon_without_starting_event_loop(qapp, monkeypatch) -> None:
    marker = QPixmap(31, 31)
    marker.fill(QColor("magenta"))
    expected_icon = QIcon(marker)
    created_windows: list[object] = []

    class WindowStub:
        def __init__(self, controller: object) -> None:
            created_windows.append(controller)

        def show(self) -> None:
            pass

    controller = object()
    monkeypatch.setattr(gui, "load_application_icon", lambda: expected_icon)
    monkeypatch.setattr(gui, "project_controller", lambda: controller)
    monkeypatch.setattr(gui, "MainWindow", WindowStub)

    assert gui.main([]) == 0
    assert created_windows == [controller]
    assert qapp.applicationName() == "ComponentPress"
    assert qapp.organizationName() == "ComponentPress"
    assert qapp.windowIcon().cacheKey() == expected_icon.cacheKey()


def test_windows_ico_has_valid_directory_offsets_and_expected_png_sizes() -> None:
    payload = (ROOT / "packaging" / "componentpress.ico").read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", payload)

    assert (reserved, image_type, count) == (0, 1, len(EXPECTED_ICON_SIZES))
    entries = []
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", payload, 6 + 16 * index)
        width_byte, height_byte, colors, reserved_byte, planes, bits, length, offset = entry
        width = 256 if width_byte == 0 else width_byte
        height = 256 if height_byte == 0 else height_byte
        entries.append((width, height, length, offset))
        assert colors == 0
        assert reserved_byte == 0
        assert planes == 1
        assert bits == 32

    assert tuple(width for width, _, _, _ in entries) == EXPECTED_ICON_SIZES
    assert all(width == height for width, height, _, _ in entries)

    expected_offset = 6 + 16 * count
    for width, height, length, offset in entries:
        assert offset == expected_offset
        assert length > 24
        image = payload[offset : offset + length]
        assert len(image) == length
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        png_width, png_height = struct.unpack_from(">II", image, 16)
        assert (png_width, png_height) == (width, height)
        expected_offset += length
    assert expected_offset == len(payload)


def test_windows_ico_is_reproducible_from_packaged_png(tmp_path: Path) -> None:
    generated = tmp_path / "componentpress.ico"
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy,sys; from pathlib import Path; "
                "build_icon=runpy.run_path(sys.argv[1])['build_icon']; "
                "build_icon(Path(sys.argv[2]), Path(sys.argv[3]))"
            ),
            str(ROOT / "packaging" / "generate_icon.py"),
            str(ROOT / "src" / "componentpress" / "app_assets" / "componentpress.png"),
            str(generated),
        ],
        check=True,
    )

    assert generated.read_bytes() == (ROOT / "packaging" / "componentpress.ico").read_bytes()


def test_icon_assets_are_declared_for_python_and_windows_packages() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert "*.png" in package_data["componentpress.app_assets"]

    spec = (ROOT / "packaging" / "windows.spec").read_text(encoding="utf-8")
    assert 'root / "src" / "componentpress" / "app_assets" / "componentpress.png"' in spec
    assert 'datas=[(str(application_icon), "componentpress/app_assets")]' in spec
    assert 'windows_icon = spec_directory / "componentpress.ico"' in spec
    assert "icon=str(windows_icon)" in spec
