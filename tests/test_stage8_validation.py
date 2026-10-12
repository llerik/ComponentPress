"""Acceptance checks for the unified project Excel source and full-sheet validation."""

from __future__ import annotations

from pathlib import Path
import shutil

from openpyxl import load_workbook
import pytest

from componentpress.application.identifiers import instance_id
from componentpress.application.preview_service import PreviewService, ValidationIssues
from componentpress.bootstrap import project_repository
from componentpress.data_sources import XlsxReader
from componentpress.domain.diagnostics import ProjectError


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"


def copy_demo(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    shutil.copytree(DEMO, root)
    return root


def test_schema_three_rejects_old_project_without_rewriting_files(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    project = root / "project.yaml"
    original = project.read_bytes()
    project.write_text(project.read_text(encoding="utf-8").replace("schema_version: 5", "schema_version: 2", 1), encoding="utf-8")
    old = project.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert project.read_bytes() == old and original != old


def test_validation_checks_zero_copy_rows_and_keeps_last_successful_snapshot(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    snapshot = project_repository().open(root)
    preview = PreviewService(XlsxReader())
    component_id = "forest-card"
    book_path = root / "data/game.xlsx"
    book = load_workbook(book_path)
    sheet = book["Карты"]
    assert sheet["I3"].value == 0  # Prod excludes this row.
    sheet["D3"] = "assets/images/not-found.png"
    book.save(book_path)
    book.close()

    with pytest.raises(ValidationIssues) as caught:
        preview.refresh(snapshot, component_id)
    issue = caught.value.diagnostics[0]
    assert (issue.code, issue.sheet, issue.cell, issue.node_id) == ("RESOURCE_MISSING", "Карты", "D3", "leaf-art")
    assert preview.cached_data(snapshot, "forest-card") is None
    assert preview.cached_data(snapshot, "event-card") is not None

    book = load_workbook(book_path)
    book["Карты"]["D3"] = "assets/images/animals/enemy/leaf.png"
    book.save(book_path)
    book.close()
    checked = preview.refresh(snapshot, component_id)
    assert checked is not None and len(checked.rows) == 2
    first_id = checked.rows[0].instance_id

    book = load_workbook(book_path)
    book["Карты"]["D3"] = "assets/images/still-missing.png"
    book.save(book_path)
    book.close()
    with pytest.raises(ValidationIssues):
        preview.refresh(snapshot, component_id)
    assert preview.data(snapshot, component_id) is checked
    assert first_id == instance_id(snapshot.model.id, component_id, "Карты", 2)


def test_unchanged_excel_reuses_data_and_saves_its_fast_fingerprint(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)

    class CountingReader:
        def __init__(self):
            self.reads = 0
            self.reader = XlsxReader()

        def read(self, *args, **kwargs):
            self.reads += 1
            return self.reader.read(*args, **kwargs)

    snapshot = project_repository().open(root)
    reader = CountingReader()
    preview = PreviewService(reader)
    data = preview.refresh(snapshot, "forest-card")
    assert data is not None and reader.reads == 2  # Both component sheets.

    moved = snapshot.documents["forest-card"].model.model_copy(update={
        "elements": (snapshot.documents["forest-card"].model.elements[0].model_copy(update={"x_mm": 12.0}),
                     *snapshot.documents["forest-card"].model.elements[1:]),
    })
    checked = preview.refresh(snapshot, "forest-card", component=moved)

    assert checked is data
    assert reader.reads == 2
    state = root / ".componentpress" / "validation.json"
    assert state.is_file()
    state_before = state.read_bytes()
    assert b'"mtime_ns"' in state_before
    assert b'"validated_components"' in state_before
    preview.refresh(snapshot, "forest-card", component=moved)
    assert state.read_bytes() == state_before


def test_empty_sheet_checks_column_bindings_and_manual_resources(tmp_path: Path) -> None:
    from componentpress.domain.nodes import ImageNode

    root = copy_demo(tmp_path)
    book_path = root / "data/game.xlsx"
    book = load_workbook(book_path)
    sheet = book["Карты"]
    sheet.delete_rows(2, sheet.max_row)
    book.save(book_path)
    book.close()
    snapshot = project_repository().open(root)
    model = snapshot.documents["forest-card"].model
    image = model.elements[0]
    assert isinstance(image, ImageNode)

    missing_column = model.model_copy(update={
        "elements": (image.model_copy(update={"source_column": "Нет такого столбца"}), *model.elements[1:]),
    })
    diagnostics = PreviewService(XlsxReader()).validate_all(
        snapshot, manual=True, component_ids=("forest-card",),
        component_overrides={"forest-card": missing_column},
    )
    assert any(item.code == "COLUMN_UNKNOWN" and item.sheet == "Карты" for item in diagnostics)

    missing_resource = model.model_copy(update={
        "elements": (image.model_copy(update={
            "source_mode": "manual", "source_column": None, "source": "assets/images/missing.png",
        }), *model.elements[1:]),
    })
    diagnostics = PreviewService(XlsxReader()).validate_all(
        snapshot, manual=True, component_ids=("forest-card",),
        component_overrides={"forest-card": missing_resource},
    )
    assert any(item.code == "RESOURCE_MISSING" and item.node_id == "leaf-art" for item in diagnostics)


def test_validation_does_not_publish_mixed_excel_generations(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    snapshot = project_repository().open(root)

    class ChangingReader:
        def __init__(self):
            self.reader = XlsxReader()
            self.changed = False

        def read(self, *args, **kwargs):
            data = self.reader.read(*args, **kwargs)
            if not self.changed:
                self.changed = True
                book_path = root / "data/game.xlsx"
                book_path.touch()
            return data

    preview = PreviewService(ChangingReader())
    diagnostics = preview.validate_all(snapshot, force_read=True)

    assert len(diagnostics) == 1
    assert diagnostics[0].code == "FILE_CHANGED_EXTERNALLY"
    assert preview._data == {}
    assert not (root / ".componentpress" / "validation.json").exists()


def test_validate_all_checks_static_component_resources(tmp_path: Path) -> None:
    root = tmp_path / "static-game"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "demo-game", root)
    image_path = root / "assets" / "images" / "animals" / "enemy" / "leaf.png"
    image_path.unlink()
    snapshot = project_repository().open(root)

    diagnostics = PreviewService(XlsxReader()).validate_all(snapshot)

    assert diagnostics
    assert any(item.code == "RESOURCE_MISSING" and item.node_id == "leaf-art" for item in diagnostics)


def test_uuidv5_tracks_project_component_sheet_and_excel_row() -> None:
    expected = instance_id("game", "card", "Карты", 2)
    assert instance_id("game", "card", "Карты", 2) == expected
    assert len(expected) == 36
    assert instance_id("game", "card", "Карты", 3) != expected
    assert instance_id("game", "card", "События", 2) != expected
    assert instance_id("game", "other-card", "Карты", 2) != expected
    assert instance_id("other-game", "card", "Карты", 2) != expected


def test_column_html_rejects_user_supplied_image_tags(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    book_path = root / "data/game.xlsx"
    book = load_workbook(book_path)
    book["Карты"]["K2"] = '<p><img src="assets/images/animals/enemy/leaf.png" /></p>'
    book.save(book_path)
    book.close()
    snapshot = project_repository().open(root)
    with pytest.raises(ValidationIssues) as caught:
        PreviewService(XlsxReader()).refresh(snapshot, "forest-card")
    issue = caught.value.diagnostics[0]
    assert (issue.code, issue.cell, issue.node_id) == ("HTML_UNSUPPORTED", "K2", "heading")
