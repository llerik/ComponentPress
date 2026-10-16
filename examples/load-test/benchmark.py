"""Validate and build the 75-record fixture, recording repeatable timings."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import posixpath
import statistics
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "src"))

from openpyxl import load_workbook
from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtPdf import QPdfDocument

from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest
from componentpress.application.preview_service import PreviewService, ValidationIssues
from componentpress.bootstrap import project_service
from componentpress.build.engine import BuildEngine
from componentpress.build.input_snapshot import InputSnapshotBuilder
from componentpress.data_sources import XlsxReader
from componentpress.i18n import set_language
from componentpress.rendering.exporter import ensure_gui_application

SHEETS = ("ComponentOne", "ComponentTwo", "ComponentThree")
COMPONENTS = ("componentone", "componenttwo", "componentthree")
HEADERS = ("Title", "Subtitle", "Body", "Image 1", "Image 2", "Prod", "Test")
NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
XLSX = ROOT / "data" / "load-test.xlsx"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_fingerprint() -> dict[str, str]:
    files = sorted((REPO / "src" / "componentpress").rglob("*.py"))
    digest_builder = hashlib.sha256()
    for path in files:
        digest_builder.update(path.relative_to(REPO).as_posix().encode())
        digest_builder.update(path.read_bytes())
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {
        "git_head": head,
        "working_tree_dirty": str(bool(status.strip())).lower(),
        "componentpress_source_sha256": digest_builder.hexdigest(),
    }


def check_workbook() -> dict:
    book = load_workbook(XLSX, read_only=True, data_only=True)
    assert tuple(book.sheetnames) == SHEETS, book.sheetnames
    checked_paths: set[str] = set()
    expected_by_sheet = {}
    for name, component_id in zip(SHEETS, COMPONENTS, strict=True):
        rows = list(book[name].iter_rows(values_only=True))
        assert rows[0] == HEADERS, (name, rows[0])
        assert len(rows) == 26, (name, len(rows))
        assert all(len(row) == 7 for row in rows), name
        assert all(row[5:] == (4, 1) for row in rows[1:]), name
        expected_images = {}
        for row in rows[1:]:
            for col in (3, 4):
                path = row[col]
                assert isinstance(path, str) and path.startswith("assets/images/")
                assert (ROOT / path).is_file(), path
                checked_paths.add(path)
                expected_images[(len(expected_images) // 2 + 1, col)] = path
        # All three text cells have non-empty, distinct source passages.
        assert all(all(isinstance(value, str) and value.strip() for value in row[:3]) for row in rows[1:])
        component = (ROOT / "components" / f"{component_id}.yaml").read_text(encoding="utf-8")
        for column in HEADERS[:5]:
            assert column in component, (component_id, column)
        expected_by_sheet[name] = expected_images
    book.close()

    anchors = 0
    embedded_images: set[str] = set()
    with zipfile.ZipFile(XLSX) as archive:
        for sheet_index in range(1, 4):
            name = SHEETS[sheet_index - 1]
            expected_images = expected_by_sheet[name]
            sheet_xml = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index}.xml"))
            sheet_rels = ET.fromstring(archive.read(f"xl/worksheets/_rels/sheet{sheet_index}.xml.rels"))
            drawing_rel = next((r.attrib["Target"] for r in sheet_rels.findall("p:Relationship", NS) if r.attrib["Type"].endswith("/drawing")), None)
            assert drawing_rel
            drawing_path = (drawing_rel.lstrip("/") if drawing_rel.startswith("/")
                            else posixpath.normpath(str(PurePosixPath("xl/worksheets") / drawing_rel)))
            drawing_dir = str(PurePosixPath(drawing_path).parent)
            drawing_rels_path = str(PurePosixPath(drawing_dir) / "_rels" / (PurePosixPath(drawing_path).name + ".rels"))
            drawing_rels = ET.fromstring(archive.read(drawing_rels_path))
            targets = {r.attrib["Id"]: r.attrib["Target"] for r in drawing_rels.findall("p:Relationship", NS)}
            drawing = ET.fromstring(archive.read(drawing_path))
            sheet_anchors = []
            for anchor in drawing.findall("xdr:oneCellAnchor", NS):
                cell = anchor.find("xdr:from", NS)
                row = int(cell.findtext("xdr:row", namespaces=NS))
                col = int(cell.findtext("xdr:col", namespaces=NS))
                embedded = anchor.find(".//a:blip", NS).attrib[f"{{{NS['r']}}}embed"]
                target = targets[embedded]
                image_path = (target.lstrip("/") if target.startswith("/")
                              else posixpath.normpath(str(PurePosixPath(drawing_dir) / target)))
                embedded_images.add(image_path)
                source_path = expected_images[(row, col)]
                assert archive.read(image_path) == (ROOT / source_path).read_bytes(), (name, row, col, source_path)
                sheet_anchors.append((row, col))
            expected = set(expected_images)
            assert set(sheet_anchors) == expected and len(sheet_anchors) == 50, sheet_anchors
            anchors += len(sheet_anchors)
    assert anchors == 150
    assert all(path in zipfile.ZipFile(XLSX).namelist() for path in embedded_images)
    return {"worksheets": 3, "rows": 75, "data_columns": 5, "copy_columns": 2,
            "image_cell_paths": len(checked_paths), "embedded_image_anchors": anchors,
            "xlsx_sha256": digest(XLSX)}


def timed_validation(snapshot, reader) -> tuple[float, float]:
    source = snapshot.model.data_source
    from componentpress.project_io.paths import resolve_project_path
    xlsx_path = resolve_project_path(snapshot.root, source.path)
    started = time.perf_counter()
    for sheet in SHEETS:
        reader.read(xlsx_path, sheet, copies_columns=("Prod", "Test"))
    xlsx_ms = (time.perf_counter() - started) * 1000

    preview = PreviewService(reader)
    started = time.perf_counter()
    diagnostics = preview.validate_all(snapshot, force_read=True, manual=True)
    validation_ms = (time.perf_counter() - started) * 1000
    if diagnostics:
        raise ValidationIssues(diagnostics)
    return xlsx_ms, validation_ms


def pdf_preview(path: Path, output: Path, expected_pages: int) -> None:
    document = QPdfDocument()
    error = document.load(str(path))
    assert error == QPdfDocument.Error.None_, (path, error)
    assert document.pageCount() == expected_pages, (path, document.pageCount(), expected_pages)
    for page_index in sorted({0, expected_pages - 1}):
        image = document.render(page_index, QSize(840, 1188))
        assert not image.isNull(), (path, page_index)
        assert image.save(str(output / f"page-{page_index + 1:02}.png"), "PNG")
    document.close()


def system_info() -> dict:
    try:
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        memory = Memory()
        memory.dwLength = ctypes.sizeof(Memory)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
        ram = round(memory.ullTotalPhys / (1024**3), 2)
    except Exception:
        ram = None
    from componentpress.version import APPLICATION_VERSION
    import PySide6
    from PySide6.QtCore import qVersion
    return {"python": platform.python_version(), "os": platform.platform(), "cpu": platform.processor() or None,
            "logical_cpu_count": os.cpu_count(), "ram_gib": ram, "app_version": APPLICATION_VERSION,
            "pyside6": PySide6.__version__, "qt": qVersion(), "workers": 2, "dpi": 300,
            "qt_platform": os.environ.get("QT_QPA_PLATFORM", "default")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument("--start-iteration", type=int, default=1,
                        help="append later iterations to the existing timing files")
    args = parser.parse_args()
    assert args.repetitions >= 1 and args.start_iteration >= 1
    process_started = time.perf_counter()
    app = ensure_gui_application()
    set_language("en")
    fixture = check_workbook()
    snapshot = project_service().open(ROOT)
    assert tuple(snapshot.documents) == COMPONENTS, tuple(snapshot.documents)
    reader = XlsxReader()
    service = project_service()
    session = service.open_session(ROOT)
    builder = BuildService(service)
    source_meta = source_fingerprint()
    hardware = system_info()
    previous = None
    if args.start_iteration > 1:
        previous = json.loads((ROOT / "timings.json").read_text(encoding="utf-8"))
        assert previous["fixture"]["xlsx_sha256"] == fixture["xlsx_sha256"]
        records = previous["measurements"]
        all_builds = previous["builds"]
    else:
        records = []
        all_builds = []

    for mode in ("prod", "test"):
        for iteration in range(args.start_iteration, args.start_iteration + args.repetitions):
            xlsx_ms, validation_ms = timed_validation(snapshot, reader)
            phase_times: dict[str, float] = {}
            original_create = InputSnapshotBuilder.create
            original_render = BuildEngine._render
            import componentpress.build.engine as engine_module
            original_pdf = engine_module.write_print_pdf

            def timed_create(instance, *call_args, **kwargs):
                began = time.perf_counter()
                try:
                    return original_create(instance, *call_args, **kwargs)
                finally:
                    phase_times["snapshot_prepare_ms"] = (time.perf_counter() - began) * 1000

            def timed_render(*call_args, **kwargs):
                began = time.perf_counter()
                try:
                    return original_render(*call_args, **kwargs)
                finally:
                    phase_times["png_render_ms"] = (time.perf_counter() - began) * 1000

            def timed_pdf(*call_args, **kwargs):
                began = time.perf_counter()
                try:
                    return original_pdf(*call_args, **kwargs)
                finally:
                    phase_times["pdf_write_ms"] = (time.perf_counter() - began) * 1000

            image_before = digest(XLSX)
            started = time.perf_counter()
            with patch.object(InputSnapshotBuilder, "create", timed_create), \
                 patch.object(BuildEngine, "_render", staticmethod(timed_render)), \
                 patch.object(engine_module, "write_print_pdf", timed_pdf):
                result = builder.run(session, BuildRequest(mode=mode, max_render_workers=2))
            total_build_ms = (time.perf_counter() - started) * 1000
            assert result.status == "succeeded", (mode, iteration, result.diagnostics)
            assert digest(XLSX) == image_before, "The application changed the source workbook"

            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            expected_copies = 300 if mode == "prod" else 75
            expected_pages = 34 if mode == "prod" else 9
            counts = report["counts"]
            assert counts == {"instances": 75, "copies": expected_copies, "png": 75, "pages": expected_pages}, counts
            assert len(report["placements"]) == expected_copies
            assert len(report["instances"]) == 75
            copy_counts = {item["component_id"]: 0 for item in report["instances"]}
            for item in report["instances"]:
                assert item["copies"] == (4 if mode == "prod" else 1)
                copy_counts[item["component_id"]] += item["copies"]
            assert all(copy_counts.get(component_id) == (100 if mode == "prod" else 25) for component_id in COMPONENTS)
            assert len(list((result.output_directory / "images").glob("*.png"))) == 75
            assert phase_times.keys() == {"snapshot_prepare_ms", "png_render_ms", "pdf_write_ms"}, phase_times

            verification_dir = ROOT / ".componentpress" / "load-test-verification" / mode / f"run-{iteration}"
            verification_dir.mkdir(parents=True, exist_ok=True)
            pdf_preview(result.pdf_path, verification_dir, expected_pages)
            row = {
                "mode": mode,
                "iteration": iteration,
                "first_run": iteration == 1,
                "xlsx_read_3_sheets_ms": round(xlsx_ms, 2),
                "full_validate_3_sheets_ms": round(validation_ms, 2),
                **{key: round(value, 2) for key, value in phase_times.items()},
                "total_build_ms": round(total_build_ms, 2),
                "instances": counts["instances"], "copies": counts["copies"],
                "pages": counts["pages"], "output": str(result.output_directory.relative_to(ROOT)),
                "pdf_sha256": digest(result.pdf_path),
            }
            records.append(row)
            all_builds.append({**row, "report": report})
            print(json.dumps(row, ensure_ascii=False), flush=True)

    input_hashes_after = {"xlsx": digest(XLSX)}
    for image in sorted((ROOT / "assets" / "images").rglob("*.png")):
        input_hashes_after[image.relative_to(ROOT).as_posix()] = digest(image)
    fields = list(records[0])
    with (ROOT / "timings.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    summary = {
        "fixture": fixture,
        "machine": hardware,
        "application_source": source_meta,
        "process_start_seconds_including_fixture_checks": (
            previous["process_start_seconds_including_fixture_checks"] if previous else
            round(time.perf_counter() - process_started, 2)
        ),
        "environment_note": "First iteration is the first build in this process. All iterations share OS file cache; no cold-cache claim.",
        "measurements": records,
        "medians": {
            mode: {
                metric: round(statistics.median(row[metric] for row in records if row["mode"] == mode), 2)
                for metric in ("xlsx_read_3_sheets_ms", "full_validate_3_sheets_ms", "snapshot_prepare_ms", "png_render_ms", "pdf_write_ms", "total_build_ms")
            }
            for mode in ("prod", "test")
        },
        "source_hashes_after": input_hashes_after,
        "builds": all_builds,
    }
    (ROOT / "timings.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"medians": summary["medians"], "timings_csv": str(ROOT / "timings.csv"),
                      "timings_json": str(ROOT / "timings.json")}, ensure_ascii=False, indent=2))
    app.processEvents()


if __name__ == "__main__":
    main()
